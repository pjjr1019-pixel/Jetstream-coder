import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deskhand_dispatch import Request, Route
from deskhand_local import LocalChat


class LocalTests(unittest.TestCase):
    def test_external_or_credentialed_origins_rejected(self):
        route = Route("local", "fixture-model", "", "")
        for endpoint in ("http://example.com", "http://localhost", "http://user:secret@127.0.0.1", "http://127.0.0.1/path", "https://127.0.0.1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                LocalChat(route, endpoint)

    def test_does_not_silently_drop_account_or_effort(self):
        for route in (Route("local", "model", "account", ""), Route("local", "model", "", "high")):
            with self.assertRaises(ValueError):
                LocalChat(route, "http://127.0.0.1:11434")

    def test_http_fixture_preserves_request_and_rejects_model_mismatch(self):
        received = []
        model = ["fixture-model"]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                payload = json.dumps({"model": model[0], "done": True,
                                      "message": {"role": "assistant", "content": "fixture reply"}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            route = Route("local", "fixture-model", "", "")
            adapter = LocalChat(route, f"http://127.0.0.1:{server.server_port}")
            request = Request("fixture-turn", "hello", route)
            self.assertEqual(adapter(request).text, "fixture reply")
            self.assertEqual(received[0], ("/api/chat", {"model": "fixture-model", "stream": False,
                                                       "keep_alive": "30m",
                                                       "messages": [{"role": "user", "content": "hello"}]}))
            model[0] = "different-model"
            with self.assertRaises(ValueError):
                adapter(request)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()
