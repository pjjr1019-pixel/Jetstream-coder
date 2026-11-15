import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tunnel_operator as operator


class OperatorWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, name, content='{"secret": "PRIVATE-TEXT"}'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_status_is_metadata_only_and_never_signals(self):
        path = self.file('turns-claimed/abc.json')
        os.utime(path, (1, 1))
        self.file('tunnel-poll.pid', '12345')
        with patch('os.kill', side_effect=AssertionError('must not signal')):
            status = operator.collect_status(self.root, now=1000)
        self.assertEqual(status['turns'][0]['state'], 'outcome_uncertain')
        self.assertEqual(status['process']['pid_state'], 'identity_unverified')
        self.assertNotIn('PRIVATE-TEXT', json.dumps(status))
        self.assertTrue(path.exists())

    def test_receipt_conflict_and_cleanup_evidence(self):
        self.file('reply-completed/abc.json')
        self.file('replies-conflicts/random/abc.json')
        self.file('inbox-cleanup/other/abc.json')
        row = operator.collect_status(self.root)['turns'][0]
        self.assertEqual(row['state'], 'review_required')
        self.assertIn('completed', row['evidence'])
        self.assertIn('cleanup_held', row['evidence'])

    def test_completion_needs_correlated_digest_and_kind(self):
        marker = self.file('reply-completed/abc.json', '{}')
        self.assertEqual(operator.collect_status(self.root)['turns'][0]['state'], 'review_required')
        marker.write_text(json.dumps({'turn_id': 'other', 'kind': 'reply', 'stored_sha256': 'a' * 64}))
        self.assertEqual(operator.collect_status(self.root)['turns'][0]['state'], 'review_required')
        marker.write_text(json.dumps({'turn_id': 'abc', 'kind': 'reply', 'stored_sha256': 'a' * 64}))
        self.assertEqual(operator.collect_status(self.root)['turns'][0]['state'], 'reply_accepted')

    def test_record_review_is_append_only_and_does_not_retry(self):
        turn = self.file('turns-claimed/abc.json')
        before = turn.read_bytes()
        self.assertRaises(ValueError, operator.record_review, self.root, 'abc', 'unknown', 'other')
        self.assertFalse((self.root / 'operator-reviews').exists())
        operator.record_review(self.root, 'abc', 'not_sent_after_review', 'abc')
        operator.record_review(self.root, 'abc', 'waiting', 'abc')
        files = list((self.root / 'operator-reviews').glob('*.json'))
        self.assertEqual(len(files), 2)
        self.assertEqual(turn.read_bytes(), before)
        self.assertNotIn('PRIVATE-TEXT', ''.join(p.read_text() for p in files))
        self.assertFalse((self.root / 'turns').exists())

    def test_no_evidence_or_invalid_id_refused(self):
        with self.assertRaises(ValueError):
            operator.record_review(self.root, '../outside', 'unknown', '../outside')
        with self.assertRaises(ValueError):
            operator.record_review(self.root, 'absent', 'unknown', 'absent')
        self.assertFalse((self.root / 'operator-reviews').exists())

    def test_unrecognized_ids_are_counted_without_exposing_filenames(self):
        self.file('turns/bad name.json')
        status = operator.collect_status(self.root)
        self.assertEqual(status['unrecognized_file_counts']['inbox'], 1)
        self.assertEqual(status['turns'], [])
        self.assertNotIn('bad name', json.dumps(status))

    def test_completion_and_fingerprint_metadata_are_reported_as_retained(self):
        self.file('reply-completed/abc.json')
        self.file('turn-fingerprints/abc.json')
        retention = operator.collect_status(self.root)['retention']
        self.assertEqual(retention['completion_markers'], 1)
        self.assertEqual(retention['turn_fingerprints'], 1)
        self.assertIn('retained indefinitely', retention['policy'])


if __name__ == '__main__':
    unittest.main()
