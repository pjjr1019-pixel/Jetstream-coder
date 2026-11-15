import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
if TEST_DIR.name == 'tests' and TEST_DIR.parent.name == 'work':
    VM_DIR = TEST_DIR.parent / 'vm'
else:
    VM_DIR = TEST_DIR


def _required_vm_file(name):
    path = VM_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f'required staged VM file {name!r} was not found at {path}; '
            'the hook tests must run beside the staged VM directory or '
            'under candidate/work/tests with candidate/work/vm present')
    return path


HOOK = _required_vm_file('tunnel-turn-consumer.sh')
PUBLISH_HELPER = _required_vm_file('publish_reply.py')
CLEANUP_HELPER = _required_vm_file('inbox_cleanup.py')
BASH = shutil.which('bash')


def _bash_usable():
    if not BASH:
        return False
    try:
        return subprocess.run([BASH, '-c', 'exit 0'], capture_output=True, timeout=2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(os.name == 'posix' and _bash_usable(), 'requires Linux and a usable bash shell')
class ConsumerHookTest(unittest.TestCase):
    def run_conflict(self, root, block_conflict_storage=False):
        state = root / 'state'
        for name in ('turns', 'turns-claimed', 'replies'):
            (state / name).mkdir(parents=True, exist_ok=True)
        if block_conflict_storage:
            (state / 'replies-conflicts').write_text('blocked', encoding='utf-8')
        turn_id = 'ping-race'
        (state / 'turns' / (turn_id + '.json')).write_text(
            json.dumps({'turn_id': turn_id, 'text': 'ping'}), encoding='utf-8')
        wake_log = root / 'wake.log'
        event_log = root / 'events.log'
        runtime = root / 'runtime.sh'
        runtime.write_text(
            'log() { printf "%s\\n" "$*" >> "$TEST_EVENT_LOG"; }\n'
            'silent() { :; }\n'
            'wake() { printf "%s\\n" "$*" >> "$TEST_WAKE_LOG"; }\n',
            encoding='utf-8')
        claim_helper = root / 'claim_fixture.py'
        claim_helper.write_text(
            'import json, shutil, sys\n'
            'from pathlib import Path\n'
            'source, dest = map(Path, sys.argv[1:3])\n'
            'shutil.copy2(source, dest)\n'
            'replies = dest.parent.parent / "replies"\n'
            'payload = {"turn_id": dest.stem, "reply": "worker reply"}\n'
            '(replies / (dest.stem + ".json")).write_text(json.dumps(payload), encoding="utf-8")\n',
            encoding='utf-8')
        env = os.environ.copy()
        env.update({
            'HATCH_HOOK_RUNTIME': str(runtime),
            'TUNNEL_STATE_DIR': str(state),
            'TUNNEL_PUBLISH_HELPER': str(PUBLISH_HELPER),
            'TUNNEL_CLAIM_HELPER': str(claim_helper),
            'TUNNEL_CLEANUP_HELPER': str(CLEANUP_HELPER),
            'TEST_EVENT_LOG': str(event_log),
            'TEST_WAKE_LOG': str(wake_log),
            'HATCH_HOOK_DRY_RUN': '0',
        })
        result = subprocess.run(
            [BASH, str(HOOK)], env=env, capture_output=True, text=True, timeout=30)
        return result, state, wake_log, event_log, turn_id

    def test_fast_path_conflict_is_held_without_worker_wake(self):
        with tempfile.TemporaryDirectory() as directory:
            result, state, wake_log, event_log, turn_id = self.run_conflict(Path(directory))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(wake_log.exists())
            self.assertTrue((state / 'turns-claimed' / (turn_id + '.json')).exists())
            self.assertEqual(
                json.loads((state / 'replies' / (turn_id + '.json')).read_text())['reply'],
                'worker reply')
            self.assertIn(
                'fast-path reply conflict requires review',
                event_log.read_text(encoding='utf-8'))
            conflicts = list((state / 'replies-conflicts').glob('*/' + turn_id + '.json'))
            self.assertEqual(len(conflicts), 1)

    def test_conflict_storage_failure_still_does_not_wake_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            result, state, wake_log, event_log, turn_id = self.run_conflict(
                Path(directory), block_conflict_storage=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(wake_log.exists())
            self.assertTrue((state / 'turns-claimed' / (turn_id + '.json')).exists())
            self.assertEqual(
                json.loads((state / 'replies' / (turn_id + '.json')).read_text())['reply'],
                'worker reply')
            self.assertIn(
                'fast-path reply conflict requires review',
                event_log.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
