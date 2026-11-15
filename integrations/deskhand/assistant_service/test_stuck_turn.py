"""Stuck 'dispatching' turns: watchdog auto-clear + honest STOP confirmation.

A plain chat turn waits on one Muse reply. When the reply path is dead
(gateway 401, worker disconnected, app funnel down) the relay waits its
full 600s bound, the single worker thread is blocked, and the chat shows
"Waiting for assistant..." the whole time. The watchdog interrupts a plain
turn after a short timeout; the dispatch thread still owns the journal
entry (unknown when delivery may have begun), so nothing is replayed.
A phone STOP on such a turn must also report confirmed once the chat is
unblocked, even though the turn ends 'unknown' rather than 'cancelled'.
"""
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from deskhand_dispatch import Dispatcher, Reply, Route
from deskhand_mailbox import Mailbox
from deskhand_service import ChatService


class BlockingAdapter:
    """Blocks until stopped, then fails the way a half-sent relay turn does."""

    def __init__(self):
        self.entered = threading.Event()
        self.stop_requested = threading.Event()

    def request_stop(self):
        self.stop_requested.set()

    def __call__(self, request):
        self.entered.set()
        self.stop_requested.wait(10)
        raise RuntimeError("Muse delivery may have started; check the conversation before retrying")


class UninterruptibleAdapter:
    """Blocks with no request_stop; the watchdog must leave it alone."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, request):
        self.entered.set()
        self.release.wait(10)
        return Reply(request.turn_id, request.route, "late reply")


class StuckTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Mailbox(Path(self.tmp.name) / "turns.sqlite")
        self.route = Route("codex", "fixture-model", "fixture-account", "high")

    def _run_dispatch(self, dispatcher):
        outcome = {}

        def target():
            try:
                dispatcher.dispatch_one()
            except Exception as error:  # noqa: BLE001 - recorded by the dispatcher
                outcome["error"] = error

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, outcome

    def test_interrupt_stale_marks_unknown_and_never_replays(self):
        turn = str(uuid.uuid4())
        self.box.submit(turn, "hello", **vars(self.route))
        adapter = BlockingAdapter()
        with Dispatcher(self.box, {self.route: adapter}) as dispatcher:
            thread, _ = self._run_dispatch(dispatcher)
            self.assertTrue(adapter.entered.wait(5))
            time.sleep(0.3)
            outcome = dispatcher.interrupt_stale(0.2)
            self.assertTrue(outcome["stale"])
            self.assertTrue(outcome["interrupt_requested"])
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(self.box.get(turn)["status"], "unknown")
            self.assertIsNone(dispatcher.dispatch_one())

    def test_interrupt_stale_ignores_fresh_turn(self):
        turn = str(uuid.uuid4())
        self.box.submit(turn, "hello", **vars(self.route))
        adapter = BlockingAdapter()
        with Dispatcher(self.box, {self.route: adapter}) as dispatcher:
            thread, _ = self._run_dispatch(dispatcher)
            self.assertTrue(adapter.entered.wait(5))
            outcome = dispatcher.interrupt_stale(60)
            self.assertFalse(outcome["stale"])
            self.assertTrue(thread.is_alive())
            adapter.request_stop()
            thread.join(5)
            self.assertEqual(self.box.get(turn)["status"], "unknown")

    def test_interrupt_stale_skips_agent_turns_by_default(self):
        turn, task = str(uuid.uuid4()), str(uuid.uuid4())
        self.box.submit(turn, "hello", **vars(self.route), agent_task_id=task)
        adapter = BlockingAdapter()
        with Dispatcher(self.box, {self.route: adapter}) as dispatcher:
            thread, _ = self._run_dispatch(dispatcher)
            self.assertTrue(adapter.entered.wait(5))
            time.sleep(0.2)
            outcome = dispatcher.interrupt_stale(0.1)
            self.assertTrue(outcome["stale"])
            self.assertEqual(outcome.get("skipped"), "agent_turn")
            self.assertFalse(outcome["interrupt_requested"])
            self.assertTrue(thread.is_alive())
            adapter.request_stop()
            thread.join(5)

    def test_interrupt_stale_reports_adapter_without_request_stop(self):
        turn = str(uuid.uuid4())
        self.box.submit(turn, "hello", **vars(self.route))
        adapter = UninterruptibleAdapter()
        with Dispatcher(self.box, {self.route: adapter}) as dispatcher:
            thread, _ = self._run_dispatch(dispatcher)
            self.assertTrue(adapter.entered.wait(5))
            time.sleep(0.2)
            outcome = dispatcher.interrupt_stale(0.1)
            self.assertTrue(outcome["stale"])
            self.assertFalse(outcome["interrupt_requested"])
            self.assertIn("cannot be interrupted", outcome.get("error", ""))
            self.assertTrue(thread.is_alive())
            adapter.release.set()
            thread.join(5)
            self.assertEqual(self.box.get(turn)["status"], "completed")

    def test_watchdog_auto_clears_stuck_plain_turn_and_chat_stays_usable(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True,
                                  stale_chat_timeout=0.3, watchdog_interval=0.05)
            try:
                # A local route's adapter is left alone by submit (unlike the
                # fixture codex route, which submit always re-registers).
                service.handle({"op": "local_configure", "model": "fixture",
                                "endpoint": "http://127.0.0.1:11434"})
                route = next(r for r in service.handle({"op": "catalog"})["routes"]
                             if r["assistant"] == "local")
                key = Route(route["assistant"], route["model"], route["account"], route["effort"])
                adapter = BlockingAdapter()
                service.dispatcher.adapters[key] = adapter
                turn = str(uuid.uuid4())
                service.handle({"op": "submit", "id": turn, "text": "stuck message",
                                **{k: route[k] for k in ("assistant", "model", "account", "effort")}})
                self.assertTrue(adapter.entered.wait(5))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if service.box.get(turn)["status"] == "unknown":
                        break
                    time.sleep(0.05)
                self.assertEqual(service.box.get(turn)["status"], "unknown")
                self.assertIn("may have started", service.box.get(turn)["error"])
                # The chat was never stopped by the watchdog: a new turn works.
                service.dispatcher.adapters[key] = lambda r: Reply(r.turn_id, r.route, "Fixture reply: next")
                nxt = str(uuid.uuid4())
                service.handle({"op": "submit", "id": nxt, "text": "next",
                                **{k: route[k] for k in ("assistant", "model", "account", "effort")}})
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if service.box.get(nxt)["status"] == "completed":
                        break
                    time.sleep(0.01)
                self.assertEqual(service.box.get(nxt)["status"], "completed")
            finally:
                service.close()

    def test_stop_chat_on_uncertain_turn_stays_unconfirmed_but_unblocks_chat(self):
        # Deliberate invariant (see test_chat_stop_keeps_active_uncertain_turn_unconfirmed):
        # a turn that ends 'unknown' may still have been delivered, so the
        # stop is honestly "unconfirmed". The chat itself must still be
        # unblocked: after a local resume, a new turn completes.
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                # A local route's adapter is left alone by submit (unlike the
                # fixture codex route, which submit always re-registers).
                service.handle({"op": "local_configure", "model": "fixture",
                                "endpoint": "http://127.0.0.1:11434"})
                route = next(r for r in service.handle({"op": "catalog"})["routes"]
                             if r["assistant"] == "local")
                key = Route(route["assistant"], route["model"], route["account"], route["effort"])
                adapter = BlockingAdapter()
                service.dispatcher.adapters[key] = adapter
                turn = str(uuid.uuid4())
                service.handle({"op": "submit", "id": turn, "text": "stuck message",
                                **{k: route[k] for k in ("assistant", "model", "account", "effort")}})
                self.assertTrue(adapter.entered.wait(5))
                result = service.handle({"op": "stop_chat"})
                self.assertEqual(result["deskhand_stop"], "unconfirmed")
                self.assertEqual(result["active_status"], "unknown")
                self.assertEqual(service.box.get(turn)["status"], "unknown")
                self.assertTrue(service.dispatcher.admission_state()["stopped"])
                self.assertTrue(service.dispatcher.wait_for_idle(5))
                service.handle({"op": "resume_chat_local"})
                service.dispatcher.adapters[key] = lambda r: Reply(r.turn_id, r.route, "Fixture reply: next")
                nxt = str(uuid.uuid4())
                service.handle({"op": "submit", "id": nxt, "text": "next",
                                **{k: route[k] for k in ("assistant", "model", "account", "effort")}})
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if service.box.get(nxt)["status"] == "completed":
                        break
                    time.sleep(0.01)
                self.assertEqual(service.box.get(nxt)["status"], "completed")
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
