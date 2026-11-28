"""Publish verified standalone Codex history while coordinating native writers."""

from contextlib import closing, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import sqlite3
import subprocess
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Thread

from history_stage import _read


WRITER_DIR = 'thread-writer-locks'
SUPPORTED = {'codex-cli 0.171.0-alpha.13', 'codex-cli 0.171.0-alpha.13.2'}


class NativeRPCError(RuntimeError):
    pass


class BusyHistory(RuntimeError):
    pass


@contextmanager
def file_lock(path, create=True):
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name == 'nt':
            import ctypes as c
            from ctypes import wintypes as w
            import msvcrt

            class Overlapped(c.Structure):
                _fields_ = [('internal', c.c_size_t), ('high', c.c_size_t),
                            ('offset', w.DWORD), ('offset_high', w.DWORD), ('event', w.HANDLE)]

            kernel = c.WinDLL('kernel32', use_last_error=True)
            lock = kernel.LockFileEx
            lock.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, c.POINTER(Overlapped)]
            lock.restype = w.BOOL
            acquired = bool(lock(msvcrt.get_osfhandle(fd), 3, 0, 0xffffffff, 0xffffffff,
                                 c.byref(Overlapped())))
            if not acquired and c.get_last_error() != 33:
                raise c.WinError(c.get_last_error())
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        yield acquired
    finally:
        os.close(fd)


@contextmanager
def publication_lock(home, thread_id):
    locks = home / WRITER_DIR
    locks.mkdir(exist_ok=True)
    with file_lock(locks / '.coordination.lock') as owned:
        if not owned:
            yield False
            return
        try:
            with file_lock(locks / f'{thread_id}.lock', create=False) as idle:
                allowed = idle
        except FileNotFoundError:
            allowed = True
        yield allowed


def digest(data):
    return sha256(data).hexdigest()


def atomic_bytes(path, data, *, new=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix='.codex-sync-', delete=False) as stream:
        temp = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if new:
            os.link(temp, path)
        else:
            temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def indexed_path(home, thread_id):
    location = (home / 'state_5.sqlite').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(location, uri=True, timeout=5)) as connection:
        row = connection.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
    if row is None:
        return None
    path = row[0]
    if os.name == 'nt' and path.startswith('\\\\?\\') and path[5:7] == ':\\':
        path = path[4:]
    indexed = Path(path).resolve()
    return indexed.with_suffix('') if indexed.name.endswith('.jsonl.zst') else indexed


@contextmanager
def rpc_server(home, executable):
    env = dict(os.environ, CODEX_HOME=str(home))
    env.pop('CODEX_SQLITE_HOME', None)
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    with subprocess.Popen([str(executable), 'app-server', '--stdio', '--disable', 'plugins',
                           '--disable', 'remote_plugin', '-c', 'analytics.enabled=false'], env=env,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kwargs) as proc:
        events = Queue()

        def read_events():
            for line in proc.stdout:
                if line.startswith(b'{'):
                    events.put(json.loads(line))
            events.put(None)

        Thread(target=read_events, daemon=True).start()
        request_id = 0

        def send(packet):
            proc.stdin.write(json.dumps({'jsonrpc': '2.0', **packet}).encode() + b'\n')
            proc.stdin.flush()

        def call(method, params):
            nonlocal request_id
            request_id += 1
            send({'id': request_id, 'method': method, 'params': params})
            while True:
                try:
                    event = events.get(timeout=25)
                except Empty as exc:
                    raise NativeRPCError(f'App Server timed out during {method}') from exc
                if event is None:
                    raise NativeRPCError(f'App Server exited during {method}')
                if event.get('id') != request_id:
                    continue
                if 'error' in event:
                    msg = event['error']['message']
                    if 'active writer' in msg or 'open in another' in msg:
                        raise BusyHistory(method)
                    raise NativeRPCError(f'{method}: {msg}')
                return event['result']

        try:
            call('initialize', {'clientInfo': {'name': 'native-history-sync', 'version': '1'},
                                'capabilities': {'experimentalApi': True}})
            send({'method': 'initialized'})
            yield call
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def verify_native(home, executable, thread_id, path, cwd, expected):
    with rpc_server(home, executable) as call:
        resumed = call('thread/resume', {'threadId': thread_id, 'path': str(path), 'cwd': str(cwd),
                                         'excludeTurns': True})
        if resumed['thread']['id'] != thread_id or not Path(resumed['cwd']).samefile(cwd):
            raise NativeRPCError('Native session ID or workspace did not match')
        params = {'threadId': thread_id, 'sortDirection': 'asc', 'limit': 25, 'itemsView': 'summary'}
        seen = []
        while True:
            page = call('thread/turns/list', params)
            seen.extend(turn['id'] for turn in page['data'])
            if not page.get('nextCursor'):
                break
            params['cursor'] = page['nextCursor']
        if seen[:len(expected)] != expected:
            raise NativeRPCError('Native completed turn IDs did not match the staged prefix')


