import json
import os
import tempfile
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from deskhand_dispatch import Request, Route, StopConfirmed
from deskhand_muse_relay import MuseRelay


class MuseRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inbox = Path(self.temp.name)
        self.turn, self.reply = str(uuid.uuid4()), str(uuid.uuid4())
        self.route = Route('muse', 'muse', '', '')
        self.request = Request(self.turn, 'fixture prompt', self.route)
        self.job = {'id': self.turn, 'reply_id': self.reply, 'attempt': 'attempt', 'state': 'sent'}
        self.mailbox = Mock()
        self.mailbox.enqueue.return_value = self.job
        self.mailbox.get.return_value = self.job
        self.relay = MuseRelay(self.mailbox, self.inbox, timeout=.15)

    def test_matches_receipt_and_acknowledges_only_after_save(self):
        (self.inbox / (self.reply + '.json')).write_text(json.dumps({'id': self.reply, 'from': 'jett', 'text': 'answer'}))
        result = self.relay(self.request)
        self.assertEqual((result.turn_id, result.text), (self.turn, 'answer'))
        self.mailbox.complete.assert_not_called()
        self.relay.reply_saved(self.request)
        self.mailbox.complete.assert_called_once_with(self.turn, 'attempt')

    def test_matches_durable_https_receipt_without_a_desktop_action(self):
        self.mailbox.https_reply.return_value = 'HTTPS answer'
        result = self.relay(self.request)
        self.assertEqual((result.turn_id, result.text), (self.turn, 'HTTPS answer'))
        self.mailbox.https_reply.assert_called_once_with(self.reply)
        self.mailbox.complete.assert_not_called()
        self.relay.reply_saved(self.request)
        self.mailbox.complete.assert_called_once_with(self.turn, 'attempt')

    def test_uncertain_delivery_is_not_retried(self):
        self.job['state'] = 'uncertain'
        with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
            self.relay(self.request)
        self.mailbox.enqueue.assert_called_once()
        self.mailbox.complete.assert_not_called()

    def test_invalid_reply_identity_is_refused(self):
        self.job['reply_id'] = '../other'
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.relay(self.request)

    def test_stop_does_not_claim_the_remote_turn_was_cancelled(self):
        self.mailbox.get.side_effect = lambda identity: (self.relay.request_stop() or self.job)
        self.mailbox.cancel_queued.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'may have started'):
            self.relay(self.request)

    def test_stop_before_enqueue_does_not_create_a_browser_job(self):
        self.relay.request_stop()
        with self.assertRaises(StopConfirmed):
            self.relay(self.request)
        self.mailbox.enqueue.assert_not_called()

    def test_disconnected_before_admission_does_not_enqueue(self):
        self.relay.ensure_ready = Mock(side_effect=RuntimeError('offline'))
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            self.relay(self.request)
        self.mailbox.enqueue.assert_not_called()

    def test_disconnect_after_send_is_uncertain_and_not_replayed(self):
        self.relay.ensure_ready = Mock(side_effect=[None, RuntimeError('offline')])
        self.mailbox.cancel_queued.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'may have started'):
            self.relay(self.request)
        self.mailbox.enqueue.assert_called_once()
        self.mailbox.complete.assert_not_called()

    def test_disconnect_cancels_unclaimed_turn(self):
        self.relay.ensure_ready = Mock(side_effect=[None, RuntimeError('offline')])
        self.mailbox.cancel_queued.return_value = True
        with self.assertRaises(StopConfirmed):
            self.relay(self.request)

    def test_real_copied_mailbox_correlates_and_finishes_one_turn(self):
        source = Path(__file__).resolve().parent.parent / 'vendor' / 'jettpad'
        sys.path.insert(0, str(source))
        try:
            from browser_mailbox import BrowserMailbox
            mailbox = BrowserMailbox(self.inbox / 'pad')
            try:
                job = mailbox.enqueue({'id': self.turn, 'text': 'fixture prompt', 'ts': 1})
                attempt = str(uuid.uuid4())
                with mailbox.db:
                    mailbox.db.execute("UPDATE jobs SET state='sent', attempt=? WHERE id=?", (attempt, self.turn))
                receipt = self.inbox / (job['reply_id'] + '.json')
                receipt.write_text(json.dumps({'id': job['reply_id'], 'from': 'jett', 'text': 'fixture answer'}))
                relay = MuseRelay(mailbox, self.inbox, timeout=.2)
                self.assertEqual(relay(self.request).text, 'fixture answer')
                self.assertEqual(mailbox.get(self.turn)['state'], 'sent')
                relay.reply_saved(self.request)
                self.assertEqual(mailbox.get(self.turn)['state'], 'complete')
                other = str(uuid.uuid4())
                mailbox.enqueue({'id': other, 'text': 'queued', 'ts': 2})
                self.assertTrue(mailbox.cancel_queued(other))
                self.assertEqual(mailbox.get(other)['state'], 'abandoned')
                self.assertFalse(mailbox.cancel_queued(other))
                interrupted = str(uuid.uuid4())
                mailbox.enqueue({'id': interrupted, 'text': 'interrupted', 'ts': 3})
                self.assertEqual(mailbox.abandon_queued_after_restart(), 1)
                self.assertEqual(mailbox.get(interrupted)['state'], 'abandoned')
            finally:
                mailbox.close()
        finally:
            sys.path.remove(str(source))

    def test_https_receipt_recovers_from_real_isolated_mailbox(self):
        source = Path(__file__).resolve().parent.parent / 'vendor' / 'jettpad'
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source / 'backend' / 'src'))
        try:
            from browser_mailbox import BrowserMailbox
            from musebridge.https_pad_reply import store_reply
            with patch.dict(os.environ, {'DESKHAND_JETT_RUNTIME': str(self.inbox)}):
                mailbox = BrowserMailbox(self.inbox / 'pad')
                try:
                    job = mailbox.enqueue({'id': self.turn, 'text': self.request.text, 'ts': 1})
                    attempt = str(uuid.uuid4())
                    with mailbox.db:
                        mailbox.db.execute("UPDATE jobs SET state='sent',attempt=? WHERE id=?", (attempt, self.turn))
                    store_reply(job['reply_id'], 'HTTPS fixture answer')
                    relay = MuseRelay(mailbox, self.inbox, timeout=.2)
                    self.assertEqual(relay(self.request).text, 'HTTPS fixture answer')
                    relay.reply_saved(self.request)
                    self.assertEqual(mailbox.get(self.turn)['state'], 'complete')
                finally:
                    mailbox.close()
        finally:
            sys.path.remove(str(source))
            sys.path.remove(str(source / 'backend' / 'src'))

    def test_stop_abandons_real_unclaimed_job(self):
        source = Path(__file__).resolve().parent.parent / 'vendor' / 'jettpad'
        sys.path.insert(0, str(source))
        try:
            from browser_mailbox import BrowserMailbox
            mailbox = BrowserMailbox(self.inbox / 'pad')
            try:
                relay = MuseRelay(mailbox, self.inbox, timeout=2)
                outcome = []
                def run():
                    try: relay(self.request)
                    except Exception as error: outcome.append(error)
                thread = threading.Thread(target=run)
                thread.start()
                for _ in range(100):
                    if mailbox.get(self.turn): break
                    time.sleep(.01)
                relay.request_stop()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(outcome[0], StopConfirmed)
                self.assertEqual(mailbox.get(self.turn)['state'], 'abandoned')
                self.assertIsNone(mailbox.claim(str(uuid.uuid4())))
            finally:
                mailbox.close()
        finally:
            sys.path.remove(str(source))

    def test_browser_send_fails_closed_without_host_stop_authority(self):
        source = Path(__file__).resolve().parent.parent / 'vendor' / 'jettpad'
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source / 'backend' / 'src'))
        try:
            from browser_mailbox import BrowserMailbox
            with patch.dict(os.environ, {'DESKHAND_JETT_RUNTIME': str(self.inbox)}, clear=False):
                os.environ.pop('DESKHAND_HOST_STOP_EVENT', None)
                mailbox = BrowserMailbox(self.inbox / 'pad')
                try:
                    mailbox.enqueue({'id': self.turn, 'text': 'fixture', 'ts': 1})
                    job = mailbox.claim(str(uuid.uuid4()))
                    send = Mock()
                    self.assertEqual(mailbox.send_guarded(job, send), {'state': 'blocked'})
                    send.assert_not_called()
                finally:
                    mailbox.close()
        finally:
            sys.path.remove(str(source))
            sys.path.remove(str(source / 'backend' / 'src'))
