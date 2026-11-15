#!/usr/bin/env python3
"""Headless tests for laptop_launcher.py (no Windows, no display
needed except the optional GUI smoke test, which uses xvfb).

Run:  python test_launcher.py
Exit 0 = all checks passed. Every check prints [PASS]/[FAIL] with
real evidence (values, codes, outputs), not just "ok".

Covers: config load/save round-trip, validation matrix, token
generator, Tailscale detect with no binary, health check against
a local stub, launcher backend start/stop of the REAL agent
(including the bad-config exit — the failure Phil already hit),
flag-off refusal, token redaction, token-free status text, and
the agent file being byte-identical to v2 (unchanged).
"""

import http.server
import secrets
import json
import os
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import laptop_launcher as L  # noqa: E402  (importing must NOT open a window)

# Test token: generated fresh at runtime, never a real
# credential and never written to any shipped file.
_TEST_TOKEN = secrets.token_urlsafe(32)

PASSES = []
FAILURES = []


def check(name, cond, evidence=""):
    if cond:
        PASSES.append(name)
        print(f"[PASS] {name} — {evidence}")
    else:
        FAILURES.append(name)
        print(f"[FAIL] {name} — {evidence}")


def good_config(**overrides):
    cfg = dict(L.DEFAULT_CONFIG)
    cfg["allowed_roots"] = []
    cfg["token"] = _TEST_TOKEN
    cfg["host"] = "127.0.0.1"
    cfg["remote_control_enabled"] = True
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------- config

def test_config_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg, err = L.load_config(base)
        check("config missing -> defaults + error",
              cfg["token"] == "CHANGE-ME" and err is not None
              and "not found" in err, f"err={err!r}")
        good = good_config()
        good["allowed_roots"] = ["C:\\test", "D:\\data"]
        good["mystery_key"] = "kept"
        L.save_config(good, base)
        reloaded, err2 = L.load_config(base)
        check("config save/load round-trip",
              err2 is None and reloaded["token"] == good["token"]
              and reloaded["port"] == 8899
              and reloaded["allowed_roots"] == ["C:\\test", "D:\\data"],
              f"token={reloaded['token'][:12]}... roots="
              f"{reloaded['allowed_roots']}")
        check("unknown keys kept on round-trip",
              reloaded.get("mystery_key") == "kept",
              f"mystery_key={reloaded.get('mystery_key')!r}")
        (base / "agent_config.json").write_text("{not json",
                                                encoding="utf-8")
        cfg2, err3 = L.load_config(base)
        check("corrupt config -> defaults + error",
              cfg2["token"] == "CHANGE-ME" and err3 is not None
              and "JSON" in err3, f"err={err3!r}")


def test_validation():
    cases = [
        ("empty token", {"token": ""}, "Token is empty"),
        ("CHANGE-ME token", {"token": "CHANGE-ME"}, "still CHANGE-ME"),
        ("short token", {"token": "abc123"}, "too short"),
        ("empty host", {"host": ""}, "Host is empty"),
        ("0.0.0.0 host", {"host": "0.0.0.0"}, "0.0.0.0 is refused"),
        ("bad host", {"host": "not-an-ip"}, "not a valid IP"),
        ("host 999", {"host": "999.1.1.1"}, "not a valid IP"),
        ("port text", {"port": "abc"}, "HTTP port must be a whole number"),
        ("port 0", {"port": "0"}, "between 1 and 65535"),
        ("port 70000", {"port": "70000"}, "between 1 and 65535"),
        ("bridge port text", {"bridge_port": "x"},
         "Bridge port must be a whole number"),
        ("same ports", {"port": "8899", "bridge_port": "8899"},
         "must be different"),
        ("timeout 0", {"timeout_secs": "0"}, "between 5 and 3600"),
        ("timeout text", {"timeout_secs": "soon"},
         "whole number of seconds"),
    ]
    for name, override, needle in cases:
        errors = L.validate_config(good_config(**override))
        check(f"validation rejects: {name}",
              any(needle in e for e in errors),
              f"errors={errors}")
    check("validation accepts good config",
          L.validate_config(good_config()) == [],
          f"errors={L.validate_config(good_config())}")
    check("validation accepts flag-off good config",
          L.validate_config(good_config(remote_control_enabled=False)) == [],
          "flag off is valid (just refuses at runtime)")


