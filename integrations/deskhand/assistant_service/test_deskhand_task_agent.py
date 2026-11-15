"""Mock tests for deskhand_task_agent.py (laptop-agent fallback client).

Runs entirely in the VM against a local fake agent HTTP server. No PC,
no real token, no network beyond loopback. The test token is a dummy
value that never leaves this file.

Run: python3 test_deskhand_task_agent.py
"""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deskhand_task_agent import (  # noqa: E402
    TaskAgentAuthError,
    TaskAgentClient,
    TaskAgentDenied,
    TaskAgentError,
    TaskAgentMalformedResponse,
    TaskAgentNotEnabled,
    TaskAgentUnavailable,
)

TEST_TOKEN = "test-token-1234567890abcdefghij"
REMOTE_ENABLED = {"value": True}
SEEN_AUTH = []
SEEN_PATHS = []
# Fake /screenshot shape. "real" is the shape the actual laptop agent
# returns (data_b64 -- the bridge/gateway convention). "legacy" is the
# png_base64 shape the old fake used (and what the live reader looked
# for on 2026-11-08, producing the empty-image symptom). "empty" and
# "missing" reproduce a capture that yields dimensions but no image.
SCREENSHOT_MODE = {"value": "real"}
# B2-D H14: when True, /exec answers 200 with a non-JSON body.
MALFORMED_MODE = {"value": False}
# Minimal 1x1 PNG bytes, base64-encoded (valid PNG magic, decodes).
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class FakeAgent(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        return self.headers.get("Authorization", "") == "Bearer " + TEST_TOKEN

    def do_GET(self):
        SEEN_PATHS.append(("GET", self.path))
        if self.path == "/health":
            self._send(200, {"ok": True, "server": "laptop-agent-v2",
                             "remote_control_enabled": REMOTE_ENABLED["value"]})
            return
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path == "/screenshot":
            mode = SCREENSHOT_MODE["value"]
            if mode == "real":
                self._send(200, {"format": "png", "width": 640,
                                 "height": 360, "data_b64": TINY_PNG_B64})
            elif mode == "legacy":
                self._send(200, {"format": "png", "width": 640,
                                 "height": 360, "png_base64": TINY_PNG_B64})
            elif mode == "empty":
                self._send(200, {"format": "png", "width": 640,
                                 "height": 360, "data_b64": ""})
            else:  # "missing"
                self._send(200, {"format": "png", "width": 640,
                                 "height": 360})
            return
        self._send(404, {"error": "unknown endpoint"})

    def do_POST(self):
        SEEN_PATHS.append(("POST", self.path))
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/exec":
            if MALFORMED_MODE["value"]:
                raw = b"this is not json {"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            cmd = data.get("command", "")
            if "deskhand_task_agent_open.ps1" in cmd:
                self._send(200, {"stdout": "CIM ReturnValue=0 PID=5932\n"
                                           "RUNNING PID=5932 title=[Untitled - Notepad]\n",
                                 "stderr": "", "exit_code": 0})
            else:
                self._send(200, {"stdout": "ran: " + cmd, "stderr": "",
                                 "exit_code": 0})
            return
        if self.path == "/files/read":
            self._send(200, {"path": data.get("path"), "content": "hello"})
            return
        if self.path == "/files/write":
            self._send(200, {"path": data.get("path"), "bytes_written": 5})
            return
        if self.path == "/files/search":
            self._send(200, {"results": []})
            return
        self._send(404, {"error": "unknown endpoint"})


def start_fake_agent():
    srv = HTTPServer(("127.0.0.1", 0), FakeAgent)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def make_client(base_url, grant=True, log=None):
    d = tempfile.mkdtemp()
    cfg = os.path.join(d, "agent_config.json")
    with open(cfg, "w") as f:
        json.dump({"token": TEST_TOKEN, "host": "127.0.0.1",
                   "port": 8899, "remote_control_enabled": True}, f)
    logs = []
    client = TaskAgentClient(
        task_agent_dir=d, base_url=base_url, token_source=cfg,
        deskhand_grant_on=(lambda: grant) if isinstance(grant, bool) else grant,
        log=(log if log is not None else logs.append))
    client._test_logs = logs
    return client


PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print("PASS " + name)
    except Exception as e:  # noqa: BLE001
        FAILED.append(name)
        print("FAIL %s: %s: %s" % (name, type(e).__name__, e))


def main():
    srv, base = start_fake_agent()
    try:
        # 1. health (no auth) + token sent on authed calls
        def t_health():
            c = make_client(base)
            h = c.health()
            assert h["ok"] is True and h["remote_control_enabled"] is True
            c.exec("echo hi")
            assert ("GET", "/health") in SEEN_PATHS
        check("health + token auth", t_health)

        # 2. token never appears in logs or errors
        def t_token_never_leaks():
            c = make_client(base)
            c.exec("echo hi")
            c.files_read("C:\\x.txt")
            blob = "\n".join(c._test_logs)
            assert TEST_TOKEN not in blob, "token leaked into logs"
            try:
                bad = TaskAgentClient(task_agent_dir="/nonexistent",
                                      base_url=base, token_source="/nonexistent/agent_config.json",
                                      deskhand_grant_on=True)
                bad.reload_token()
                raise AssertionError("expected TaskAgentError")
            except TaskAgentError as e:
                assert TEST_TOKEN not in str(e)
        check("token never in logs/errors", t_token_never_leaks)

        # 3. Deskhand grant off -> denied
        def t_grant_off():
            c = make_client(base, grant=False)
            try:
                c.exec("echo hi")
                raise AssertionError("expected TaskAgentDenied")
            except TaskAgentDenied:
                pass
        check("deskhand grant off denies", t_grant_off)

        # 4. agent remote_control_enabled off -> not enabled
        def t_agent_flag_off():
            REMOTE_ENABLED["value"] = False
            try:
                c = make_client(base)
                try:
                    c.exec("echo hi")
                    raise AssertionError("expected TaskAgentNotEnabled")
                except TaskAgentNotEnabled:
                    pass
            finally:
                REMOTE_ENABLED["value"] = True
        check("agent flag off denies", t_agent_flag_off)

        # 5. exec / files / screenshot through run_action
        def t_run_action():
            c = make_client(base)
            r = c.run_action({"kind": "exec", "command": "echo hi"})
            assert r["ok"] and r["result"]["exit_code"] == 0
            r = c.run_action({"kind": "files_read", "path": "C:\\x.txt"})
            assert r["result"]["content"] == "hello"
            r = c.run_action({"kind": "screenshot"})
            assert r["result"]["width"] == 640
            assert r["result"]["height"] == 360
        check("run_action exec/files/screenshot", t_run_action)

        # 11. screenshot: real data_b64 shape is normalized (both
        # fields populated, byte count reported)
        def t_screenshot_real_shape():
            SCREENSHOT_MODE["value"] = "real"
            try:
                c = make_client(base)
                shot = c.screenshot()
                assert shot["data_b64"] == TINY_PNG_B64
                assert shot["png_base64"] == TINY_PNG_B64
                assert shot["bytes"] > 0
                assert shot["width"] == 640 and shot["height"] == 360
            finally:
                SCREENSHOT_MODE["value"] = "real"
        check("screenshot real data_b64 shape normalized", t_screenshot_real_shape)

        # 12. screenshot: legacy png_base64 shape is normalized too
        # (this is the shape the live reader expected on 2026-11-08)
        def t_screenshot_legacy_shape():
            SCREENSHOT_MODE["value"] = "legacy"
            try:
                c = make_client(base)
                shot = c.screenshot()
                assert shot["data_b64"] == TINY_PNG_B64
                assert shot["png_base64"] == TINY_PNG_B64
            finally:
                SCREENSHOT_MODE["value"] = "real"
        check("screenshot legacy png_base64 shape normalized", t_screenshot_legacy_shape)

        # 13. screenshot: dimensions with empty/missing image data is
        # an honest error, never a hollow success
        def t_screenshot_empty_rejected():
            c = make_client(base)
            for mode in ("empty", "missing"):
                SCREENSHOT_MODE["value"] = mode
                try:
                    c.screenshot()
                    raise AssertionError(
                        "expected TaskAgentError for mode " + mode)
                except TaskAgentError as e:
                    assert "no image data" in str(e)
            SCREENSHOT_MODE["value"] = "real"
        check("screenshot empty/missing image data rejected", t_screenshot_empty_rejected)

        # 6. open_notepad via the CIM .ps1 path
        def t_open_notepad():
            c = make_client(base)
            r = c.run_action({"kind": "open_notepad"})
            assert r["ok"] and r["result"]["pid"] == 5932
            assert r["result"]["title"] == "Untitled - Notepad"
        check("open_notepad (CIM launch + verify)", t_open_notepad)

        # 7. type/click kinds are rejected (stay with the gateway worker)
        def t_type_rejected():
            c = make_client(base)
            for kind in ("type", "click", "key", "desktop_type",
                         "desktop_click", "ui_invoke"):
                try:
                    c.run_action({"kind": kind, "text": "x"})
                    raise AssertionError("expected rejection for " + kind)
                except TaskAgentError as e:
                    assert "gateway worker" in str(e)
        check("type/click rejected", t_type_rejected)

        # 8. forbidden characters in exe/args rejected (no injection)
        def t_forbidden():
            c = make_client(base)
            for bad in ("notepad.exe; rm", "calc.exe | x", "a`b"):
                try:
                    c.open_app(bad)
                    raise AssertionError("expected ValueError for " + bad)
                except ValueError:
                    pass
        check("forbidden chars rejected", t_forbidden)

        # 9. agent unreachable -> TaskAgentUnavailable (clean, no hang)
        def t_unreachable():
            c = make_client("http://127.0.0.1:1", grant=True)
            try:
                c.health()
                raise AssertionError("expected TaskAgentUnavailable")
            except TaskAgentUnavailable:
                pass
        check("unreachable agent -> clean error", t_unreachable)

        # 14. malformed 200 on an acting call -> TaskAgentMalformedResponse
        # (B2-D H14): never a bare ValueError, so the op classifies it
        # "uncertain" (may have executed) instead of "failed" (retry).
        def t_malformed_200_is_uncertain_not_failed():
            MALFORMED_MODE["value"] = True
            try:
                c = make_client(base)
                try:
                    c.exec("echo hi")
                    raise AssertionError("expected TaskAgentMalformedResponse")
                except TaskAgentMalformedResponse as e:
                    assert "may have executed" in str(e)
                    assert isinstance(e, TaskAgentError)
                try:
                    c.exec("echo hi")
                    raise AssertionError("expected TaskAgentMalformedResponse")
                except TaskAgentMalformedResponse:
                    pass
                except ValueError:
                    raise AssertionError(
                        "malformed 200 must not raise bare ValueError")
            finally:
                MALFORMED_MODE["value"] = False
        check("malformed 200 -> uncertain, never bare ValueError", t_malformed_200_is_uncertain_not_failed)

        # 10. wrong token -> 401 -> TaskAgentAuthError
        def t_bad_token():
            d = tempfile.mkdtemp()
            cfg = os.path.join(d, "agent_config.json")
            with open(cfg, "w") as f:
                json.dump({"token": "wrong-token", "remote_control_enabled": True}, f)
            c = TaskAgentClient(task_agent_dir=d, base_url=base,
                                token_source=cfg, deskhand_grant_on=True)
            try:
                c.exec("echo hi")
                raise AssertionError("expected TaskAgentAuthError")
            except TaskAgentAuthError:
                pass
        check("wrong token -> auth error", t_bad_token)
    finally:
        srv.shutdown()
    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
