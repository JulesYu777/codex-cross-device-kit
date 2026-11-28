"""Optional native history commands for the workspace kit."""

import json
from pathlib import Path
import sys


def add_commands(parser):
    commands = parser.add_subparsers(dest='history_command', required=True)
    export = commands.add_parser('export', help='snapshot a standalone history')
    lineage = commands.add_parser('export-lineage', help='snapshot a complete native segment chain')
    markdown = commands.add_parser('archive-markdown', help='export visible conversation text')
    identify = commands.add_parser('identity', help='read the local account pairing fingerprint')
    for command in (export, lineage, markdown, identify):
        command.add_argument('--codex-home', type=Path, required=True)
        if command is not identify:
            command.add_argument('--thread-id', required=True)
            command.add_argument('--out', type=Path, required=True)
        if command is not export:
            command.add_argument('--app-cli', type=Path, required=True)
    export.add_argument('--packed', action='store_true')

    stage = commands.add_parser('stage-fast-forward', help='stage missing standalone turns')
    stage.add_argument('--source', type=Path, required=True)
    stage.add_argument('--target', type=Path, required=True)
    stage.add_argument('--out', type=Path, required=True)
    exchange = commands.add_parser('exchange-once', help='exchange and stage a single standalone thread')
    exchange.add_argument('--codex-home', type=Path, required=True)
    exchange.add_argument('--thread-id', required=True)
    exchange.add_argument('--exchange', type=Path, required=True)
    exchange.add_argument('--device', required=True)
    exchange.add_argument('--peer', required=True)
    exchange.add_argument('--stage-dir', type=Path, required=True)
    for command in (stage, exchange):
        command.add_argument('--cwd', type=Path, required=True)
        command.add_argument('--source-root')
        command.add_argument('--target-root')
    for command in (export, lineage, exchange):
        command.add_argument('--decompressor', type=Path)

    service = commands.add_parser('service', help='run the account-verified history service')
    service.add_argument('--config', type=Path, required=True)
    service.add_argument('--watch', action='store_true')

    recover = commands.add_parser('recover-lineage', help='build and verify an isolated native profile')
    recover.add_argument('--bundle', type=Path, required=True)
    recover.add_argument('--path-maps', type=Path, required=True)
    rollback = commands.add_parser('rollback-lineage', help='use the previous verified isolated profile')
    rollback.add_argument('--generation', required=True)
    for command in (recover, rollback):
        command.add_argument('--destination', type=Path, required=True)
        command.add_argument('--app-cli', type=Path, required=True)


def run(args):
    if sys.version_info < (3, 12):
        raise ValueError('Native history commands require Python 3.12 or newer')
    command = args.history_command
    if command == 'service':
        from history_service import run as run_service
        return run_service(args.config, watch=args.watch)
    if command == 'identity':
        from history_service import account_fingerprint
        result = {'account_fingerprint': account_fingerprint(args.codex_home, args.app_cli)}
    elif command == 'export':
        from history_stage import export_history
        result = export_history(args.codex_home, args.thread_id, args.out,
                                decompressor=args.decompressor, packed=args.packed)
    elif command == 'export-lineage':
        from history_export import export_lineage
        result = export_lineage(args.codex_home, args.thread_id, args.out, args.app_cli,
                                decompressor=args.decompressor)
    elif command == 'archive-markdown':
        from history_export import archive_markdown
        result = archive_markdown(args.codex_home, args.thread_id, args.out, args.app_cli)
    elif command == 'recover-lineage':
        from history_lineage_recovery import recover
        from history_stage import _object
        mappings = json.loads(args.path_maps.read_bytes(), object_pairs_hook=_object)
        result = recover(args.bundle, args.destination, args.app_cli, mappings)
    elif command == 'rollback-lineage':
        from history_lineage_recovery import rollback
        result = rollback(args.destination, args.app_cli, args.generation)
    elif command == 'exchange-once':
        from history_exchange import exchange_once
        result = exchange_once(args.codex_home, args.thread_id, args.exchange, args.device, args.peer,
                               args.stage_dir, args.cwd, source_root=args.source_root,
                               target_root=args.target_root, decompressor=args.decompressor)
    else:
        from history_stage import stage_fast_forward
        missing = stage_fast_forward(args.source, args.target, args.out, args.cwd,
                                     source_root=args.source_root, target_root=args.target_root)
        result = {'status': 'staged' if missing else 'no-op', 'missing_turn_ids': missing}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get('status') in {'conflict', 'rejected'} else 0
