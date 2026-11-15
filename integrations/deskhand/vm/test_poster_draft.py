import json
import hashlib
import tempfile
import unittest
import subprocess
import sys
import os
from unittest.mock import patch
from pathlib import Path

from vm_tunnel_poll import PollLoop
from publish_reply import publish


class PosterDraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = PollLoop('http://localhost', 'fake', self.tmp.name, 2)

    def reply(self, name):
        path = self.loop.replies_dir / (name + '.json')
        path.write_text(json.dumps({'turn_id': name, 'reply': 'hello'}))
        return path

    def test_failed_first_post_does_not_block_second(self):
        first, second = self.reply('a'), self.reply('b')
        def send(method, path, payload):
            if payload['turn_id'] == 'a':
                raise ConnectionError('offline')
            return 200, {'turn_id': 'b', 'status': 'replied', 'stored_sha256': hashlib.sha256(b'hello').hexdigest()}
        self.loop._agent = send
        self.loop._post_replies()
        self.assertFalse(first.exists())
        self.assertTrue(list(self.loop.claims_dir.glob('*/a.json')))
        self.assertFalse(second.exists())
        self.assertFalse(list(self.loop.claims_dir.glob('*/b.json')))
        self.assertEqual(json.loads(self.loop.posted_turns_path.read_text()), ['b'])

    def test_unverified_receipts_and_temporary_files_are_retained(self):
        first = self.reply('a')
        temp = self.loop.replies_dir / '.tmp.json'
        temp.write_text('partial')
        self.loop._agent = lambda *args: (200, {'turn_id': 'wrong', 'status': 'replied'})
        self.loop._post_replies()
        self.assertTrue(list(self.loop.claims_dir.glob('*/a.json')))
        self.assertTrue(temp.exists())

    def test_failed_receipt_record_keeps_reply(self):
        first = self.reply('a')
        self.loop._agent = lambda *args: (200, {'turn_id': 'a', 'status': 'replied', 'stored_sha256': hashlib.sha256(b'hello').hexdigest()})
        self.loop._record_posted = lambda *_: (_ for _ in ()).throw(OSError('disk full'))
        self.loop._post_replies()
        self.assertTrue(list(self.loop.claims_dir.glob('*/a.json')))

    def test_failed_claim_sync_never_posts_reply(self):
        self.reply('a')
        sent = []
        self.loop._agent = lambda *args: sent.append(args)
        with patch.object(self.loop, '_sync_dir', side_effect=OSError('sync failed')):
            self.loop._post_replies()
        self.assertEqual(sent, [])
        self.assertTrue(list(self.loop.claims_dir.glob('*/a.json')))

    def test_conflicting_stored_reply_is_not_deleted(self):
        first = self.reply('a')
        self.loop._agent = lambda *args: (200, {'turn_id': 'a', 'status': 'replied',
                                                'stored_sha256': hashlib.sha256(b'other').hexdigest()})
        self.loop._post_replies()
        self.assertTrue(list(self.loop.claims_dir.glob('*/a.json')))

    def test_replacement_during_post_is_preserved_and_processed(self):
        self.reply('a')
        def send(method, path, payload):
            publish(self.loop.replies_dir, {'turn_id': 'a', 'reply': 'replacement'})
            return 200, {'turn_id': 'a', 'status': 'replied',
                         'stored_sha256': hashlib.sha256(b'hello').hexdigest()}
        self.loop._agent = send
        self.loop._post_replies()
        self.assertEqual(json.loads((self.loop.replies_dir / 'a.json').read_text())['reply'], 'replacement')
        self.loop._agent = lambda *args: self.fail('conflicting late reply must not be reposted')
        self.loop._post_replies()
        conflicts = list((Path(self.tmp.name) / 'replies-conflicts').glob('*/a.json'))
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(json.loads(conflicts[0].read_text())['reply'], 'replacement')
        self.assertFalse(list(self.loop.claims_dir.glob('*/a.json')))

    def test_publication_conflict_never_overwrites_first(self):
        self.assertEqual(publish(self.loop.replies_dir, {'turn_id': 'a', 'reply': 'first'}), 'published')
        self.assertEqual(publish(self.loop.replies_dir, {'turn_id': 'a', 'reply': 'second'}), 'conflict')
        self.assertEqual(json.loads((self.loop.replies_dir / 'a.json').read_text())['reply'], 'first')
        conflicts = list((Path(self.tmp.name) / 'replies-conflicts').glob('*/a.json'))
        self.assertEqual(json.loads(conflicts[0].read_text())['reply'], 'second')

    def test_restart_recovers_claim_after_acceptance(self):
        self.reply('a')
        self.loop._agent = lambda *args: (200, {'turn_id': 'a', 'status': 'replied', 'stored_sha256': hashlib.sha256(b'hello').hexdigest()})
        with patch.object(self.loop, '_record_posted', side_effect=OSError('disk full')):
            self.loop._post_replies()
        restarted = PollLoop('http://localhost', 'fake', self.tmp.name, 2)
        restarted._agent = self.loop._agent
        restarted._post_replies()
        self.assertFalse(list(restarted.claims_dir.glob('*/a.json')))

    def test_previously_posted_conflict_is_retained(self):
        self.loop.posted_turn_ids = ['a']
        self.reply('a')
        self.loop._agent = lambda *args: (200, {'turn_id': 'a', 'status': 'replied',
                                                'stored_sha256': hashlib.sha256(b'other').hexdigest()})
        self.loop._post_replies()
        self.assertTrue(list(self.loop.claims_dir.glob('*/a.json')))

    def test_durable_receipt_quarantines_conflict_without_reposting(self):
        marker = self.loop.completed_dir / 'a.json'
        marker.write_text(json.dumps({'turn_id': 'a', 'kind': 'reply',
                                      'stored_sha256': hashlib.sha256(b'first').hexdigest()}))
        path = self.reply('a')
        self.loop._agent = lambda *args: self.fail('conflicting reply must not be reposted')
        self.loop._post_replies()
        self.assertFalse(path.exists())
        conflicts = list((Path(self.tmp.name) / 'replies-conflicts').glob('*/a.json'))
        self.assertEqual(json.loads(conflicts[0].read_text())['reply'], 'hello')
        # A fresh process consults the same durable marker, too.
        self.reply('a')
        restarted = PollLoop('http://localhost', 'fake', self.tmp.name, 2)
        restarted._agent = self.loop._agent
        restarted._post_replies()
        self.assertEqual(len(list((Path(self.tmp.name) / 'replies-conflicts').glob('*/a.json'))), 2)

    def test_orphan_claim_is_retried_after_restart(self):
        claim = self.loop.claims_dir / 'test-claim'
        claim.mkdir()
        (claim / 'a.json').write_text(json.dumps({'turn_id': 'a', 'reply': 'hello'}))
        self.loop._agent = lambda *args: (200, {'turn_id': 'a', 'status': 'replied',
                                                'stored_sha256': hashlib.sha256(b'hello').hexdigest()})
        self.loop._post_replies()
        self.assertFalse(claim.exists())

    def test_confirmed_reply_creates_completion_marker_before_cleanup(self):
        self.reply('a')
        self.loop._agent = lambda *args: (200, {'turn_id': 'a', 'status': 'replied',
                                                'stored_sha256': hashlib.sha256(b'hello').hexdigest()})
        self.loop._post_replies()
        marker = self.loop.completed_dir / 'a.json'
        self.assertEqual(json.loads(marker.read_text())['stored_sha256'], hashlib.sha256(b'hello').hexdigest())
        self.assertFalse(list(self.loop.claims_dir.glob('*/a.json')))

    @unittest.skipUnless(os.name == 'posix', 'requires Linux flock')
    def test_exclusive_claim_blocks_a_second_loop(self):
        first = PollLoop('http://localhost', 'fake', self.tmp.name, 2)
        second = PollLoop('http://localhost', 'fake', self.tmp.name, 2)
        self.assertTrue(first.claim_pid())
        try:
            self.assertFalse(second.claim_pid())
        finally:
            first.release_pid()
        self.assertTrue(second.claim_pid())
        second.release_pid()


if __name__ == '__main__':
    unittest.main()
