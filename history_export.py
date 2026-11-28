"""Export a selected native lineage or a readable text transcript."""

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from uuid import UUID

from history_exchange import _child
from history_lineage import _NAME, parse_chain
from history_lineage_recovery import native_turns
from history_live import assert_native_version, atomic_bytes, indexed_path, rpc_server
from history_stage import _object
from history_storage import DEFAULT_MAX_BYTES, file_signature, read_rollout, resolve_rollout


def _source(codex_home, thread_id, output, app_cli):
    if str(UUID(thread_id)) != thread_id:
        raise ValueError('Expected the canonical thread ID')
    home, executable = (Path(value).expanduser().resolve(strict=True) for value in (codex_home, app_cli))
    output = Path(output).expanduser()
    if output.exists() or output.is_symlink():
        raise ValueError('Export destination must be a new path')
    output = output.resolve()
    if output.is_relative_to(home):
        raise ValueError('Export destination must be outside CODEX_HOME')
    assert_native_version(executable)
    database = (home / 'state_5.sqlite').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(database, uri=True)) as db:
        row = db.execute('SELECT history_mode FROM threads WHERE id=?', (thread_id,)).fetchone()
    if row is None or row[0] != 'paginated':
        raise ValueError('Expected a paginated thread in the local native index')
    leaf = indexed_path(home, thread_id)
    if leaf is None or not leaf.is_relative_to(home) or leaf.parent == home or leaf == home:
        raise ValueError('Indexed rollout is outside the local history directories')
    if leaf.relative_to(home).parts[0] not in {'sessions', 'archived_sessions'}:
        raise ValueError('Indexed rollout is outside the local history directories')
    return home, leaf, output, executable


def _rollout(home, path):
    canonical, actual = resolve_rollout(path)
    for candidate in (canonical, actual):
        if _child(home, candidate.relative_to(home).as_posix()) != candidate:
            raise ValueError('Local rollout resolves to an unexpected path')
    return canonical, actual


def _inventory(home, thread_id):
    result = {}
    for root in ('sessions', 'archived_sessions'):
        for path in (home / root).rglob(f'rollout-*-{thread_id}*.jsonl*'):
            if not path.name.endswith(('.jsonl', '.jsonl.zst')):
                continue
            plain = path.with_suffix('') if path.suffix == '.zst' else path
            match = _NAME.fullmatch(plain.name)
            if match is not None and match['logical'] == thread_id:
                result.setdefault(match['physical'] or thread_id, set()).add(plain)
    return result


def export_lineage(codex_home, thread_id, output, app_cli, *, decompressor=None):
    home, leaf, output, executable = _source(codex_home, thread_id, output, app_cli)
    inventory = _inventory(home, thread_id)
    entries, signatures, seen, remaining = [], [], set(), DEFAULT_MAX_BYTES
    cursor = leaf
    while True:
        canonical, actual = _rollout(home, cursor)
        if canonical in seen:
            raise ValueError('Cycle in the native rollout paths')
        seen.add(canonical)
        before = file_signature(actual)
        raw = read_rollout(actual, decompressor=decompressor, max_bytes=remaining)
        remaining -= len(raw)
        entries.append((canonical.relative_to(home), raw))
        signatures.append((canonical, actual, before))
        first = json.loads(raw.partition(b'\n')[0], object_pairs_hook=_object)
        if not isinstance(first, dict) or first.get('type') != 'session_meta' or not isinstance(first.get('payload'), dict):
            raise ValueError('Expected a native metadata header')
        dependency = first['payload'].get('history_base')
        if dependency is None:
            break
        if not isinstance(dependency, dict) or not isinstance(dependency.get('thread_id'), str):
            raise ValueError('Invalid native lineage dependency')
        paths = inventory.get(dependency['thread_id'], set())
        if len(paths) != 1:
            raise ValueError('Native lineage has a missing or ambiguous physical predecessor')
        cursor, = paths
    chain = parse_chain(thread_id, reversed(entries))
    with rpc_server(home, executable) as call:
        turns = native_turns(call, thread_id)
    if not turns or any(status not in {'completed', 'interrupted', 'failed'} for _, status in turns):
        raise ValueError('Select a thread with indexed, settled native turns')

    manifest = {'version': 1, 'thread_id': thread_id, 'turns': turns, 'segments': []}
    with TemporaryDirectory(prefix='.lineage-export-', dir=output.parent) as temp:
        bundle = Path(temp) / 'bundle'
        for segment in chain.segments:
            path = bundle / segment.path
            atomic_bytes(path, segment.raw, new=True)
            manifest['segments'].append({'path': segment.path.as_posix(), 'bytes': len(segment.raw),
                                         'sha256': sha256(segment.raw).hexdigest()})
        atomic_bytes(bundle / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode(), new=True)
        if (indexed_path(home, thread_id) != leaf or any(
            _rollout(home, canonical) != (canonical, actual) or file_signature(actual) != signature
            for canonical, actual, signature in signatures
        )):
            raise ValueError('Native history changed during lineage export')
        if output.exists() or output.is_symlink():
            raise ValueError('Export destination must be a new path')
        bundle.rename(output)
    return {'thread_id': thread_id, 'segments': len(chain.segments), 'turns': len(turns)}


def _text_items(call, thread_id, turn_id):
    params = {'threadId': thread_id, 'turnId': turn_id, 'sortDirection': 'asc', 'limit': 100,
              'deferOutput': {'commandExecution': True, 'mcpToolCall': True}}
    while True:
        page = call('thread/items/list', params)
        for entry in page['data']:
            item = entry['item']
            if item['type'] == 'userMessage':
                parts = [part['text'] for part in item['content'] if part['type'] == 'text']
                if parts:
                    yield 'User', '\n\n'.join(parts)
            elif item['type'] == 'agentMessage' and item['text']:
                yield 'Assistant', item['text']
        if not page.get('nextCursor'):
            break
        params['cursor'] = page['nextCursor']


def archive_markdown(codex_home, thread_id, output, app_cli):
    home, leaf, output, executable = _source(codex_home, thread_id, output, app_cli)
    rollout = _rollout(home, leaf)
    signature = file_signature(rollout[1])
    with rpc_server(home, executable) as call:
        metadata = call('thread/read', {'threadId': thread_id})['thread']
        name = ' '.join((metadata['name'] or 'Codex conversation').split())
        turns = native_turns(call, thread_id)
        if not turns or any(status not in {'completed', 'interrupted', 'failed'} for _, status in turns):
            raise ValueError('Select a thread with indexed, settled native turns')
        sections = [f'# {name}\n\nThread ID: `{thread_id}`',
                    'User and assistant text from the native turn index. Attachments and tool output are omitted.']
        for number, (turn, status) in enumerate(turns, 1):
            sections.append(f'## Turn {number} · {status}')
            for role, text in _text_items(call, thread_id, turn):
                sections.append(f'### {role}\n\n' + '\n'.join(f'> {line}' for line in text.split('\n')))
        if (native_turns(call, thread_id) != turns or indexed_path(home, thread_id) != leaf
                or _rollout(home, leaf) != rollout or file_signature(rollout[1]) != signature):
            raise ValueError('Native history changed during Markdown export')
    data = ('\n\n'.join(sections) + '\n').encode('utf-8')
    atomic_bytes(output, data, new=True)
    return {'thread_id': thread_id, 'turns': len(turns), 'bytes': len(data), 'sha256': sha256(data).hexdigest()}
