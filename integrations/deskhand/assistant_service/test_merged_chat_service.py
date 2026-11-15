import json
import tempfile
import time
import threading
import unittest
import uuid
from pathlib import Path

from deskhand_service import ChatService
from deskhand_codex import CodexChat
from deskhand_dispatch import Request, Route, StopConfirmed


class ServiceTests(unittest.TestCase):

    def test_delayed_submission_from_before_stop_is_not_admitted_after_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                route = service.handle({"op": "catalog"})["routes"][0]
                ticket = service.dispatcher.admission_ticket()
                service.handle({"op": "stop_chat"})
                service.handle({"op": "resume_chat_local"})
                command = {"op": "submit", "id": str(uuid.uuid4()), "text": "delayed",
                           **{k: route[k] for k in ("assistant", "model", "account", "effort")}}
                with self.assertRaisesRegex(RuntimeError, "cancelled by a stop request"):
                    service.handle(command, submission_ticket=ticket)
                self.assertEqual(service.handle({"op": "history"})["turns"], [])
            finally:
                service.close()

    def test_chat_stop_never_claims_desktop_stop_and_local_resume_reopens_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                route = service.handle({"op": "catalog"})["routes"][0]
                result = service.handle({"op": "stop_chat"})
                self.assertFalse(result["all_ai_stopped"])
                self.assertIn("not fully confirmed", result["status"].lower())
                self.assertEqual(result["deskhand_stop"], "confirmed")
                self.assertEqual(result["musebridge_stop"], "not_enabled")
                request = {"op": "submit", "id": str(uuid.uuid4()), "text": "after stop",
                           **{k: route[k] for k in ("assistant", "model", "account", "effort")}}
                with self.assertRaises(RuntimeError): service.handle(request)
                service.handle({"op": "resume_chat_local"})
                self.assertEqual(service.handle(request)["id"], request["id"])
            finally:
                service.close()

    def test_shared_history_and_duplicate_submit(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                route = service.handle({"op": "catalog"})["routes"][0]
                request = {"op": "submit", "id": str(uuid.uuid4()), "text": "shared message",
                           **{k: route[k] for k in ("assistant", "model", "account", "effort")}}
                service.handle(request)
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    row = service.handle({"op": "get", "id": request["id"]})
                    if row["status"] == "completed":
                        break
                    time.sleep(.01)
                self.assertEqual(row["reply"], "Fixture reply: shared message")
                self.assertEqual(service.handle(request)["status"], "completed")
                self.assertEqual(len(service.handle({"op": "history"})["turns"]), 1)
                with self.assertRaises(ValueError):
                    service.handle({**request, "text": "changed"})
            finally:
                service.close()

    def test_unconfigured_route_does_not_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                service.handle({"op": "catalog"})
                with self.assertRaises(ValueError):
                    service.handle({"op": "submit", "id": str(uuid.uuid4()), "text": "hello",
                                    "assistant": "codex", "model": "other-model", "account": "default", "effort": "high"})
                self.assertEqual(service.handle({"op": "history"})["turns"], [])
            finally:
                service.close()


    def test_chat_stop_confirms_a_cancelled_active_turn_and_reports_muse_stop_removed(self):
        started = threading.Event()
        stop_requested = threading.Event()

        class Adapter:
            def __call__(self, request):
                started.set()
                if not stop_requested.wait(2):
                    raise TimeoutError("fixture stop was not requested")
                raise StopConfirmed("fixture turn cancelled")

            def request_stop(self):
                stop_requested.set()

        with tempfile.TemporaryDirectory() as folder:
            muse = type("Muse", (), {
                "relay": Adapter(),
                "close": lambda self: None,
            })()
            service = ChatService(folder, fixture=True, muse=muse)
            try:
                route = next(Route.of(row) for row in service.handle({"op": "catalog"})["routes"]
                             if row["assistant"] == "muse")
                turn = str(uuid.uuid4())
                service.handle({"op": "submit", "id": turn, "text": "fixture",
                                "assistant": route.assistant, "model": route.model,
                                "account": route.account, "effort": route.effort})
                self.assertTrue(started.wait(2))
                result = service.handle({"op": "stop_chat"})
                self.assertEqual(result["deskhand_stop"], "confirmed")
                # STOP removed: no Muse stop exists to confirm. The
                # Muse leg reports not-enabled with stop_removed True;
                # all_ai_stopped stays False because the MuseBridge
                # stop it used to require no longer exists.
                self.assertEqual(result["musebridge_stop"], "not_enabled")
                self.assertTrue(result["stop_removed"])
                self.assertFalse(result["all_ai_stopped"])
                self.assertEqual(service.box.get(turn)["status"], "cancelled")
            finally:
                service.close()

    def test_chat_stop_keeps_active_uncertain_turn_unconfirmed(self):
        started, stop_requested, release = threading.Event(), threading.Event(), threading.Event()

        class Adapter:
            def __call__(self, request):
                started.set()
                stop_requested.wait(2)
                release.wait(2)
                raise RuntimeError("fixture delivery uncertain")

            def request_stop(self):
                stop_requested.set()

        with tempfile.TemporaryDirectory() as folder:
            muse = type("Muse", (), {
                "relay": Adapter(),
                "close": lambda self: None,
            })()
            service = ChatService(folder, fixture=True, muse=muse)
            stop_results = []
            try:
                route = next(Route.of(row) for row in service.handle({"op": "catalog"})["routes"]
                             if row["assistant"] == "muse")
                turn = str(uuid.uuid4())
                service.handle({"op": "submit", "id": turn, "text": "fixture uncertain",
                                "assistant": route.assistant, "model": route.model,
                                "account": route.account, "effort": route.effort})
                self.assertTrue(started.wait(2))
                stopper = threading.Thread(target=lambda: stop_results.append(service.handle({"op": "stop_chat"})))
                stopper.start()
                self.assertTrue(stop_requested.wait(2))
                release.set()
                stopper.join(3)
                self.assertFalse(stopper.is_alive())
                self.assertEqual(service.box.get(turn)["status"], "unknown")
                self.assertEqual(stop_results[0]["deskhand_stop"], "unconfirmed")
                self.assertEqual(stop_results[0]["musebridge_stop"], "not_enabled")
                self.assertTrue(stop_results[0]["stop_removed"])
                self.assertFalse(stop_results[0]["all_ai_stopped"])
            finally:
                release.set()
                service.close()


class CodexTests(unittest.TestCase):
    def test_stop_retries_when_turn_registry_is_not_ready(self):
        attempts = []
        class FakeRpc:
            def __init__(self, home): pass
            def call(self, method, params):
                if method == "thread/start": return {"thread": {"id": "thread"}}
                if method == "turn/start":
                    adapter.request_stop()
                    return {"turn": {"id": "turn"}}
                if method == "turn/interrupt":
                    attempts.append(params)
                    if len(attempts) == 1: raise RuntimeError("no active turn to interrupt")
                    return {}
                raise AssertionError(method)
            def receive(self, deadline):
                if len(attempts) == 1:
                    time.sleep(.21)
                    raise TimeoutError()
                return {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "interrupted"}}}
            def close(self): pass
        route = Route("codex", "fixture-model", "default", "high")
        with tempfile.TemporaryDirectory() as folder:
            adapter = CodexChat(route, Path(folder), rpc_factory=FakeRpc)
            with self.assertRaises(StopConfirmed): adapter(Request("one", "first", route))
            self.assertEqual(len(attempts), 2)

    def test_stop_requests_correlated_interrupt_and_requires_terminal_confirmation(self):
        calls = []
        class FakeRpc:
            def __init__(self, home): pass
            def call(self, method, params):
                calls.append((method, params))
                if method == "thread/start": return {"thread": {"id": "thread"}}
                if method == "turn/start":
                    adapter.request_stop()
                    return {"turn": {"id": "turn"}}
                if method == "turn/interrupt": return {}
                raise AssertionError(method)
            def receive(self, deadline):
                return {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "interrupted"}}}
            def close(self): pass
        route = Route("codex", "fixture-model", "default", "high")
        with tempfile.TemporaryDirectory() as folder:
            adapter = CodexChat(route, Path(folder), rpc_factory=FakeRpc)
            with self.assertRaises(StopConfirmed):
                adapter(Request("one", "first", route))
            self.assertIn(("turn/interrupt", {"threadId": "thread", "turnId": "turn"}), calls)
            self.assertFalse(adapter.stop_requested.is_set())

    def test_thread_resume_and_correlated_final_message(self):
        calls = []
        class FakeRpc:
            def __init__(self, home):
                self.events = iter([
                    {"method": "item/completed", "params": {"threadId": "other-thread", "turnId": "turn", "item": {"id": "bad", "type": "agentMessage", "text": "wrong reply"}}},
                    {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn", "item": {"id": "answer", "type": "agentMessage", "text": "fixture answer"}}},
                    {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}}])
            def call(self, method, params):
                calls.append((method, params))
                if method.startswith("thread/"):
                    return {"thread": {"id": "thread"}, "model": "fixture-model"}
                if method == "turn/start":
                    # Batch 1, v100.003: the adapter now reuses ONE warm
                    # CodexRpc across turns, so the fake must model a
                    # server that emits events per turn (like the real
                    # reader thread does), not a one-shot iterator that
                    # only survives a fresh process per call.
                    self.events = iter([
                        {"method": "item/completed", "params": {"threadId": "other-thread", "turnId": "turn", "item": {"id": "bad", "type": "agentMessage", "text": "wrong reply"}}},
                        {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn", "item": {"id": "answer", "type": "agentMessage", "text": "fixture answer"}}},
                        {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}}])
                return {"turn": {"id": "turn"}}
            def receive(self, deadline):
                return next(self.events)
            def close(self):
                pass
        route = Route("codex", "fixture-model", "default", "high")
        with tempfile.TemporaryDirectory() as folder:
            adapter = CodexChat(route, Path(folder), rpc_factory=FakeRpc)
            first = adapter(Request("one", "first", route))
            second = adapter(Request("two", "second", route))
            self.assertEqual(first.text, "fixture answer")
            self.assertEqual(second.turn_id, "two")
            self.assertEqual([method for method, _ in calls], ["thread/start", "turn/start", "thread/resume", "turn/start"])
            self.assertEqual(calls[-1][1]["effort"], "high")
            self.assertEqual(calls[-1][1]["model"], "fixture-model")
