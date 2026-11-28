"""Exercise isolated native recovery using a portable three-segment fixture."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from history_lineage_recovery import native_turns, recover, rollback
from history_live import rpc_server


def build(target):
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    cwd = '/fixture/codex-project'
    thread = str(uuid4())
    base, offset, ordinal = None, 0, 0
    turns, segments = [], []
    for index in range(3):
        segment_id = thread if index == 0 else str(uuid4())
        turn = str(uuid4())
        message = 'msg_' + uuid4().hex
        user_message = str(uuid4())
        prompt = 'Synthetic Codex history check · 双端验收'
        content = f'Synthetic response for turn {index + 1}'
        status = 'interrupted' if index == 1 else 'completed'
        header = {'id': thread, 'timestamp': stamp, 'cwd': cwd, 'runtime_workspace_roots': [cwd],
                  'originator': 'codex_cli_rs', 'cli_version': '0.171.0-alpha.13', 'source': 'cli',
                  'model_provider': 'openai', 'history_mode': 'paginated', 'account_scoped': False}
        terminal = ({'type': 'task_complete', 'turn_id': turn, 'root_turn_id': turn,
                     'completed_at': int(now.timestamp()), 'duration_ms': 1, 'last_agent_message': content}
                    if status == 'completed' else {'type': 'turn_aborted', 'turn_id': turn, 'reason': 'interrupted'})
        rows = [{'timestamp': stamp, 'type': kind, 'payload': payload} for kind, payload in (
            ('session_meta', header),
            ('event_msg', {'type': 'task_started', 'turn_id': turn, 'root_turn_id': turn,
                           'started_at': int(now.timestamp()), 'collaboration_mode_kind': 'default'}),
            ('event_msg', {'type': 'item_completed', 'thread_id': thread, 'turn_id': turn,
                           'started_at_ms': int(now.timestamp() * 1000), 'completed_at_ms': int(now.timestamp() * 1000),
                           'item': {'type': 'UserMessage', 'id': user_message, 'client_id': None,
                                    'content': [{'type': 'text', 'text': prompt, 'text_elements': []}]}}),
            ('turn_context', {'turn_id': turn, 'root_turn_id': turn, 'cwd': cwd, 'model': 'gpt-5.4'}),
            ('event_msg', {'type': 'item_completed', 'thread_id': thread, 'turn_id': turn,
                           'started_at_ms': int(now.timestamp() * 1000), 'completed_at_ms': int(now.timestamp() * 1000),
                           'item': {'type': 'AgentMessage', 'id': message, 'phase': 'final_answer',
                                    'content': [{'type': 'Text', 'text': content}]}}),
            ('response_item', {'type': 'message', 'id': message, 'role': 'assistant',
                               'content': [{'type': 'output_text', 'text': content}]}),
            ('event_msg', terminal),
        )]
        if base is not None:
            rows[0]['payload']['history_base'] = {'thread_id': base, 'end_byte_offset': offset,
                                                  'end_ordinal_exclusive': ordinal}
        raw = b''.join((json.dumps({**row, 'ordinal': i}) + '\n').encode() for i, row in enumerate(rows, ordinal))
        name = f'rollout-{now:%Y-%m-%dT%H-%M}-{index:02}-{thread}' + (f'_{segment_id}' if index else '') + '.jsonl'
        relative = Path(f'sessions/{now:%Y/%m/%d}') / name
        segments.append((relative.as_posix(), raw))
        turns.append([turn, status])
        base, offset, ordinal = segment_id, len(raw), ordinal + len(rows)
    target.mkdir(parents=True)
    (target/'fixture.json').write_text(json.dumps({'thread': thread, 'source_cwd': cwd, 'turns': turns}), encoding='utf-8')
    for stage in (1, 2, 3):
        bundle = target / str(stage)
        inventory = []
        for relative, raw in segments[:stage]:
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            inventory.append({'path': relative, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
        (bundle/'manifest.json').write_text(json.dumps({'version': 1, 'thread_id': thread,
                        'turns': turns[:stage], 'segments': inventory}), encoding='utf-8')


def current(destination):
    return json.loads((destination/'head.json').read_bytes())['generation']


def exercise(fixture, executable):
    meta = json.loads((fixture/'fixture.json').read_bytes())
    with TemporaryDirectory(prefix='lineage-recovery-') as scratch:
        area = Path(scratch).resolve()
        workspace = area / '跨端 Unicode project'
        workspace.mkdir()
        destination = area / 'generations'
        mappings = [{'from': meta['source_cwd'], 'to': str(workspace)}]
        original = recover(fixture/'1', destination, executable, mappings)
        before = current(destination)
        assert original['turns'] == 1 and before == original['generation']
        # Stop the process after all native verification and immediately before head publication.
        crash = """
import json, os, sys
from pathlib import Path
import history_lineage_recovery as h
old = h.atomic_bytes
def write(path, *args, **kwargs):
    if path.name == 'head.json':
        os._exit(83)
    return old(path, *args, **kwargs)
h.atomic_bytes = write
h.recover(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), json.loads(sys.argv[4]))
"""
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p))
        stopped = subprocess.run([sys.executable, '-c', crash, str(fixture/'3'), str(destination), str(executable),
                                  json.dumps(mappings)], env=env)
        assert stopped.returncode == 83, stopped.returncode
        assert current(destination) == before
        with rpc_server(destination/before/'home', executable) as call:
            assert native_turns(call, meta['thread']) == meta['turns'][:1]
        applied = recover(fixture/'3', destination, executable, mappings)
        assert applied['turns'] == 3 and current(destination) != before
        assert recover(fixture/'3', destination, executable, mappings)['status'] == 'unchanged'
        revert = rollback(destination, executable, applied['generation'])
        assert revert['generation'] == before and current(destination) == before and revert['turns'] == 1
        final = recover(fixture/'3', destination, executable, mappings)
        with rpc_server(destination/final['generation']/'home', executable) as call:
            got = native_turns(call, meta['thread'])
        assert got == meta['turns']
        print(json.dumps({'thread': meta['thread'], 'native_statuses': Counter(status for _, status in got),
                          'restart_after_interrupted_commit': True, 'rollback_verified': True,
                          'idempotent_retry': True, 'turn_ids_match': True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('build').add_argument('fixture', type=Path)
    verify = commands.add_parser('exercise')
    verify.add_argument('fixture', type=Path)
    verify.add_argument('app_cli', type=Path)
    args = parser.parse_args()
    if args.action == 'build':
        build(args.fixture.resolve())
    else:
        exercise(args.fixture.resolve(strict=True), args.app_cli.resolve(strict=True))


if __name__ == '__main__':
    main()
