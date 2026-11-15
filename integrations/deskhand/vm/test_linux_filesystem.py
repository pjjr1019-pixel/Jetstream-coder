"""Linux-only staged filesystem integration tests; never contacts live services."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import claim_turn
import inbox_cleanup
import publish_reply
from vm_tunnel_poll import PollLoop


@unittest.skipUnless(os.name == 'posix', 'requires Linux hard links and fsync')
class LinuxFilesystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.loop = PollLoop('http://127.0.0.1', 'fake', self.root, 2)
        self.claims = self.root / 'turns-claimed'
        self.claims.mkdir()

    def test_complete_turn_claim_reply_and_inbox_cleanup(self):
        source = self.loop.turns_dir / 'turn.json'
        source.write_text(json.dumps({'turn_id': 'turn', 'text': 'synthetic'}), encoding='utf-8')
        claim = self.claims / 'turn.json'
        claim_turn.claim(source, claim)
        self.assertEqual(publish_reply.publish(self.loop.replies_dir,
                         {'turn_id': 'turn', 'reply': 'synthetic reply'}), 'published')
        digest = hashlib.sha256(b'synthetic reply').hexdigest()
        self.loop._agent = lambda *a: (200, {'turn_id': 'turn', 'status': 'replied', 'stored_sha256': digest})
        self.loop._post_replies()
        self.assertTrue((self.loop.completed_dir / 'turn.json').exists())
        self.assertEqual(inbox_cleanup.cleanup(self.root, 'turn'), 'removed')
        self.assertFalse(source.exists())
        self.assertTrue(claim.exists())  # active workers may still be using it
        self.assertEqual(inbox_cleanup.cleanup(self.root, 'turn', 'turns-claimed'), 'review_required')

    def test_conflicting_reply_and_replaced_input_are_retained(self):
        source = self.loop.turns_dir / 'turn.json'
        source.write_text('{"turn_id":"turn","text":"original"}')
        claim_turn.claim(source, self.claims / 'turn.json')
        publish_reply.publish(self.loop.replies_dir, {'turn_id': 'turn', 'reply': 'first'})
        self.assertEqual(publish_reply.publish(self.loop.replies_dir,
                         {'turn_id': 'turn', 'reply': 'different'}), 'conflict')
        digest = hashlib.sha256(b'first').hexdigest()
        self.loop._agent = lambda *a: (200, {'turn_id': 'turn', 'status': 'replied', 'stored_sha256': digest})
        self.loop._post_replies()
        replacement = source.with_name('replacement.tmp')
        replacement.write_text('{"turn_id":"turn","text":"other"}')
        os.replace(replacement, source)
        self.assertEqual(inbox_cleanup.cleanup(self.root, 'turn'), 'conflict')
        self.assertTrue(list((self.root / 'inbox-cleanup').glob('*/turn.json')))
        self.assertTrue(list((self.root / 'replies-conflicts').glob('*/turn.json')))


if __name__ == '__main__':
    unittest.main()
