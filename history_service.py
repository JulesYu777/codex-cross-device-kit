"""Run logged, account-verified Codex history service cycles."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import time

from history_daemon import Daemon, service_paths
from history_exchange import _child
from history_live import assert_native_version, atomic_bytes, file_lock, reconcile_pending, rpc_server
from history_stage import _object


def account_fingerprint(codex_home, app_cli):
    home, executable = (Path(value).expanduser().resolve(strict=True) for value in (codex_home, app_cli))
    assert_native_version(executable)
    with rpc_server(home, executable) as call:
        account = call('account/read', {'refreshToken': False})['account']
    if account is None or account['type'] != 'chatgpt':
        raise RuntimeError('A local ChatGPT account is required')
    return sha256(account['email'].strip().lower().encode()).hexdigest()


def _cycle(config, cycles):
    paths = service_paths(config)
    state_dir = paths['state_dir']
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(state_dir / 'service.lock') as owned:
        if not owned:
            return 0
        try:
            daemon = Daemon(config)
            ready = _child(daemon.exchange, f"{config['device']}/ready.json")
            try:
                key = account_fingerprint(daemon.home, daemon.app_cli)
                if key != config['account_fingerprint']:
                    raise RuntimeError('Local account fingerprint changed')
            except (OSError, RuntimeError, ValueError, KeyError):
                ready.unlink(missing_ok=True)
                raise
            for _ in range(cycles):
                pending = reconcile_pending(daemon.home, state_dir / 'backups', daemon.app_cli)
                result = {'at': time.time(), **daemon.tick(), 'pending_index': pending}
                atomic_bytes(state_dir / 'last-run.json', (json.dumps(result, sort_keys=True) + '\n').encode())
                with (state_dir / 'runs.jsonl').open('a', encoding='utf-8') as log:
                    log.write(json.dumps(result, sort_keys=True) + '\n')
                print(json.dumps(result, sort_keys=True), flush=True)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            message = str(exc)
            safe = {'A local ChatGPT account is required', 'Local account fingerprint changed'}
            failure = {'at': time.time(), 'counts': {'service-error': 1},
                       'error_type': type(exc).__name__, 'reason': message if message in safe else type(exc).__name__}
            atomic_bytes(state_dir / 'last-run.json', (json.dumps(failure, sort_keys=True) + '\n').encode())
            print(json.dumps(failure, sort_keys=True), flush=True)
            return 1
    return 0


def run(config_path, cycles=1, *, watch=False):
    config_path = Path(config_path).expanduser().resolve(strict=True)
    while True:
        config = json.loads(config_path.read_bytes(), object_pairs_hook=_object)
        interval = config.get('interval_seconds', 60)
        if type(interval) not in (int, float) or not 1 <= interval <= 86400:
            raise ValueError('Invalid service interval')
        result = _cycle(config, cycles)
        if not watch:
            return result
        time.sleep(interval)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--cycles', type=int, choices=range(1, 33), default=1)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    raise SystemExit(run(args.config, args.cycles, watch=args.watch))