def test_token_generator():
    tokens = {L.generate_token() for _ in range(100)}
    sample = next(iter(tokens))
    ok_charset = all(c in string.ascii_letters + string.digits + "-_"
                     for c in sample)
    check("token generator: 100 unique, 43 chars, URL-safe",
          len(tokens) == 100 and len(sample) == 43 and ok_charset,
          f"len={len(sample)} unique={len(tokens)} sample={sample[:8]}...")


def test_tailscale_detect_no_binary():
    old_path = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["PATH"] = tmp  # no tailscale binary in there
        try:
            result = L.detect_tailscale_ip(timeout=3)
        finally:
            os.environ["PATH"] = old_path
    check("tailscale detect with no binary -> None, no crash",
          result is None, f"result={result!r}")


# ---------------------------------------------------------------- health stub

class _Stub(http.server.BaseHTTPRequestHandler):
    flag = True

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"ok": True, "server": "stub",
                               "remote_control_enabled": self.flag}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_health_check():
    port = _free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _Stub.flag = True
        ok, msg = L.check_health("127.0.0.1", port)
        check("health check vs stub (flag on)",
              ok and "ON" in msg, f"ok={ok} msg={msg!r}")
        _Stub.flag = False
        ok2, msg2 = L.check_health("127.0.0.1", port)
        check("health check vs stub (flag off) still OK, reports OFF",
              ok2 and "OFF" in msg2, f"ok={ok2} msg={msg2!r}")
    finally:
        server.shutdown()
    dead = _free_port()
    ok3, msg3 = L.check_health("127.0.0.1", dead)
    check("health check vs closed port -> not OK, plain message",
          not ok3 and "could not reach" in msg3, f"msg={msg3!r}")


# ---------------------------------------------------------------- redaction / status text

def test_redaction_and_status_text():
    token = _TEST_TOKEN
    line = f"EXEC: echo {token} done"
    check("redact hides token in log lines",
          token not in L.redact(line, token)
          and "[token hidden]" in L.redact(line, token),
          f"redacted={L.redact(line, token)!r}")
    check("redact leaves other lines alone",
          L.redact("HTTP listening on 127.0.0.1:8899", token)
          == "HTTP listening on 127.0.0.1:8899", "unchanged")
    text = L.status_text_for_jett(good_config(), True, True, "health OK")
    check("status-for-Jett has IP/ports/flag, never the token",
          "127.0.0.1" in text and "8899" in text
          and "remote_control_enabled: True" in text
          and token not in text,
          f"text={text!r}")


# ---------------------------------------------------------------- real agent via backend

