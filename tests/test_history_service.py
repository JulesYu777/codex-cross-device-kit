from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from history_service import run


class HistoryServiceTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='history-service-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config = {
            'home': str(self.root / 'native'),
            'app_cli': str(self.root / 'codex'),
            'exchange': str(self.root / 'exchange'),
            'state_dir': str(self.root / 'state'),
            'device': 'windows',
            'peer': 'mac',
            'pair_id': 'fixture-pair',
            'account_fingerprint': 'fixture-fingerprint',
            'path_maps': [],
            'interval_seconds': 3,
        }
        self.path = self.root / 'history-service.local.json'

    def write_config(self):
        self.path.write_text(json.dumps(self.config), encoding='utf-8')

    def test_overlapping_state_is_rejected_before_directory_creation(self):
        state = self.root / 'native' / 'state'
        self.config['state_dir'] = str(state)
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'disjoint'):
            run(self.path)
        self.assertFalse(state.exists())

    def test_changed_account_revokes_readiness_before_processing(self):
        self.write_config()
        ready = self.root / 'exchange' / 'windows' / 'ready.json'
        ready.parent.mkdir(parents=True)
        ready.write_text('{}', encoding='utf-8')
        with (patch('history_service.account_fingerprint', return_value='changed'),
              patch('history_service.reconcile_pending') as reconcile, redirect_stdout(StringIO())):
            self.assertEqual(run(self.path), 1)
        reconcile.assert_not_called()
        self.assertFalse(ready.exists())
        recorded = json.loads((self.root / 'state' / 'last-run.json').read_bytes())
        self.assertEqual(recorded['reason'], 'Local account fingerprint changed')

    def test_watcher_rereads_the_config_each_cycle(self):
        self.write_config()
        values = []

        def cycle(config, count):
            self.assertEqual(count, 1)
            values.append(config['interval_seconds'])
            return 0

        def sleep(delay):
            if delay == 7:
                raise KeyboardInterrupt
            self.assertEqual(delay, 3)
            self.config['interval_seconds'] = 7
            self.write_config()

        with (patch('history_service._cycle', side_effect=cycle), patch('history_service.time.sleep', side_effect=sleep),
              self.assertRaises(KeyboardInterrupt)):
            run(self.path, watch=True)
        self.assertEqual(values, [3, 7])
