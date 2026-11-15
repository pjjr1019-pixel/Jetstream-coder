import json
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from deskhand_service import ChatService
from deskhand_codex import CodexChat
from deskhand_dispatch import Request, Route


class ServiceTests(unittest.TestCase):
    def test_agent_submission_requires_provider_readiness(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                service.handle({'op': 'local_configure', 'model': 'fixture', 'endpoint': 'http://127.0.0.1:11434'})
                service.handle({'op': 'catalog'})
                service.host_rpc = Mock()
                request = {'op': 'submit', 'id': str(uuid.uuid4()), 'text': 'ping',
                           'assistant': 'local', 'model': 'fixture', 'account': '', 'effort': '',
                           'agent_task_id': str(uuid.uuid4())}
                original = service.handle
                def handle(command, *args, **kwargs):
                    if command.get('op') == 'access_validate':
                        return {'ready': False}
                    return original(command, *args, **kwargs)
                with patch.object(service, 'handle', side_effect=handle):
                    with self.assertRaisesRegex(RuntimeError, 'connection is not ready'):
                        service.handle(request)
                self.assertEqual(service.box.recent(), [])
            finally:
                service.close()

    def test_removed_provider_is_not_advertised_or_admitted(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                routes = service.handle({"op": "catalog"})["routes"]
                self.assertTrue(routes)
                self.assertTrue(all(route["assistant"] in {"codex", "local"} for route in routes))
                for op in ("muse_open", "muse_connect", "muse_binding", "muse_recover_pending"):
                    with self.assertRaises(ValueError):
                        service.handle({"op": op})
                with self.assertRaises(ValueError):
                    service.handle({"op": "submit", "id": str(uuid.uuid4()), "text": "hello",
                                    "assistant": "muse", "model": "muse", "account": "deskhand-edge", "effort": ""})
                self.assertEqual(service.box.recent(), [])
            finally:
                service.close()

    def test_openai_local_configuration_survives_restart_and_stays_loopback_only(self):
        with tempfile.TemporaryDirectory() as folder:
            first = ChatService(folder, fixture=True)
            try:
                first.handle({"op": "local_configure", "model": "fixture", "endpoint": "openai:http://127.0.0.1:1234"})
                before = (Path(folder) / "local-models.json").read_text()
                with self.assertRaises(ValueError):
                    first.handle({"op": "local_configure", "model": "other", "endpoint": "openai:http://example.com"})
                self.assertEqual((Path(folder) / "local-models.json").read_text(), before)
            finally:
                first.close()
            second = ChatService(folder, fixture=True)
            try:
                route = next(iter(second.local_routes))
                self.assertEqual(type(second.local_routes[route]).__name__, "OpenAILocalChat")
                self.assertEqual(second.local_origins[route], "openai:http://127.0.0.1:1234")
            finally:
                second.close()

    def test_managed_local_selection_survives_restart_without_starting_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            first = ChatService(folder, fixture=True)
            try:
                first.handle({"op": "local_configure", "model": "fixture", "endpoint": "managed"})
                self.assertIsNone(first.local_runtime.process)
            finally:
                first.close()
            second = ChatService(folder, fixture=True)
            try:
                self.assertTrue(any(r["assistant"] == "local" and r["model"] == "fixture" for r in second.handle({"op": "catalog"})["routes"]))
                self.assertEqual(next(iter(second.local_origins.values())), "managed")
                self.assertIsNone(second.local_runtime.process)
            finally:
                second.close()

    def test_local_configuration_survives_restart_and_rejects_external_server(self):
        with tempfile.TemporaryDirectory() as folder:
            first = ChatService(folder, fixture=True)
            try:
                first.handle({"op": "local_configure", "model": "fixture", "endpoint": "http://127.0.0.1:11434"})
                before = (Path(folder) / "local-models.json").read_text()
                with self.assertRaises(ValueError):
                    first.handle({"op": "local_configure", "model": "other", "endpoint": "http://example.com"})
                self.assertEqual((Path(folder) / "local-models.json").read_text(), before)
            finally:
                first.close()
            second = ChatService(folder, fixture=True)
            try:
                catalog = second.handle({"op": "catalog"})
                self.assertTrue(any(r["assistant"] == "local" and r["model"] == "fixture" for r in catalog["routes"]))
            finally:
                second.close()

    def test_invalid_local_configuration_is_not_partially_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local-models.json"
            path.write_text(json.dumps({"version": 1, "models": [
                {"model": "valid", "endpoint": "http://127.0.0.1:11434"},
                {"model": "invalid", "endpoint": "http://example.com"}]}))
            service = ChatService(folder, fixture=True)
            try:
                catalog = service.handle({"op": "catalog"})
                self.assertIn("could not be loaded", catalog["error"])
                self.assertFalse(any(r["assistant"] == "local" for r in catalog["routes"]))
            finally:
                service.close()

    def test_local_model_task_returns_host_result_to_same_model(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        task, turn = str(uuid.uuid4()), str(uuid.uuid4())
        class Host:
            def __init__(self): self.calls = []
            def call(self, op, payload):
                self.calls.append((op, payload))
                if op == "task.check": return {"active": True}
                if op == "action.execute":
                    return {"actionId": payload["action_id"], "status": "completed", "data": {"items": ["fixture.txt"]}}
                return {"ended": True}
            def close(self): pass
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.requests.append(body)
                if len(self.server.requests) == 1:
                    message = {"role": "assistant", "tool_calls": [{"function": {"name": "desktop.list", "arguments": {"folder_token": None}}}]}
                else:
                    message = {"role": "assistant", "content": "Found fixture.txt"}
                response = json.dumps({"model": "fixture", "done": True, "message": message}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(response)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                host = Host(); service = ChatService(folder, fixture=True, host_rpc=host)
                try:
                    service.handle({"op": "local_configure", "model": "fixture", "endpoint": f"http://127.0.0.1:{server.server_port}"})
                    route = Route("local", "fixture", "", "")
                    self.assertTrue(any(r["model"] == "fixture" and r["assistant"] == "local" for r in service.handle({"op": "catalog"})["routes"]))
                    result = service.dispatcher.adapters[route](Request(turn, "Find fixture", route, task))
                    self.assertEqual(result.text, "Found fixture.txt")
                    self.assertIn("fixture.txt", server.requests[1]["messages"][-1]["content"])
                    self.assertEqual(host.calls[-1][1]["outcome"], "completed")
                finally:
                    service.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()




    def test_shared_agent_timeout_records_unknown_without_retry(self):
        task, turn = str(uuid.uuid4()), str(uuid.uuid4())
        class Host:
            def __init__(self): self.calls = []
            def call(self, op, payload):
                self.calls.append((op, payload))
                if op == "task.check": return {"active": True}
                if op == "action.execute": raise TimeoutError("host action timed out")
                return {"ended": True}
            def close(self): pass
        class Adapter:
            def __init__(self): self.calls = 0
            def __call__(self, results):
                self.calls += 1
                from deskhand_tool_protocol import parse_tool_request
                return parse_tool_request(json.dumps({"version": 1, "type": "deskhand.actions", "task_id": task,
                    "turn_id": turn, "request_id": str(uuid.uuid4()), "actions": [{"action_id": str(uuid.uuid4()),
                    "name": "desktop.list", "arguments": {"folder_token": None}}]}), task_id=task, turn_id=turn)
        with tempfile.TemporaryDirectory() as folder:
            host = Host(); service = ChatService(folder, fixture=True, host_rpc=host)
            try:
                adapter = Adapter()
                with self.assertRaises(TimeoutError):
                    service._agent_turn(Request(turn, "test", Route("local", "fixture", "", ""), task), adapter)
                self.assertEqual(adapter.calls, 1)
                self.assertEqual(sum(op == "action.execute" for op, _ in host.calls), 1)
                self.assertEqual(host.calls[-1][1]["outcome"], "timeout_unknown")
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


class CodexTests(unittest.TestCase):
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
