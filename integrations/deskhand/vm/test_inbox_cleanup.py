import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import inbox_cleanup


class InboxCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        for name in ('turns', 'turn-fingerprints', 'reply-completed'):
            (self.state / name).mkdir()
        self.raw = b'{"turn_id":"turn","text":"private"}'
        self.source = self.state / 'turns' / 'turn.json'
        self.source.write_bytes(self.raw)
        (self.state / 'reply-completed' / 'turn.json').write_text(json.dumps({
            'turn_id': 'turn', 'kind': 'reply', 'stored_sha256': hashlib.sha256(b'answer').hexdigest()}))
        (self.state / 'turn-fingerprints' / 'turn.json').write_text(json.dumps({
            'turn_id': 'turn', 'sha256': hashlib.sha256(self.raw).hexdigest()}))
        self.sync = patch.object(inbox_cleanup, 'sync_dir')
        self.sync.start()
        self.addCleanup(self.sync.stop)

    def test_completed_matching_copy_is_removed(self):
        self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn'), 'removed')
        self.assertFalse(self.source.exists())
        self.assertTrue((self.state / 'reply-completed' / 'turn.json').exists())

    def test_replaced_inbox_is_preserved(self):
        replacement = b'{"turn_id":"turn","text":"different"}'
        self.source.write_bytes(replacement)
        self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn'), 'conflict')
        captures = list((self.state / 'inbox-cleanup').glob('*/*.json'))
        self.assertEqual(captures[0].read_bytes(), replacement)
        self.assertEqual(inbox_cleanup.sweep(self.state), [('turn', 'conflict')])

    def test_unproven_legacy_copy_is_retained(self):
        (self.state / 'turn-fingerprints' / 'turn.json').unlink()
        self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn'), 'unverified')
        self.assertTrue(self.source.exists())

    def test_completed_claim_copy_is_removed_only_when_matching(self):
        claimed = self.state / 'turns-claimed'
        claimed.mkdir()
        path = claimed / 'turn.json'
        path.write_bytes(self.raw)
        self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn', 'turns-claimed'), 'review_required')
        self.assertTrue(path.exists())
        self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn', 'turns-claimed', reviewed_claim=True), 'removed')
        self.assertFalse(path.exists())
        self.assertTrue(self.source.exists())

    def test_conflicting_claim_copy_remains_in_private_capture(self):
        claimed = self.state / 'turns-claimed'
        claimed.mkdir()
        (claimed / 'turn.json').write_bytes(b'different')
        self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn', 'turns-claimed', reviewed_claim=True), 'conflict')
        self.assertEqual(next((self.state / 'inbox-cleanup').glob('*/*.json')).read_bytes(), b'different')

    def test_crash_after_capture_is_recovered_by_sweep(self):
        capture = self.state / 'inbox-cleanup' / 'capture'
        capture.mkdir(parents=True)
        self.source.replace(capture / 'turn.json')
        self.assertEqual(inbox_cleanup.sweep(self.state), [('turn', 'removed')])
        self.assertFalse(capture.exists())

    def test_missing_completion_retains_plaintext(self):
        (self.state / 'reply-completed' / 'turn.json').unlink()
        self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn'), 'unverified')
        self.assertTrue(self.source.exists())

    def test_malformed_completion_never_authorizes_cleanup(self):
        (self.state / 'reply-completed' / 'turn.json').write_text(json.dumps({'turn_id': 'turn'}))
        self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn'), 'unverified')
        self.assertTrue(self.source.exists())

    def test_new_inbox_entry_after_capture_survives(self):
        replacement = b'{"turn_id":"turn","text":"new request"}'
        real_process = inbox_cleanup._process
        def publish_before_cleanup(state, owned):
            self.source.write_bytes(replacement)
            return real_process(state, owned)
        with patch.object(inbox_cleanup, '_process', side_effect=publish_before_cleanup):
            self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn'), 'removed')
        self.assertEqual(self.source.read_bytes(), replacement)

    def test_failed_sync_after_unlink_clears_empty_directory_on_sweep(self):
        def fail_after_unlink(path):
            path = Path(path)
            if path.parent.name == 'inbox-cleanup' and not list(path.iterdir()):
                raise OSError('sync failed after unlink')
        with patch.object(inbox_cleanup, 'sync_dir', side_effect=fail_after_unlink):
            self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn'), 'retained')
        self.assertEqual(inbox_cleanup.sweep(self.state), [])
        self.assertEqual(list((self.state / 'inbox-cleanup').iterdir()), [])

    def test_replacement_immediately_before_capture_is_not_deleted(self):
        replacement = b'{"turn_id":"turn","text":"new request"}'
        real_replace = os.replace
        def replace_after_publish(source, dest):
            staged = self.state / 'turns' / 'replacement.tmp'
            staged.write_bytes(replacement)
            real_replace(staged, self.source)
            real_replace(source, dest)
        with patch.object(inbox_cleanup.os, 'replace', side_effect=replace_after_publish):
            self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn'), 'conflict')
        self.assertEqual(next((self.state / 'inbox-cleanup').glob('*/*.json')).read_bytes(), replacement)

    def test_sync_failure_after_capture_is_recovered(self):
        def fail_after_capture(path):
            if Path(path) == self.state / 'turns':
                raise OSError('sync failed')
        with patch.object(inbox_cleanup, 'sync_dir', side_effect=fail_after_capture):
            self.assertEqual(inbox_cleanup.cleanup(self.state, 'turn'), 'retained')
        self.assertEqual(inbox_cleanup.sweep(self.state), [('turn', 'removed')])


if __name__ == '__main__':
    unittest.main()
