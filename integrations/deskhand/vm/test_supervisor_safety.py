import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vm_tunnel_supervise as supervisor


class SupervisorSafetyTest(unittest.TestCase):
    def test_policy_describes_fail_closed_wedged_pid_recovery(self):
        config = json.loads(
            Path(supervisor.__file__).with_name('vm_tunnel_supervision.json').read_text())
        policy = config['restart_policy']
        self.assertIn('is not signaled', policy)
        self.assertIn('operator review', policy)
        self.assertNotIn('SIGTERM', policy)

    def test_supervisor_log_rotation_keeps_bounded_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'supervisor.log'
            path.write_bytes(b'a' * (supervisor.SUPERVISOR_LOG_MAX_BYTES + 1))
            first = path.with_name(path.name + '.1')
            second = path.with_name(path.name + '.2')
            first.write_text('previous')
            second.write_text('oldest')

            supervisor._rotate_log(path)

            self.assertFalse(path.exists())
            self.assertEqual(first.stat().st_size, supervisor.SUPERVISOR_LOG_MAX_BYTES + 1)
            self.assertEqual(second.read_text(), 'previous')

    def test_stale_live_pid_is_not_signalled_or_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pid').write_text('12345')
            (root / 'heartbeat').write_text(json.dumps({'last_poll_at': '2020-01-01T00:00:00+00:00'}))
            config = root / 'config.json'
            config.write_text(json.dumps({'work_dir': str(root), 'heartbeat_path': str(root / 'heartbeat'),
                'pid_path': str(root / 'pid'), 'supervisor_log': str(root / 'log'),
                'loop_command': ['unused'], 'heartbeat_max_age_secs': 180, 'wedge_max_age_secs': 300}))
            with patch.object(sys, 'argv', ['supervisor', '--supervision', str(config)]), \
                 patch.object(supervisor.os, 'kill') as kill, \
                 patch.object(supervisor.subprocess, 'Popen') as start:
                self.assertEqual(supervisor.main(), 1)
                kill.assert_called_once_with(12345, 0)
                start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
