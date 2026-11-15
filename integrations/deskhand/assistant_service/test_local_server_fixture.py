"""Temporary loopback model fixture for the Windows service integration test."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        if self.path != '/api/tags':
            self.send_error(404); return
        body = json.dumps({'models': [{'name': 'fixture-tool-model'}]}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request.get("model") != "fixture-tool-model":
            self.send_error(400); return
        messages = request.get("messages", [])
        if request.get("tools") and not any(message.get("role") == "tool" for message in messages):
            message = {"role": "assistant", "tool_calls": [{"function": {"name": "desktop.list", "arguments": {"folder_token": None}}}]}
        elif request.get("tools"):
            tool = next(message for message in messages if message.get("role") == "tool")
            message = {"role": "assistant", "content": "Found fixture.txt" if "fixture.txt" in tool.get("content", "") else "Fixture missing"}
        else:
            message = {"role": "assistant", "content": "Fixture chat reply"}
        body = json.dumps({"model": "fixture-tool-model", "done": True, "message": message}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 0), Handler)
    print(server.server_port, flush=True)
    server.serve_forever()