def _http_post_json(host, port, path, body, token=None):
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def _wait_health(host, port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, _msg = L.check_health(host, port, timeout=2)
        if ok:
            return True
        time.sleep(0.5)
    return False


def _stage_agent(tmp, http_port, bridge_port, token, flag):
    base = Path(tmp)
    shutil.copy(HERE / "laptop_agent_v2.py", base / "laptop_agent_v2.py")
    cfg = good_config(token=token, host="127.0.0.1",
                      port=http_port, bridge_port=bridge_port,
                      remote_control_enabled=flag, timeout_secs=30)
    L.save_config(cfg, base)
    return base, cfg


def test_backend_start_stop_real_agent():
    token = _TEST_TOKEN
    with tempfile.TemporaryDirectory() as tmp:
        base, _cfg = _stage_agent(tmp, 18989, 18765, token, True)
        agent = L.AgentProcess(base)
        ok, msg = agent.start()
        check("backend start (real agent, flag on)",
              ok and agent.state() == "running",
              f"ok={ok} msg={msg} state={agent.state()}")
        try:
            alive = _wait_health("127.0.0.1", 18989)
            check("real agent answers /health via backend start",
                  alive, "health OK within 20s")
            status, body = _http_post_json(
                "127.0.0.1", 18989, "/exec",
                {"command": "echo hello-launcher-v3"}, token)
            check("real agent exec through backend-started agent",
                  status == 200 and body.get("stdout", "").strip()
                  == "hello-launcher-v3",
                  f"status={status} body={body}")
            status2, body2 = _http_post_json(
                "127.0.0.1", 18989, "/files/write",
                {"path": str(base / "note.txt"),
                 "content": "launcher wrote this\n"}, token)
            status3, body3 = _http_post_json(
                "127.0.0.1", 18989, "/files/read",
                {"path": str(base / "note.txt")}, token)
            check("real agent files write/read round-trip",
                  status2 == 200 and status3 == 200
                  and body3.get("content") == "launcher wrote this\n",
                  f"write={status2} read={status3} "
                  f"content={body3.get('content')!r}")
        finally:
            msg = agent.stop()
            out = " ".join(agent.drain())
            # drain again after a beat: reader thread may still flush
            time.sleep(0.3)
            out += " " + " ".join(agent.drain())
        check("backend stop terminates the agent",
              agent.state() == "stopped", f"stop msg={msg}")
        check("agent output captured (HTTP + BRIDGE lines kept)",
              "HTTP listening" in out and "BRIDGE listening" in out,
              f"captured_tail={out[-220:]!r}")


def test_backend_bad_config_exit_is_visible():
    # THE regression test for Phil's pain: agent exits at once on a
    # bad config, and the reason must be captured, not lost.
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        shutil.copy(HERE / "laptop_agent_v2.py", base / "laptop_agent_v2.py")
        cfg = good_config(token="CHANGE-ME", host="127.0.0.1",
                          port=18990, bridge_port=18766,
                          remote_control_enabled=True)
        L.save_config(cfg, base)
        agent = L.AgentProcess(base)
        ok, msg = agent.start()
        deadline = time.time() + 15
        state = agent.state()
        while state == "running" and time.time() < deadline:
            time.sleep(0.3)
            state = agent.state()
        time.sleep(0.3)
        out = " ".join(agent.drain())
        check("bad-config agent exits fast with captured reason",
              ok and state.startswith("exited:")
              and "set a real token" in out,
              f"start_ok={ok} state={state} out={out!r}")
        agent.stop()


def test_backend_flag_off_refuses():
    token = _TEST_TOKEN
    with tempfile.TemporaryDirectory() as tmp:
        base, _cfg = _stage_agent(tmp, 18991, 18767, token, False)
        agent = L.AgentProcess(base)
        ok, _msg = agent.start()
        try:
            alive = _wait_health("127.0.0.1", 18991)
            check("flag-off agent still answers /health",
                  ok and alive, "health OK, flag off")
            status, body = _http_post_json(
                "127.0.0.1", 18991, "/exec", {"command": "whoami"}, token)
            check("flag-off agent refuses exec with 403",
                  status == 403 and "remote control is off" in str(body),
                  f"status={status} body={body}")
        finally:
            agent.stop()


def test_agent_file_unchanged():
    import hashlib
    digest = hashlib.sha256(
        (HERE / "laptop_agent_v2.py").read_bytes()).hexdigest()
    check("agent file unchanged from v2 (sha256 recorded at build)",
          digest == "4d5d64b8a8183b14913a19696a0a80bf0554b4fe2daf0b8f07c00dd4cb41773f",
          f"sha256={digest[:16]}...")


def main():
    test_config_round_trip()
    test_validation()
    test_token_generator()
    test_tailscale_detect_no_binary()
    test_health_check()
    test_redaction_and_status_text()
    test_backend_start_stop_real_agent()
    test_backend_bad_config_exit_is_visible()
    test_backend_flag_off_refuses()
    test_agent_file_unchanged()
    print(f"\n{PASSES and len(PASSES) or 0}/{len(PASSES) + len(FAILURES)} "
          f"checks passed")
    if FAILURES:
        print("FAILURES:", FAILURES)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
