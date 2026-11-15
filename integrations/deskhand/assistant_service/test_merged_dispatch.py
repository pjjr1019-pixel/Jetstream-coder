import tempfile
import unittest
import uuid
from pathlib import Path

from deskhand_dispatch import Dispatcher, QueueOwner, Reply, Route, StopConfirmed
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

    def test_only_confirmed_interruption_marks_active_turn_cancelled(self):
        def interrupted(_):
            raise StopConfirmed("backend interruption observed")
        with Dispatcher(self.box, {self.route: interrupted}) as dispatcher:
            self.assertEqual(dispatcher.dispatch_one()["status"], "cancelled")

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

    def test_cancel_unstarted_blocks_new_submissions_and_cancels_queue(self):
        with Dispatcher(self.box, {}) as dispatcher:
            self.assertEqual(dispatcher.cancel_unstarted(), [self.id])
            self.assertEqual(self.box.get(self.id)["status"], "cancelled")
            self.assertIsNone(dispatcher.dispatch_one())
            with self.assertRaises(RuntimeError):
                dispatcher.submit(str(uuid.uuid4()), "new", **vars(self.route))

    def test_preflight_started_before_stop_cannot_submit_after_resume(self):
        with Dispatcher(self.box, {}) as dispatcher:
            ticket = dispatcher.admission_ticket()
            dispatcher.request_chat_stop()
            dispatcher.resume_chat()
            with self.assertRaisesRegex(RuntimeError, "cancelled by a stop request"):
                dispatcher.submit(str(uuid.uuid4()), "old request", admission_ticket=ticket, **vars(self.route))

    def test_requested_stop_survives_restart_until_local_resume(self):
        with Dispatcher(self.box, {}) as first:
            stop = first.request_chat_stop()
        with Dispatcher(self.box, {}) as second:
            self.assertTrue(second.admission_state()["stopped"])
            self.assertEqual(second.admission_state()["epoch"], stop["admission_epoch"])
            with self.assertRaises(RuntimeError): second.admission_ticket()
            second.resume_chat()
            self.assertFalse(second.admission_state()["stopped"])
        with Dispatcher(self.box, {}) as third:
            self.assertFalse(third.admission_state()["stopped"])

    def test_restart_recovers_older_stop_with_queued_work(self):
        self.box.set_admission_state(True, 7)
        with Dispatcher(self.box, {}) as dispatcher:
            self.assertEqual(self.box.get(self.id)["status"], "cancelled")
            dispatcher.resume_chat()
            self.assertIsNone(dispatcher.dispatch_one())

    def test_cancellation_does_not_wait_for_or_claim_to_stop_active_adapter(self):
        import threading
        started, release = threading.Event(), threading.Event()
        def adapter(request):
            started.set()
            release.wait(3)
            return Reply(request.turn_id, request.route, "finished")
        with Dispatcher(self.box, {self.route: adapter}) as dispatcher:
            worker = threading.Thread(target=dispatcher.dispatch_one)
            worker.start()
            self.assertTrue(started.wait(2))
            other = str(uuid.uuid4())
            dispatcher.submit(other, "queued", **vars(self.route))
            try:
                self.assertEqual(dispatcher.cancel_unstarted(), [other])
                self.assertEqual(self.box.get(self.id)["status"], "dispatching")
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.box.get(self.id)["status"], "completed")

    def test_chat_stop_requests_active_interrupt_without_claiming_confirmation(self):
        import threading
        started, interrupted = threading.Event(), threading.Event()
        class Adapter:
            def __call__(self, request):
                started.set()
                if not interrupted.wait(3):
                    raise TimeoutError("not interrupted")
                raise StopConfirmed("confirmed by fixture")
            def request_stop(self):
                interrupted.set()
        with Dispatcher(self.box, {self.route: Adapter()}) as dispatcher:
            worker = threading.Thread(target=dispatcher.dispatch_one)
            worker.start()
            self.assertTrue(started.wait(2))
            result = dispatcher.request_chat_stop()
            self.assertEqual(result["active_turn"], self.id)
            self.assertTrue(result["interrupt_requested"])
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.box.get(self.id)["status"], "cancelled")

    def test_active_interrupt_is_requested_before_slow_storage(self):
        import threading
        started, interrupted, storage_started, release_storage = (threading.Event() for _ in range(4))
        original = self.box.persist_stop_and_cancel
        class Adapter:
            def __call__(self, request):
                started.set()
                if not interrupted.wait(3): raise TimeoutError("no interrupt")
                raise StopConfirmed("confirmed")
            def request_stop(self): interrupted.set()
        def slow_store(epoch):
            storage_started.set()
            if not release_storage.wait(3): raise TimeoutError("storage fixture")
            return original(epoch)
        with Dispatcher(self.box, {self.route: Adapter()}) as dispatcher:
            worker = threading.Thread(target=dispatcher.dispatch_one)
            worker.start()
            self.assertTrue(started.wait(2))
            self.box.persist_stop_and_cancel = slow_store
            stopper = threading.Thread(target=dispatcher.request_chat_stop)
            stopper.start()
            try:
                self.assertTrue(storage_started.wait(2))
                self.assertTrue(interrupted.is_set())
            finally:
                release_storage.set()
                stopper.join(3)
                worker.join(3)
            self.assertFalse(stopper.is_alive())
            self.assertFalse(worker.is_alive())

    def test_new_turn_clears_stale_adapter_stop_before_becoming_active(self):
        import threading
        class Adapter:
            def __init__(self):
                self.stop = threading.Event()
                self.stop.set()  # A late stop belonging to the previous turn.
            def prepare_turn(self): self.stop.clear()
            def request_stop(self): self.stop.set()
            def __call__(self, request):
                if self.stop.is_set(): raise StopConfirmed("stale stop")
                return Reply(request.turn_id, request.route, "new turn")
        with Dispatcher(self.box, {self.route: Adapter()}) as dispatcher:
            self.assertEqual(dispatcher.dispatch_one()["reply"], "new turn")


if __name__ == "__main__":
    unittest.main()
