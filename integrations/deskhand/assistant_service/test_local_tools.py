import unittest
import uuid

from deskhand_local_tools import LocalToolAdapter, LocalToolTransport
from deskhand_tool_loop import ActionResult, run_task
from deskhand_local import LocalChat
from deskhand_dispatch import Route
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading


class LocalToolTests(unittest.TestCase):
    def test_screenshot_is_forwarded_as_untrusted_image(self):
        import base64
        task, turn = str(uuid.uuid4()), str(uuid.uuid4())
        observed = []
        def model(messages, tools):
            observed.append(messages)
            if len(observed) == 1:
                return {"role": "assistant", "tool_calls": [{"function": {"name": "window.screenshot", "arguments": {"window_token": "fixture"}}}]}
            return {"role": "assistant", "content": "Saw the fixture"}
        adapter = LocalToolAdapter(task_id=task, turn_id=turn, prompt="inspect", transport=model)
        encoded = base64.b64encode(b"\xff\xd8fixture").decode()
        result = run_task(task_id=task, turn_id=turn, adapter=adapter,
            execute=lambda p, a: ActionResult(a.action_id, "completed", json.dumps({"error": None, "data": {"jpegBase64": encoded, "width": 30, "height": 20, "observationToken": "fixture-token"}})), stopped=lambda: False)
        self.assertEqual(result.text, "Saw the fixture")
        self.assertEqual(observed[-1][-1]["images"], [encoded])
        self.assertIn("untrusted", observed[-1][-1]["content"])
        self.assertNotIn(encoded, observed[-1][-2]["content"])
        self.assertIn("fixture-token", observed[-1][-2]["content"])

    def test_native_call_gets_host_result_then_final(self):
        task, turn = str(uuid.uuid4()), str(uuid.uuid4())
        observed = []
        def model(messages, tools):
            observed.append(messages)
            if len(observed) == 1:
                return {"role": "assistant", "tool_calls": [{"function": {"name": "desktop.list", "arguments": {"folder_token": None}}}]}
            return {"role": "assistant", "content": "Done"}
        adapter = LocalToolAdapter(task_id=task, turn_id=turn, prompt="List files", transport=model)
        final = run_task(task_id=task, turn_id=turn, adapter=adapter,
                         execute=lambda p, a: ActionResult(a.action_id, "completed", "fixture"), stopped=lambda: False)
        self.assertEqual(final.text, "Done")
        self.assertEqual(observed[1][-1]["role"], "tool")
        self.assertIn("fixture", observed[1][-1]["content"])

    def test_unknown_tool_does_not_become_action(self):
        adapter = LocalToolAdapter(task_id=str(uuid.uuid4()), turn_id=str(uuid.uuid4()), prompt="test",
                                   transport=lambda *_: {"role": "assistant", "tool_calls": [{"function": {"name": "shell.run", "arguments": {}}}]})
        with self.assertRaises(ValueError): adapter(())

    def test_host_json_result_is_presented_as_structured_data(self):
        task, turn = str(uuid.uuid4()), str(uuid.uuid4())
        observed = []
        def model(messages, tools):
            observed.append(messages)
            if len(observed) == 1:
                return {"role": "assistant", "tool_calls": [{"function": {"name": "window.observe", "arguments": {}}}]}
            return {"role": "assistant", "content": "Observed"}
        adapter = LocalToolAdapter(task_id=task, turn_id=turn, prompt="Observe", transport=model)
        run_task(task_id=task, turn_id=turn, adapter=adapter,
                 execute=lambda p, a: ActionResult(a.action_id, "completed", json.dumps({"error": None, "data": [{"token": "exact-token"}]})), stopped=lambda: False)
        self.assertEqual(json.loads(observed[1][-1]["content"])["output"]["data"][0]["token"], "exact-token")

    def test_loopback_transport_sends_tool_schema_and_checks_model(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.server.received = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                body = json.dumps({"model": "fixture", "done": True, "message": {"role": "assistant", "content": "ok"}}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            route = Route("local", "fixture", "", "")
            chat = LocalChat(route, f"http://127.0.0.1:{server.server_port}")
            response = LocalToolTransport(chat)([{"role": "user", "content": "hi"}], [{"type": "function"}])
            self.assertEqual(response["content"], "ok")
            self.assertEqual(server.received["tools"], [{"type": "function"}])
            self.assertEqual(server.received["options"]["num_ctx"], 16384)
        finally:
            server.shutdown(); server.server_close(); thread.join()
