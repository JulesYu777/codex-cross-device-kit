"""Two real hosts, isolated Syncthing instances and isolated native Codex homes."""

import argparse
from contextlib import contextmanager
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.error import URLError
from uuid import uuid4

import history_lineage_recovery as lineage
from history_daemon import Daemon, _encode, _json, _save, map_records
from history_live import apply_rollout, atomic_bytes, file_lock, indexed_path, rpc_server
import kit


def wait_for(check, description, timeout=105):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
        except (FileNotFoundError, ConnectionError, TimeoutError, URLError):
            value = None
        if value:
            return value
        time.sleep(1)
    raise TimeoutError(description)


@contextmanager
def syncthing(home, state):
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    with (home.parent/'syncthing.log').open('ab') as log:
        with subprocess.Popen(kit.start_args(home, state), cwd=home, stdout=log, stderr=log, **kwargs) as process:
            try:
                yield
            finally:
                try:
                    kit.api(home, state, 'system/shutdown', method='POST')
                    process.wait(timeout=12)
                except (OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    process.wait(timeout=12)


def commit_native(config, workspace, fixture, current=None):
    home, state, exe = (Path(config[k]) for k in ('home', 'state_dir', 'app_cli'))
    template = json.loads((fixture/'1/manifest.json').read_bytes())
    original = (fixture/'1'/template['segments'][0]['path']).read_bytes()
    metadata = json.loads((fixture/'fixture.json').read_bytes())
    source_rows = [json.loads(row) for row in original.splitlines()]
    fresh_turn = str(uuid4())
    if current is None:
        thread, expected, baseline = str(uuid4()), [], None
        seed_path = Path(template['segments'][0]['path'])
        relative = seed_path.with_name(seed_path.name.replace(metadata['thread'], thread))
        start, before = 0, b''
        with rpc_server(home, exe) as call:
            call('thread/list', {'limit': 1})
    else:
        thread, expected, relative = current['thread'], current['turns'], Path(current['relative'])
        previous = indexed_path(home, thread)
        assert previous == (home/relative).resolve()
        before = previous.read_bytes()
        start = len(before.splitlines()) - 1
        baseline = sha256(before).hexdigest()
    old_msg = next(row['payload']['id'] for row in source_rows if row['type'] == 'response_item')
    old_user = next(row['payload']['item']['id'] for row in source_rows
                    if row['payload'].get('item', {}).get('type') == 'UserMessage')
    rows = json.loads(json.dumps(source_rows).replace(metadata['thread'], thread)
                      .replace(metadata['turns'][0][0], fresh_turn).replace(old_msg, 'msg_' + uuid4().hex)
                      .replace(old_user, str(uuid4())))
    rows = map_records(rows, [{'from': metadata['source_cwd'], 'to': str(workspace)}])
    if current is not None:
        rows = rows[1:]
    for ordinal, record in enumerate(rows, start if current is None else start+1):
        record['ordinal'] = ordinal
    stage = state / 'local-native-candidate.jsonl'
    atomic_bytes(stage, before + _encode(rows))
    verdict = apply_rollout(home, thread, relative, stage, workspace, [*expected, fresh_turn], baseline,
                            state/'backups', exe)
    assert verdict == 'applied', verdict
    if before:
        assert (home/relative).read_bytes().startswith(before)
    return {'thread': thread, 'turns': [*expected, fresh_turn], 'relative': relative.as_posix(),
            'workspace': str(workspace)}


def received(config, other, prior=None):
    home, exe = Path(config['home']), Path(config['app_cli'])
    persisted = indexed_path(home, other['thread'])
    if persisted is None:
        return None
    with rpc_server(home, exe) as call:
        turns = lineage.native_turns(call, other['thread'])
    if [turn[0] for turn in turns] != other['turns']:
        return None
    assert all(turn[1] == 'completed' for turn in turns)
    data = persisted.read_bytes()
    if prior is not None:
        assert data.startswith(prior), 'Receiving changed its earlier local history bytes'
    return data


def bad_bundle_checks(root, fixtures, executable, source_cwd):
    local = root/'loom'
    project = root/'对端的隔离会话'
    project.mkdir()
    maps = [{'from': source_cwd, 'to': str(project)}]
    initial = lineage.recover(fixtures/'1', local, executable, maps)['generation']
    corrupt = root/'damaged-bundle'
    shutil.copytree(fixtures/'3', corrupt)
    leaf = _json(corrupt/'manifest.json')['segments'][-1]['path']
    path = corrupt/leaf
    path.write_bytes(path.read_bytes()[:-150])
    try:
        lineage.recover(corrupt, local, executable, maps)
    except ValueError as err:
        assert 'SHA-256' in str(err)
    else:
        raise AssertionError('Damaged bundle passed recovery')
    assert _json(local/'head.json')['generation'] == initial
    fork = root/'divergent-bundle'
    shutil.copytree(fixtures/'3', fork)
    inventory = _json(fork/'manifest.json')
    first = inventory['segments'][0]
    source = fork/first['path']
    original = source.read_bytes()
    changed = original.replace(b'turn 1', b'fork 1')
    assert changed != original and len(changed) == len(original)
    source.write_bytes(changed)
    first['sha256'] = sha256(changed).hexdigest()
    atomic_bytes(fork/'manifest.json', json.dumps(inventory).encode())
    try:
        lineage.recover(fork, local, executable, maps)
    except ValueError as err:
        assert 'previously verified source bytes' in str(err)
    else:
        raise AssertionError('Divergent history replaced its source prefix')
    assert _json(local/'head.json')['generation'] == initial
    write = lineage.atomic_bytes

    def no_space(destination, data, **kwargs):
        if destination.name == 'head.json':
            raise OSError(errno.ENOSPC, 'injected out-of-space at the recovery commit')
        return write(destination, data, **kwargs)

    lineage.atomic_bytes = no_space
    try:
        try:
            lineage.recover(fixtures/'3', local, executable, maps)
        except OSError as err:
            assert err.errno == errno.ENOSPC
        else:
            raise AssertionError('Disk-full injection did not run')
    finally:
        lineage.atomic_bytes = write
    assert _json(local/'head.json')['generation'] == initial
    with file_lock(local/'recovery.lock') as owned:
        assert owned
        try:
            lineage.recover(fixtures/'3', local, executable, maps)
        except RuntimeError as err:
            assert str(err) == 'Another recovery owns the destination'
        else:
            raise AssertionError('Concurrent recovery acquired the same lock')
    restored = lineage.recover(fixtures/'3', local, executable, maps)
    assert restored['turns'] == 3
    assert lineage.rollback(local, executable, restored['generation'])['generation'] == initial
    return ['truncated-segment', 'same-id-fork', 'ENOSPC-before-commit', 'exclusive-lock', 'retry', 'rollback']


def run(args):
    role, root, control = args.role, args.root.resolve(), args.control.resolve()
    fixture = args.fixture.resolve(strict=True)
    if root.exists() and any(root.iterdir()):
        raise ValueError('Choose a fresh, empty root for the isolated exercise')
    if any(root.is_relative_to(path) or path.is_relative_to(root) for path in (control, fixture)):
        raise ValueError('Keep the exercise root, control folder and fixture separate')
    other = 'mac' if role == 'windows' else 'windows'
    home, data, native, workspace = (root/x for x in ('syncthing', 'transport', 'codex', '项目 workspace'))
    for directory in (data, native, workspace):
        directory.mkdir(parents=True, exist_ok=True)
    control.mkdir(parents=True, exist_ok=True)
    own_signal = lambda step: control/f'{role}-{step}.json'
    peer_signal = lambda step: control/f'{other}-{step}.json'

    def announce(step, value=True):
        atomic_bytes(own_signal(step), json.dumps(value).encode(), new=True)

    def hear(step):
        return wait_for(lambda: _json(peer_signal(step)), f'peer did not reach {step}')

    offer = kit.prepare(home, data, role, args.ip, args.port, args.gui, args.syncthing)
    atomic_bytes(own_signal('offer'), offer.read_bytes(), new=True)
    hear('offer')
    kit.apply(home, peer_signal('offer'))
    info = kit.ready(home)
    exe = args.app_cli.resolve(strict=True)
    exchange = data/'native-exchange'
    exchange.mkdir()
    config = {'home': str(native), 'exchange': str(exchange), 'state_dir': str(root/'state'), 'app_cli': str(exe),
              'device': role, 'peer': other, 'pair_id': control.name, 'account_fingerprint': 'synthetic-acceptance',
              'path_maps': [], 'sync_compressed': True}
    if args.zstd:
        config['decompressor'] = str(args.zstd.resolve(strict=True))
    Path(config['state_dir']).mkdir()
    baseline = commit_native(config, workspace, fixture)
    announce('seed', baseline)
    remote_baseline = hear('seed')
    config['path_maps'] = [{'from': remote_baseline['workspace'], 'to': str(workspace)}]

    def connected():
        status = kit.doctor(home, info)
        return status['connected'] and status['expected_peer_ip']

    def receive(version, old_data=None):
        daemon = Daemon(config)
        if daemon.tick()['counts'].get('waiting-pair'):
            return None
        return received(config, version, old_data)

    def send_offline():
        update = commit_native(config, workspace, fixture, baseline)
        daemon = Daemon(config)
        status = daemon.tick()
        assert status['counts'].get('out:published', 0) >= 1, status['counts']
        announce('offline-update', update)
        return update

    with syncthing(home, info):
        wait_for(connected, 'distinct physical peers did not connect')
        previous = wait_for(lambda: receive(remote_baseline), 'native baseline did not cross network')
        announce('baseline')
        hear('baseline')
        if role == 'windows':
            hear('offline')
            wait_for(lambda: not kit.doctor(home, info)['connected'], 'physical link did not disconnect')
            offline_started = time.monotonic()
            send_offline()
            remote_update = hear('offline-update')
            assert received(config, remote_baseline) == previous
            announce('restart')
            wait_for(connected, 'physical link failed to reconnect')
            wait_for(lambda: receive(remote_update, previous), 'offline native increment failed after restart')
            announce('network', {'connected_again': True, 'offline_seconds': round(time.monotonic() - offline_started, 2)})
            transferred = data/'lineage-fixtures'
            shutil.copytree(fixture, transferred)
            kit.api(home, info, 'db/scan', {'folder': kit.FOLDER_ID}, method='POST')
            hear('packet')
    if role == 'mac':
        announce('offline')
        send_offline()
        remote_update = hear('offline-update')
        assert received(config, remote_baseline) == previous
        hear('restart')
        with syncthing(home, info):
            wait_for(connected, 'physical link failed to reconnect')
            wait_for(lambda: receive(remote_update, previous), 'offline native increment failed after restart')
            announce('network', {'connected_again': True})
            transferred = data/'lineage-fixtures'
            wait_for(lambda: _json(transferred/'fixture.json') and all(
                lineage.read_bundle(transferred/str(stage)) for stage in (1, 2, 3)
            ), 'three-segment bundle did not cross network')
            announce('packet')
    source_cwd = _json(transferred/'fixture.json')['source_cwd']
    faults = bad_bundle_checks(root, transferred, exe, source_cwd)
    native_sig = received(config, remote_update, previous)
    assert native_sig is not None
    report = {'role': role, 'native_rounds_each_direction': len(remote_update['turns']),
              'input_thread': remote_update['thread'], 'local_unchanged_prefix': True,
              'faults': faults, 'transport_restart': True}
    announce('passed', report)
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=['windows', 'mac'])
    for key in ('root', 'control', 'fixture', 'syncthing', 'app-cli'):
        parser.add_argument('--'+key, required=True, type=Path)
    parser.add_argument('--zstd', type=Path)
    parser.add_argument('--ip', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--gui', required=True, type=int)
    run(parser.parse_args())
