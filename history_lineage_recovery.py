"""Recover one verified Codex rollout chain in replaceable, isolated profiles."""

import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from tempfile import mkdtemp
from uuid import UUID

from history_daemon import map_records
from history_exchange import _child
from history_lineage import map_chain, parse_chain
from history_live import assert_native_version, atomic_bytes, file_lock, indexed_path, rpc_server
from history_stage import _object
from history_storage import DEFAULT_MAX_BYTES, read_rollout, resolve_rollout


def _read_json(path):
    return json.loads(path.read_bytes(), object_pairs_hook=_object)


def read_bundle(directory):
    """Load the supplied full decoded chain and verify its immutable inventory."""
    manifest_bytes = _child(directory, 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes, object_pairs_hook=_object)
    if type(manifest) is not dict or manifest.get('version') != 1:
        raise ValueError('Unsupported lineage bundle manifest')
    turns = manifest['turns']
    if not isinstance(turns, list) or not turns or any(
        not isinstance(turn, list) or len(turn) != 2 or not isinstance(turn[0], str) or str(UUID(turn[0])) != turn[0]
        or turn[1] not in {'completed', 'interrupted', 'failed'} for turn in turns
    ) or len({turn[0] for turn in turns}) != len(turns):
        raise ValueError('Invalid native turn signature')
    inventory, remaining = [], DEFAULT_MAX_BYTES
    for segment in manifest['segments']:
        source = _child(directory, segment['path'])
        if source.suffix != '.jsonl':
            raise ValueError('Lineage bundles contain decoded JSONL')
        raw = read_rollout(source, max_bytes=remaining)
        if len(raw) != segment['bytes'] or sha256(raw).hexdigest() != segment['sha256']:
            raise ValueError('Lineage segment length or SHA-256 mismatch')
        inventory.append((Path(segment['path']), raw))
        remaining -= len(raw)
    return parse_chain(manifest['thread_id'], inventory), turns, sha256(manifest_bytes).hexdigest()


def native_turns(call, thread):
    params = {'threadId': thread, 'sortDirection': 'asc', 'limit': 25, 'itemsView': 'summary'}
    turns = []
    while True:
        page = call('thread/turns/list', params)
        turns.extend([turn['id'], turn['status']] for turn in page['data'])
        if not page.get('nextCursor'):
            return turns
        params['cursor'] = page['nextCursor']


def _select_index(home, thread, previous, next_path):
    # The database belongs exclusively to a fresh generation. Native App Server
    # already indexed the previous segment and has been closed before this CAS.
    with closing(sqlite3.connect(home / 'state_5.sqlite')) as database, database:
        (old,) = database.execute('SELECT rollout_path FROM threads WHERE id=?', (thread,)).fetchone()
        if not Path(old).samefile(previous):
            raise ValueError('Native index did not point at the projected predecessor')
        if database.execute('UPDATE threads SET rollout_path = ? WHERE id=? AND rollout_path=?',
                            (str(next_path), thread, old)).rowcount != 1:
            raise ValueError('Native index changed during isolated profile construction')


def _project(home, executable, thread, files, workspace, roots):
    previous = None
    request = {'threadId': thread, 'cwd': str(workspace), 'runtimeWorkspaceRoots': roots, 'excludeTurns': True}
    for relative, raw in files:
        path = _child(home, relative.as_posix())
        atomic_bytes(path, raw, new=True)
        if previous is None:
            params = {**request, 'path': str(path)}
        else:
            _select_index(home, thread, previous, path)
            params = request
        with rpc_server(home, executable) as call:
            result = call('thread/resume', params)
            if result['thread']['id'] != thread or not Path(result['cwd']).samefile(workspace):
                raise ValueError('Native recovery returned the wrong thread or workspace')
            native_turns(call, thread)
        previous = path


def _head(root):
    path = root / 'head.json'
    return _read_json(path)['generation'] if path.is_file() else None


def _generation(root, name):
    if not isinstance(name, str) or re.fullmatch(r'profile-[a-z0-9_-]+', name) is None:
        raise ValueError('Invalid recovery generation')
    return _child(root, name)


def _verify(generation, executable, receipt):
    home = _child(generation, 'home')
    for relative, expected in receipt['history'].items():
        if sha256(_child(home, relative).read_bytes()).hexdigest() != expected:
            raise ValueError('Recovered native profile changed after verification')
    indexed = indexed_path(home, receipt['thread'])
    if indexed is None or (indexed.relative_to(home).parts[0] == 'archived_sessions') != receipt['archived']:
        raise ValueError('Cold native index did not preserve the source archive state')
    with rpc_server(home, executable) as call:
        if native_turns(call, receipt['thread']) != receipt['turns']:
            raise ValueError('Cold native read did not match the expected turns')


def _used_files(chain):
    for index, segment in enumerate(chain.segments):
        raw = segment.raw
        if index + 1 < len(chain.segments):
            limit = chain.segments[index + 1].records[0]['payload']['history_base']['end_byte_offset']
            raw = raw[:limit]
        yield segment.path, raw


def _active_path(relative):
    if relative.parts[0] == 'sessions':
        return relative
    # Native resume refuses archived rollouts. Project as active in the fresh
    # destination, then let native archive move the final selected segment.
    year, month, day = relative.name[8:18].split('-')
    return Path('sessions', year, month, day, relative.name)