def recover_receipt(home, backup_dir, app_cli, journal):
    try:
        state = json.loads(journal.read_bytes())
    except FileNotFoundError:
        return 'unchanged'
    thread_id = state['thread_id']
    path, cwd = Path(state['path']), Path(state['cwd'])
    if journal.name != f'{thread_id}.pending.json' or not path.resolve().is_relative_to(home):
        raise ValueError('Invalid pending history receipt')
    persisted = path.read_bytes() if path.is_file() else None
    length = state['length']
    if persisted is None or digest(persisted[:length]) != state['after']:
        with publication_lock(home, thread_id) as idle:
            if not idle:
                return 'pending-index'
            current = path.read_bytes() if path.is_file() else None
            if (current is None and state['before'] is None
                    or current is not None and digest(current) == state['before']):
                journal.unlink(missing_ok=True)
        return 'stale'
    try:
        verify_native(home, app_cli, thread_id, path, cwd, state['turn_ids'])
    except BusyHistory:
        return 'pending-index'
    journal.unlink()
    return 'applied'


def reconcile_pending(home, backup_dir, app_cli):
    results = {}
    for receipt in sorted(backup_dir.glob('*.pending.json')):
        thread_id = receipt.name.removesuffix('.pending.json')
        try:
            results[thread_id] = recover_receipt(home, backup_dir, app_cli, receipt)
        except (OSError, NativeRPCError, ValueError, KeyError) as exc:
            results[thread_id] = type(exc).__name__
    return results


def assert_native_version(app_cli):
    version = subprocess.check_output([str(app_cli), '--version'], text=True, timeout=10).strip()
    if version not in SUPPORTED:
        raise ValueError(f'Unverified native App Server version: {version}')


def materialize_for_sync(home, thread_id, relative, cwd, app_cli):
    """Let native Codex decompress an idle thread before its next exchange cycle."""
    assert_native_version(app_cli)
    plain = (home / relative).resolve()
    with publication_lock(home, thread_id) as idle:
        if not idle:
            return 'busy'
        if (indexed_path(home, thread_id) != plain or plain.exists()
                or not plain.with_name(plain.name + '.zst').is_file()):
            return 'stale'
    try:
        with rpc_server(home, app_cli) as call:
            result = call('thread/resume', {'threadId': thread_id, 'excludeTurns': True})
            if result['thread']['id'] != thread_id or not Path(result['cwd']).samefile(cwd):
                raise NativeRPCError('Materialized session ID or workspace did not match')
    except BusyHistory:
        return 'busy'
    if not plain.is_file():
        raise NativeRPCError('Native resume did not materialize the rollout')
    return 'materialized'


def apply_rollout(home, thread_id, relative, staged, cwd, expected_turn_ids, baseline_sha256, backup_dir, app_cli):
    home, staged, cwd, backup_dir, app_cli = (Path(p).resolve() for p in (home, staged, cwd, backup_dir, app_cli))
    relative = Path(relative)
    if (relative.is_absolute() or len(relative.parts) < 2 or '..' in relative.parts
            or relative.parts[0] not in {'sessions', 'archived_sessions'}
            or not re.fullmatch(rf'rollout-[0-9T_-]+-{re.escape(thread_id)}\.jsonl', relative.name)):
        raise ValueError('Invalid native rollout path')
    if backup_dir.is_relative_to(home) or home.is_relative_to(backup_dir):
        raise ValueError('Backup directory overlaps native profile')
    assert_native_version(app_cli)
    candidate, records, turns, _ = _read(staged)
    expected = [turn[0]['payload']['turn_id'] for turn in turns]
    meta = records[0]['payload']
    if (meta['id'] != thread_id or expected != expected_turn_ids or not Path(meta['cwd']).samefile(cwd)):
        raise ValueError('Staged profile metadata does not match apply request')

    target = (home / relative).resolve()
    if not target.is_relative_to(home):
        raise ValueError('Native rollout path escapes profile')
    with publication_lock(home, thread_id) as idle:
        if not idle:
            return 'busy'
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = backup_dir / f'{thread_id}.pending.json'
    if journal.is_file():
        return recover_receipt(home, backup_dir, app_cli, journal)

    with TemporaryDirectory(prefix='replay-', dir=backup_dir) as scratch:
        isolated = Path(scratch)
        saved = isolated / relative
        saved.parent.mkdir(parents=True)
        saved.write_bytes(candidate)
        verify_native(isolated, app_cli, thread_id, saved, cwd, expected)

    with publication_lock(home, thread_id) as idle:
        if not idle:
            return 'busy'
        indexed = indexed_path(home, thread_id)
        if baseline_sha256 is None:
            if indexed is not None or target.exists():
                return 'stale'
        else:
            if indexed != target or not target.is_file():
                return 'stale'
            previous = target.read_bytes()
            if digest(previous) != baseline_sha256 or not candidate.startswith(previous):
                return 'stale'
            if previous == candidate:
                return 'unchanged'
            backup = backup_dir / thread_id / f'{baseline_sha256}.jsonl'
            if backup.is_file():
                if digest(backup.read_bytes()) != baseline_sha256:
                    raise ValueError('Existing history backup did not match its digest')
            else:
                atomic_bytes(backup, previous, new=True)
        receipt = {'thread_id': thread_id, 'path': str(target), 'cwd': str(cwd), 'after': digest(candidate),
                   'length': len(candidate), 'before': baseline_sha256, 'turn_ids': expected}
        atomic_bytes(journal, json.dumps(receipt).encode(), new=True)
        try:
            atomic_bytes(target, candidate, new=baseline_sha256 is None)
        except (PermissionError, FileExistsError):
            journal.unlink()
            return 'busy'
    return recover_receipt(home, backup_dir, app_cli, journal)
