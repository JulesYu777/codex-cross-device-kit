from contextlib import closing, contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from history_export import archive_markdown, export_lineage
from history_lineage_recovery import read_bundle
from tools.history_fixture import build


class NativeExportTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='native-export-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = self.root / 'fixture'
        build(self.fixture)
        bundle = self.fixture / '3'
        self.manifest = json.loads((bundle / 'manifest.json').read_bytes())
        self.thread = self.manifest['thread_id']
        self.home = self.root / 'home'
        shutil.copytree(bundle / 'sessions', self.home / 'sessions')
        self.executable = self.root / 'codex-fixture'
        self.executable.touch()
        leaf = self.home / self.manifest['segments'][-1]['path']
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, history_mode TEXT)')
            db.execute('INSERT INTO threads VALUES (?, ?, ?)', (self.thread, str(leaf), 'paginated'))

    @contextmanager
    def app_server(self, home, exe):
        self.assertEqual((home, exe), (self.home, self.executable))

        def call(method, params):
            self.assertEqual(params['threadId'], self.thread)
            if method == 'thread/read':
                return {'thread': {'name': 'Unicode 笔记\n标题'}}
            if method == 'thread/turns/list':
                turns = self.manifest['turns']
                page = turns[1:] if params.get('cursor') else turns[:1]
                data = [{'id': turn_id, 'status': status} for turn_id, status in page]
                return {'data': data, 'nextCursor': None if params.get('cursor') else 'turn-page-two'}
            self.assertEqual(method, 'thread/items/list')
            if not params.get('cursor'):
                return {'data': [{'item': {'type': 'userMessage', 'content': [
                    {'type': 'text', 'text': '用户正文'}, {'type': 'image', 'url': 'fixture-attachment'}]}}],
                        'nextCursor': 'item-page-two'}
            return {'data': [{'item': {'type': 'commandExecution', 'command': 'fixture-tool-output'}},
                             {'item': {'type': 'agentMessage', 'text': '答复\n第二行'}}]}

        yield call

    def test_exports_an_exact_multisegment_snapshot_in_native_turn_order(self):
        dest = self.root / 'bundle'
        with patch('history_export.assert_native_version'), patch('history_export.rpc_server', self.app_server):
            receipt = export_lineage(self.home, self.thread, dest, self.executable)
            self.assertEqual(receipt['segments'], 3)
            self.assertEqual(receipt['turns'], 3)
            chain, signatures, _ = read_bundle(dest)
            self.assertEqual(signatures, self.manifest['turns'])
            for segment in chain.segments:
                self.assertEqual(segment.raw, (self.home / segment.path).read_bytes())

    def test_missing_parent_creates_no_partial_export(self):
        (self.home / self.manifest['segments'][1]['path']).unlink()
        dest = self.root / 'bundle'
        with patch('history_export.assert_native_version'), self.assertRaisesRegex(ValueError, 'missing or ambiguous'):
            export_lineage(self.home, self.thread, dest, self.executable)
        self.assertFalse(dest.exists())

    def test_markdown_keeps_text_status_and_exclusive_output(self):
        dest = self.root / 'notes' / 'history.md'
        with patch('history_export.assert_native_version'), patch('history_export.rpc_server', self.app_server):
            receipt = archive_markdown(self.home, self.thread, dest, self.executable)
            self.assertEqual(receipt['turns'], 3)
            body = dest.read_text(encoding='utf-8')
            self.assertIn('# Unicode 笔记 标题', body)
            self.assertEqual(body.count('> 用户正文'), 3)
            self.assertEqual(body.count('> 答复\n> 第二行'), 3)
            self.assertIn('## Turn 2 · interrupted', body)
            self.assertNotIn('fixture-attachment', body)
            self.assertNotIn('fixture-tool-output', body)
            with self.assertRaisesRegex(ValueError, 'new path'):
                archive_markdown(self.home, self.thread, dest, self.executable)
