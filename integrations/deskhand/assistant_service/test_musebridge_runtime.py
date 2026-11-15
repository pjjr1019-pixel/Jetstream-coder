import json
import os
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from deskhand_musebridge_runtime import MuseBridgeRuntime


class RuntimeTests(unittest.TestCase):
    def test_environment_does_not_inherit_jettpad_credentials_or_python_paths(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'MUSEBRIDGE_TOKEN': 'do-not-copy', 'PYTHONPATH': 'foreign', 'PYTHONHOME': 'foreign',
            'DESKHAND_MUSE_ACTIONS': '1', 'DESKHAND_HOST_STOP_EVENT': 'fixture-host-stop'}):
            manager = MuseBridgeRuntime(Path(folder) / 'runtime', folder)
            env = manager._environment()
            self.assertEqual(env['DESKHAND_JETT_RUNTIME'], str(manager.root))
            self.assertEqual(env['DESKHAND_MUSE_READONLY_GATEWAY'], '1')
            self.assertEqual(env['DESKHAND_MUSE_SESSION_GATEWAY'], '1')
            self.assertEqual(env['DESKHAND_MUSE_LOCAL_ADAPTER'], '1')
            self.assertNotIn('DESKHAND_MUSE_ACTIONS', env)
            self.assertEqual(env['DESKHAND_HOST_STOP_EVENT'], 'fixture-host-stop')
            self.assertEqual(env['DESKHAND_MUSE_SHARED_ACTIONS'], '1')
            self.assertNotIn('MUSEBRIDGE_TOKEN', env)
            self.assertNotIn('PYTHONPATH', env)
            self.assertNotIn('PYTHONHOME', env)

    def test_request_refuses_stale_or_unowned_process_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MuseBridgeRuntime(Path(folder) / 'runtime', folder)
            manager.home.mkdir(parents=True)
            manager.process = Mock(pid=42)
            manager.process.poll.return_value = None
            manager.client = Mock()
            (manager.home / 'runtime.json').write_text(json.dumps({'pid': 41, 'operator_port': 8766}))
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                manager.local_action('ping_muse')
            manager.client.open.assert_not_called()

    def test_owned_fixture_process_confirms_stop_via_request(self):
        # B2-D (100.030): MuseBridgeRuntime.stop_all() was removed
        # (zero production callers); the vendor stop_all action is
        # exercised here through _request() directly instead.
        source = Path(__file__).resolve().parent.parent / 'vendor' / 'jettpad'
        fixture = source / 'backend' / 'tests' / 'fixture_owned_bridge.py'
        with tempfile.TemporaryDirectory() as folder:
            manager = MuseBridgeRuntime(Path(folder) / 'runtime', source, sys.executable)
            manager.home.mkdir(parents=True)
            config = {'token': 'fixture-token-' + 'x' * 40}
            (manager.home / 'musebridge.config.json').write_text(json.dumps(config), encoding='utf-8')
            manager._command = lambda *args: [sys.executable, '-B', str(fixture)]
            try:
                with patch('deskhand_musebridge_runtime._make_kill_job', return_value=None):
                    state = manager.start()
                    process = manager.process
                    self.assertFalse(state['enabled'])
                    self.assertTrue(manager._request('stop_all')['result']['stopped'])
                    manager.close()
                    self.assertIsNotNone(process.poll())
                    self.assertFalse((manager.home / 'runtime.json').exists())
            finally:
                manager.close()


    def test_request_refuses_stale_runtime_even_if_pid_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MuseBridgeRuntime(Path(folder) / 'runtime', folder)
            manager.home.mkdir(parents=True)
            manager.process = Mock(pid=42)
            manager.process.poll.return_value = None
            manager.started_after = 100
            manager.client = Mock()
            (manager.home / 'runtime.json').write_text(json.dumps({'pid': 42, 'operator_port': 8766, 'started_at': 99}))
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                manager.local_action('ping_muse')
            manager.client.open.assert_not_called()
