import tempfile
import unittest
import uuid
from pathlib import Path

from deskhand_dispatch import Dispatcher, NotDispatched, QueueOwner, Reply, Route
from deskhand_mailbox import Mailbox


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Mailbox(Path(self.tmp.name) / "turns.sqlite")
        self.route = Route("codex", "fixture-model", "fixture-account", "high")
        self.id = str(uuid.uuid4())
        self.box.submit(self.id, "hello", **vars(self.route))

    def test_exact_route_reply(self):
        def adapter(request):
            self.assertEqual(request.route, self.route)
            self.assertEqual(request.text, "hello")
            return Reply(request.turn_id, request.route, "fixture reply")
        with Dispatcher(self.box, {self.route: adapter}) as dispatcher:
            self.assertEqual(dispatcher.dispatch_one()["reply"], "fixture reply")

    def test_no_fallback_to_other_account(self):
        other = Route("codex", "fixture-model", "different-account", "high")
        with Dispatcher(self.box, {other: lambda _: self.fail("wrong account")}) as dispatcher:
            self.assertEqual(dispatcher.dispatch_one()["status"], "failed")

    def test_mismatched_reply_is_unknown_and_never_retried(self):
        with Dispatcher(self.box, {self.route: lambda r: Reply("wrong-id", r.route, "reply")}) as dispatcher:
            with self.assertRaises(ValueError):
                dispatcher.dispatch_one()
            self.assertEqual(self.box.get(self.id)["status"], "unknown")
            self.assertIsNone(dispatcher.dispatch_one())

    def test_transport_failure_is_unknown(self):
        def broken(_):
            raise TimeoutError("destination might already have received the request")
        with Dispatcher(self.box, {self.route: broken}) as dispatcher:
            with self.assertRaises(TimeoutError):
                dispatcher.dispatch_one()
            self.assertEqual(self.box.get(self.id)["status"], "unknown")

    def test_confirmed_unsent_message_is_failed_not_uncertain(self):
        def unavailable(_):
            raise NotDispatched('Message was not sent: Muse is disconnected')
        with Dispatcher(self.box, {self.route: unavailable}) as dispatcher:
            row = dispatcher.dispatch_one()
            self.assertEqual(row['status'], 'failed')
            self.assertIn('Message was not sent', row['error'])

    def test_relay_ack_follows_durable_reply_and_failure_does_not_replay(self):
        box = self.box
        case = self
        class Adapter:
            def __call__(self, request):
                return Reply(request.turn_id, request.route, 'saved reply')
            def reply_saved(self, request):
                case.assertEqual(box.get(request.turn_id)['status'], 'completed')
                raise RuntimeError('fixture cleanup failure')
        with Dispatcher(self.box, {self.route: Adapter()}) as dispatcher:
            with self.assertLogs(level='ERROR'):
                row = dispatcher.dispatch_one()
            self.assertEqual(row['status'], 'completed')
            self.assertIsNone(dispatcher.dispatch_one())

    def test_exclusive_owner_and_recovery(self):
        self.box.claim()
        with QueueOwner(self.box.path):
            with self.assertRaises(OSError):
                with Dispatcher(self.box, {}):
                    self.fail("second owner admitted")
            self.assertEqual(self.box.get(self.id)["status"], "dispatching")
        with Dispatcher(self.box, {}):
            self.assertEqual(self.box.get(self.id)["status"], "unknown")

    def test_stop_blocks_admission_and_closed_dispatcher_cannot_claim(self):
        dispatcher = Dispatcher(self.box, {})
        with dispatcher:
            dispatcher.stop()
            self.assertIsNone(dispatcher.dispatch_one())
            self.assertEqual(self.box.get(self.id)["status"], "queued")
        with self.assertRaises(RuntimeError):
            dispatcher.dispatch_one()


if __name__ == "__main__":
    unittest.main()