def recover(bundle, destination, executable, mappings):
    bundle, destination, executable = map(lambda value: Path(value).resolve(), (bundle, destination, executable))
    if bundle.is_relative_to(destination) or destination.is_relative_to(bundle):
        raise ValueError('Bundle and recovery destination must be separate')
    assert_native_version(executable)
    chain, turns, bundle_hash = read_bundle(bundle)
    mapped = map_chain(chain, lambda records: map_records(records, mappings))
    thread = chain.thread_id
    meta = mapped.segments[-1].records[0]['payload']
    workspace = Path(meta['cwd'])
    roots = meta.get('runtime_workspace_roots', [])
    files = [(_active_path(relative), raw) for relative, raw in _used_files(mapped)]
    original_files = list(_used_files(chain))
    archived = original_files[-1][0].parts[0] == 'archived_sessions'
    destination.mkdir(parents=True, exist_ok=True)
    with file_lock(destination / 'recovery.lock') as owned:
        if not owned:
            raise RuntimeError('Another recovery owns the destination')
        previous = _head(destination)
        if previous is not None:
            active = _generation(destination, previous)
            prior = _read_json(active / 'receipt.json')
            if prior['thread'] != thread:
                raise ValueError('Recovery destination belongs to another logical thread')
            prior_turns = prior['turns']
            if turns[:len(prior_turns)] != prior_turns:
                raise ValueError('Recovery would replace or remove an existing native turn')
            prior_source = prior['source']
            if len(original_files) < len(prior_source) or any(
                relative.as_posix() != record['path'] or sha256(raw[:record['bytes']]).hexdigest() != record['sha256']
                for (relative, raw), record in zip(original_files, prior_source)
            ):
                raise ValueError('Recovery would change previously verified source bytes')
            _verify(active, executable, prior)
            if prior['bundle'] == bundle_hash and prior['mappings'] == mappings:
                return {'status': 'unchanged', 'generation': previous, 'turns': len(turns)}
        generation = Path(mkdtemp(prefix='profile-', dir=destination))
        home = generation / 'home'
        home.mkdir()
        _project(home, executable, thread, files, workspace, roots)
        for relative, raw in files:
            if not _child(home, relative.as_posix()).read_bytes().startswith(raw):
                raise ValueError('Native recovery changed the verified segment prefix')
        history_paths = [home / relative for relative, _ in files]
        if archived:
            with rpc_server(home, executable) as call:
                call('thread/archive', {'threadId': thread})
            history_paths = []
            for relative, _ in files:
                archived_path = home / 'archived_sessions' / relative.name
                locator = archived_path if archived_path.is_file() or archived_path.with_suffix('.jsonl.zst').is_file() else home / relative
                history_paths.append(resolve_rollout(locator)[1])
            if not history_paths[-1].samefile(resolve_rollout(indexed_path(home, thread))[1]):
                raise ValueError('Native archive moved the index to an unexpected segment')
        receipt = {'thread': thread, 'previous': previous, 'bundle': bundle_hash, 'turns': turns,
                   'mappings': mappings, 'archived': archived,
                   'source': [{'path': relative.as_posix(), 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
                              for relative, raw in original_files],
                   'history': {path.relative_to(home).as_posix(): sha256(path.read_bytes()).hexdigest()
                               for path in history_paths}}
        _verify(generation, executable, receipt)
        atomic_bytes(generation / 'receipt.json', json.dumps(receipt).encode(), new=True)
        atomic_bytes(destination / 'head.json', json.dumps({'generation': generation.name}).encode())
    return {'status': 'restored', 'generation': generation.name, 'turns': len(turns)}


def rollback(destination, executable, generation):
    destination, executable = Path(destination).resolve(strict=True), Path(executable).resolve(strict=True)
    assert_native_version(executable)
    with file_lock(destination / 'recovery.lock') as owned:
        if not owned:
            raise RuntimeError('Another recovery owns the destination')
        if _head(destination) != generation:
            raise ValueError('Active recovery generation already changed')
        current = _generation(destination, generation)
        receipt = _read_json(current / 'receipt.json')
        previous = receipt['previous']
        if previous is None:
            raise ValueError('No previously verified generation to roll back to')
        original = _generation(destination, previous)
        prior = _read_json(original / 'receipt.json')
        _verify(current, executable, receipt)
        _verify(original, executable, prior)
        atomic_bytes(destination / 'head.json', json.dumps({'generation': previous}).encode())
    return {'status': 'rolled-back', 'generation': previous, 'turns': len(prior['turns'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    restore = commands.add_parser('recover')
    restore.add_argument('--bundle', type=Path, required=True)
    restore.add_argument('--path-maps', type=Path, required=True)
    revert = commands.add_parser('rollback')
    revert.add_argument('--generation', required=True)
    for command in (restore, revert):
        command.add_argument('--destination', type=Path, required=True)
        command.add_argument('--app-cli', type=Path, required=True)
    args = parser.parse_args()
    result = (recover(args.bundle, args.destination, args.app_cli, _read_json(args.path_maps))
              if args.command == 'recover' else rollback(args.destination, args.app_cli, args.generation))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
