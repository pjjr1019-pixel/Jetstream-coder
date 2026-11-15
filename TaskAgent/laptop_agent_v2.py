#!/usr/bin/env python3
"""
laptop_agent_v2.py — Throwaway-laptop agent v2.

v1 (HTTP on 8899) + Jett's own coded bridge (WebSocket, PROTOCOL.md v1)
on ws://<tailscale-ip>:8765/v1/bridge, in ONE process.

TEST BOX ONLY. Do not run on a machine you care about.

What v2 adds over v1
--------------------
* A WebSocket bridge server speaking PROTOCOL.md v1, backed by the SAME
  exec / files / screenshot functions as the v1 HTTP endpoints, so
  there is one implementation of each action, not two that can drift.
* The same shared token from agent_config.json authenticates BOTH
  transports. The token travels in the first WebSocket message only
  and is never written to any log.
* `remote_control_enabled` master flag (see mind-bridge-toggle-spec):
  enforced server-side on every control endpoint (HTTP + bridge).
  OFF means OFF — exec / files / screenshot refuse with a clear
  reason, on both transports. Default is FALSE: Phil enables it
  deliberately per machine by setting it to true in agent_config.json,
  so a stray run of a copied folder never grants access by accident.
  Status / capabilities / ping still answer while off, and they
  report the flag, so Jett can see WHY an action was refused instead
  of guessing.
* PROTOCOL.md v1 semantics: envelope id/ts/in_reply_to, ping/pong,
  get_status, get_capabilities, get_screenshot (PNG, longest edge
  <= 640, target < 300 KiB), unknown types ignored, >5 MiB messages
  rejected, duplicate ids return the same result without re-running,
  and ambiguous requests get an `error` reply instead of a guess.

What v2 does NOT add (honest gaps)
----------------------------------
* No submit_task / chat: this agent is a command+files+screenshot
  bridge, not a task runner or chat relay. get_capabilities says so.
* No lock-screen auto-off: there is no Windows session watcher in
  this build. Closing the window (Ctrl+C) is the kill-switch and it
  kills access instantly on both transports.
* No foreground-app probe: get_status reports foreground_app as
  "unknown" — this build does not pretend to know what window is
  in front.

Security model (same as v1, extended to the bridge)
---------------------------------------------------
* Bearer token on every HTTP request; `auth` as the first WebSocket
  message. Constant-time compare. Wrong token -> 401 / auth_fail.
* Bind the Tailscale interface or 127.0.0.1 only. 0.0.0.0 is refused
  at startup. NEVER expose these ports to the public internet.
* Every command, file op, screenshot and bridge action is appended
  to agent.log with its message id and type. File contents and the
  token are never logged.
* One bridge connection at a time: a newer authenticated connection
  closes the older one.

Endpoints (HTTP, port 8899 by default — unchanged from v1)
  POST /exec        {"command": "whoami"} -> {"stdout","stderr","exit_code"}
  POST /files/search {"root": ".", "pattern": "*.py"} -> {"results": [...]}
  POST /files/read   {"path": "..."} -> {"encoding","content","truncated"}
  POST /files/write  {"path": "...", "content": "..."} -> {"bytes_written"}
  GET  /screenshot                      -> {"format":"png","data_b64":"..."}
  GET  /health                          -> {"ok": true}  (no auth, liveness only)

Bridge messages (WebSocket, port 8765 by default, path /v1/bridge)
  Client -> server: auth, ping, get_status, get_capabilities,
                    get_screenshot, exec, files.search, files.read,
                    files.write
  Server -> client: auth_ok / auth_fail, pong, status, capabilities,
                    screenshot, exec_result, files.search_result,
                    files.read_result, files.write_result, error

Usage:
  1. cp agent_config.example.json agent_config.json
     (set token, host = laptop Tailscale IP, remote_control_enabled = true)
  2. pip install websockets mss pillow   (mss/pillow only for screenshots)
  3. python laptop_agent_v2.py [--config agent_config.json]
"""

import argparse
import asyncio
import base64
import fnmatch
import hmac
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

MAX_BODY = 1024 * 1024            # 1 MiB HTTP request cap (v1, unchanged)
MAX_WS_BYTES = 5 * 1024 * 1024    # PROTOCOL.md section 3: 5 MiB hard cap
SCREENSHOT_MAX_BYTES = 300 * 1024  # PROTOCOL.md section 7: target < 300 KiB
SCREENSHOT_DEFAULT_EDGE = 640
PROTOCOL_VERSION = 1
SERVER_NAME = "laptop-agent-v2"

# Protocol types this server knows but deliberately does NOT implement.
# These get an honest `error` reply (not silence, not a guess) so Jett
# never waits forever for an answer that will not come.
KNOWN_UNSUPPORTED = {
    "submit_task", "cancel_task", "task_accepted", "task_update",
    "task_result", "chat_request", "chat_response", "chat_accepted",
    "chat_reply", "chat_failed", "chat_proactive", "chat_cancel",
    "chat_proactive_accepted", "status", "screenshot", "capabilities",
    "exec_result", "files.search_result", "files.read_result",
    "files.write_result", "auth_ok", "auth_fail", "alert",
}

CONFIG = None
LOG_LOCK = threading.Lock()
STARTED_AT = time.time()


# ---------------------------------------------------------------- config

class AgentConfig:
    def __init__(self, path):
        p = Path(path)
        if not p.exists():
            sys.exit(f"config not found: {p} (copy agent_config.example.json)")
        cfg = json.loads(p.read_text())
        self.token = cfg.get("token", "")
        if not self.token or self.token == "CHANGE-ME":
            sys.exit("set a real token in the config first")
        self.host = cfg.get("host", "127.0.0.1")
        if self.host == "0.0.0.0":
            sys.exit("host 0.0.0.0 is refused: bind the Tailscale IP or "
                     "127.0.0.1 only — never expose this agent publicly")
        self.port = int(cfg.get("port", 8899))
        self.bridge_port = int(cfg.get("bridge_port", 8765))
        self.bridge_path = cfg.get("bridge_path", "/v1/bridge")
        # Master flag (mind-bridge-toggle-spec). Default FALSE on purpose:
        # access is granted by an explicit act in the config, never by
        # accident from running a copied folder.
        self.remote_control_enabled = bool(cfg.get("remote_control_enabled", False))
        self.timeout_secs = int(cfg.get("timeout_secs", 120))
        self.log_file = Path(cfg.get("log_file", "agent.log"))
        self.allowed_roots = [os.path.abspath(r) for r in cfg.get("allowed_roots", [])]


def log_line(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    print(line, end="", flush=True)
    with LOG_LOCK:
        try:
            with CONFIG.log_file.open("a") as f:
                f.write(line)
        except OSError:
            pass


def remote_enabled():
    return bool(CONFIG.remote_control_enabled)


class FlagOff(Exception):
    """Raised when a control op is attempted while the master flag is off."""


def require_enabled():
    if not remote_enabled():
        raise FlagOff("remote control is off "
                      "(remote_control_enabled=false in agent_config.json)")


# ---------------------------------------------------------------- shared core
# One implementation of each action. HTTP handlers and bridge handlers
# both call these — nothing is implemented twice.

def check_path(p):
    """Resolve p; return absolute path, or None if outside allowed_roots."""
    if not p or not isinstance(p, str):
        return None
    ap = os.path.abspath(os.path.expanduser(p))
    if CONFIG.allowed_roots:
        for r in CONFIG.allowed_roots:
            try:
                if os.path.commonpath([ap, r]) == r:
                    return ap
            except ValueError:
                continue
        return None
    return ap


def do_exec(command):
    """Run a shell command. Returns {"stdout","stderr","exit_code"}."""
    require_enabled()
    if not isinstance(command, str) or not command.strip():
        raise ValueError('command must be a non-empty string')
    log_line(f"EXEC: {command[:200]}")
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=CONFIG.timeout_secs)
        result = {"stdout": proc.stdout[-20000:],
                  "stderr": proc.stderr[-20000:],
                  "exit_code": proc.returncode}
    except subprocess.TimeoutExpired:
        result = {"stdout": "", "stderr": "timed out",
                  "exit_code": 124}
    log_line(f"EXIT {result['exit_code']}: {command[:120]}")
    return result


def do_files_search(root, pattern="*", max_results=50):
    require_enabled()
    ap = check_path(root if isinstance(root, str) else ".")
    if not ap or not os.path.isdir(ap):
        raise ValueError("bad root (or outside allowed_roots)")
    try:
        max_results = min(int(max_results), 200)
    except (TypeError, ValueError):
        raise ValueError("max_results must be an integer")
    results, visited = [], 0
    for dirpath, _dirnames, filenames in os.walk(ap):
        visited += 1
        if visited > 10000:
            break
        for fn in filenames:
            if fnmatch.fnmatch(fn, pattern):
                full = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                results.append({"path": full, "size": size})
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break
    log_line(f"SEARCH root={ap} pattern={pattern} hits={len(results)}")
    return {"results": results, "truncated": len(results) >= max_results}


def do_files_read(path):
    require_enabled()
    p = check_path(path)
    if not p or not os.path.isfile(p):
        raise FileNotFoundError("not found (or outside allowed_roots)")
    with open(p, "rb") as f:
        raw = f.read(1024 * 1024 + 1)
    truncated = len(raw) > 1024 * 1024
    raw = raw[:1024 * 1024]
    try:
        payload = {"encoding": "utf-8", "content": raw.decode("utf-8")}
    except UnicodeDecodeError:
        payload = {"encoding": "base64",
                   "content": base64.b64encode(raw).decode()}
    payload["truncated"] = truncated
    log_line(f"READ {p}")
    return payload


def do_files_write(path, content, encoding="utf-8"):
    require_enabled()
    p = check_path(path)
    if not p:
        raise ValueError("bad path (or outside allowed_roots)")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    try:
        raw = content.encode("utf-8") if encoding == "utf-8" \
            else base64.b64decode(content)
    except (ValueError, UnicodeEncodeError):
        raise ValueError("bad content encoding")
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("content over 5 MiB cap")
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(p, "wb") as f:
        f.write(raw)
    log_line(f"WRITE {p} ({len(raw)} bytes)")
    return {"bytes_written": len(raw), "path": p}


class ScreenshotUnavailable(Exception):
    pass


def do_screenshot(max_edge=SCREENSHOT_DEFAULT_EDGE):
    """Capture the desktop. Returns {"format","width","height","data_b64"}.

    Enforces PROTOCOL.md section 7: longest edge <= max_edge
    (default 640) and PNG target < 300 KiB — the edge is stepped down
    until the PNG fits, or an honest error is raised.
    """
    require_enabled()
    try:
        max_edge = int(max_edge)
    except (TypeError, ValueError):
        raise ValueError("max_edge must be an integer")
    if not 16 <= max_edge <= 640:
        raise ValueError("max_edge must be between 16 and 640")
    try:
        import mss
        from PIL import Image
    except ImportError:
        raise ScreenshotUnavailable(
            "pip install mss pillow for screenshots")
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    edge = max_edge
    while True:
        frame = img.copy()
        frame.thumbnail((edge, edge))
        buf = io.BytesIO()
        frame.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= SCREENSHOT_MAX_BYTES or edge <= 80:
            break
        edge //= 2
    if len(data) > SCREENSHOT_MAX_BYTES:
        raise ScreenshotUnavailable(
            f"screenshot still over 300 KiB at {edge}px edge; refusing "
            "to send an oversize image")
    log_line(f"SCREENSHOT {frame.width}x{frame.height} "
             f"{len(data)} bytes (edge {edge})")
    return {"format": "png", "width": frame.width, "height": frame.height,
            "data_b64": base64.b64encode(data).decode()}


def do_status():
    return {
        "activity": "idle — waiting for a bridge or HTTP request",
        "foreground_app": "unknown (no foreground probe in this build)",
        "pending": [],
        "notes": (
            f"laptop-agent-v2, protocol {PROTOCOL_VERSION}, "
            f"uptime {int(time.time() - STARTED_AT)}s, "
            f"remote_control_enabled={remote_enabled()}, "
            f"http_port={CONFIG.port}, bridge_port={CONFIG.bridge_port}"
        ),
        "remote_control_enabled": remote_enabled(),
    }


def do_capabilities():
    on = remote_enabled()
    return {
        "operations": {
            "exec": {"supported": True, "enabled": on},
            "files.search": {"supported": True, "enabled": on},
            "files.read": {"supported": True, "enabled": on},
            "files.write": {"supported": True, "enabled": on},
            "get_screenshot": {"supported": True, "enabled": on},
            "get_status": {"supported": True, "enabled": True},
            "get_capabilities": {"supported": True, "enabled": True},
            "ping": {"supported": True, "enabled": True},
            "submit_task": {"supported": False, "enabled": False,
                            "requires": "not implemented in this agent"},
            "chat": {"supported": False, "enabled": False,
                     "requires": "not implemented in this agent"},
        },
        "features": {
            "screenshots": {"local_available": True, "bridge_exposed": True,
                             "enabled": on},
            "exec": {"local_available": True, "bridge_exposed": True,
                     "enabled": on},
            "files": {"local_available": True, "bridge_exposed": True,
                      "enabled": on},
        },
        "chat": {"streaming": False},
        "limits": {
            "max_message_bytes": MAX_WS_BYTES,
            "screenshot_max_edge": SCREENSHOT_DEFAULT_EDGE,
            "screenshot_max_bytes": SCREENSHOT_MAX_BYTES,
            "exec_timeout_secs": CONFIG.timeout_secs,
        },
        "protocol": PROTOCOL_VERSION,
        "authorization": ("shared token + remote_control_enabled flag; "
                          "flag off refuses exec/files/screenshot"),
        "remote_control_enabled": on,
    }


# ---------------------------------------------------------------- HTTP (v1, unchanged behaviour + flag gate)

def authed(handler):
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:], CONFIG.token)


def send_json(handler, code, obj):
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "LaptopAgent/2.0"

    def log_message(self, *args):
        pass  # we log our own way

    def _require_auth(self):
        if not authed(self):
            send_json(self, 401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            send_json(self, 200, {"ok": True,
                                  "server": SERVER_NAME,
                                  "remote_control_enabled": remote_enabled()})
            return
        if not self._require_auth():
            return
        if self.path == "/screenshot":
            self.handle_screenshot()
        else:
            send_json(self, 404, {"error": "unknown endpoint"})

    def do_POST(self):
        if not self._require_auth():
            return
        if self.path == "/exec":
            self.handle_exec()
        elif self.path == "/files/search":
            self.handle_files_search(self.read_json_body())
        elif self.path == "/files/read":
            self.handle_files_read(self.read_json_body())
        elif self.path == "/files/write":
            self.handle_files_write(self.read_json_body())
        else:
            send_json(self, 404, {"error": "unknown endpoint"})

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY or length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None

    def _flag_gate(self):
        """403 when the master flag is off. Returns True if allowed."""
        if not remote_enabled():
            send_json(self, 403, {"error": "remote control is off "
                                  "(remote_control_enabled=false)"})
            return False
        return True

    def handle_files_search(self, data):
        if not data:
            send_json(self, 400, {"error": "bad JSON body"})
            return
        if not self._flag_gate():
            return
        try:
            result = do_files_search(data.get("root", "."),
                                     data.get("pattern", "*"),
                                     data.get("max_results", 50))
        except ValueError as e:
            send_json(self, 400, {"error": str(e)})
            return
        send_json(self, 200, result)

    def handle_files_read(self, data):
        if not data:
            send_json(self, 400, {"error": "bad JSON body"})
            return
        if not self._flag_gate():
            return
        try:
            result = do_files_read(data.get("path", ""))
        except FileNotFoundError as e:
            send_json(self, 404, {"error": str(e)})
            return
        except ValueError as e:
            send_json(self, 400, {"error": str(e)})
            return
        send_json(self, 200, result)

    def handle_files_write(self, data):
        if not data or "path" not in data or "content" not in data:
            send_json(self, 400, {"error": 'body must be {"path","content"}'})
            return
        if not self._flag_gate():
            return
        try:
            result = do_files_write(data["path"], data["content"],
                                    data.get("encoding", "utf-8"))
        except ValueError as e:
            send_json(self, 400, {"error": str(e)})
            return
        send_json(self, 200, result)

    def handle_exec(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY or length <= 0:
            send_json(self, 400, {"error": "bad body size"})
            return
        try:
            data = json.loads(self.rfile.read(length))
            command = data["command"]
        except (json.JSONDecodeError, KeyError):
            send_json(self, 400, {"error": 'body must be {"command": "..."}'})
            return
        if not self._flag_gate():
            return
        try:
            result = do_exec(command)
        except ValueError as e:
            send_json(self, 400, {"error": str(e)})
            return
        send_json(self, 200, result)

    def handle_screenshot(self):
        if not self._flag_gate():
            return
        try:
            result = do_screenshot()
        except ScreenshotUnavailable as e:
            send_json(self, 501, {"error": str(e)})
            return
        except ValueError as e:
            send_json(self, 400, {"error": str(e)})
            return
        send_json(self, 200, result)


def run_http():
    srv = HTTPServer((CONFIG.host, CONFIG.port), Handler)
    log_line(f"HTTP listening on {CONFIG.host}:{CONFIG.port} "
             "(v1 endpoints, TEST ONLY — do not expose publicly)")
    srv.serve_forever()


# ---------------------------------------------------------------- bridge (PROTOCOL.md v1)

class BridgeState:
    """Per-process bridge state: idempotency cache + current connection."""

    def __init__(self):
        self.seen = OrderedDict()   # msg id -> reply dict (bounded)
        self.seen_lock = threading.Lock()
        self.current = None         # current authenticated connection
        self.current_lock = threading.Lock()

    def replay(self, msg_id):
        with self.seen_lock:
            hit = self.seen.get(msg_id)
        return json.loads(json.dumps(hit)) if hit is not None else None

    def record(self, msg_id, reply):
        with self.seen_lock:
            self.seen[msg_id] = json.loads(json.dumps(reply))
            while len(self.seen) > 500:
                self.seen.popitem(last=False)


BRIDGE = BridgeState()


def envelope(msg_id, msg_type, in_reply_to, payload):
    return {"id": msg_id if msg_id else str(uuid.uuid4()),
            "type": msg_type, "ts": int(time.time()),
            "in_reply_to": in_reply_to, "payload": payload or {}}


def error_reply(msg_id, in_reply_to, message):
    return envelope(msg_id, "error", in_reply_to,
                    {"message": message})


async def handle_bridge_request(msg):
    """Dispatch one authenticated bridge request. Returns a reply dict.

    Returns None for truly unknown types (PROTOCOL.md: ignore them).
    Raises nothing — every failure becomes an `error` reply.
    """
    msg_type = msg.get("type")
    msg_id = msg.get("id")
    payload = msg.get("payload") or {}
    if not isinstance(payload, dict):
        return error_reply(None, msg_id,
                           "payload must be a JSON object, got "
                           f"{type(payload).__name__}")

    try:
        if msg_type == "ping":
            return envelope(None, "pong", msg_id, {})
        if msg_type == "get_status":
            return envelope(None, "status", msg_id, do_status())
        if msg_type == "get_capabilities":
            return envelope(None, "capabilities", msg_id, do_capabilities())
        if msg_type == "get_screenshot":
            max_edge = payload.get("max_edge", SCREENSHOT_DEFAULT_EDGE)
            try:
                shot = await asyncio.to_thread(do_screenshot, max_edge)
            except ScreenshotUnavailable as e:
                return error_reply(None, msg_id, str(e))
            return envelope(None, "screenshot", msg_id, shot)
        if msg_type == "exec":
            if "command" not in payload:
                return error_reply(
                    None, msg_id,
                    "ambiguous request: exec needs payload.command — "
                    "not guessing what to run")
            result = await asyncio.to_thread(do_exec, payload["command"])
            return envelope(None, "exec_result", msg_id, result)
        if msg_type == "files.search":
            if "root" not in payload:
                return error_reply(
                    None, msg_id,
                    "ambiguous request: files.search needs payload.root")
            result = await asyncio.to_thread(
                do_files_search, payload["root"],
                payload.get("pattern", "*"),
                payload.get("max_results", 50))
            return envelope(None, "files.search_result", msg_id, result)
        if msg_type == "files.read":
            if "path" not in payload:
                return error_reply(
                    None, msg_id,
                    "ambiguous request: files.read needs payload.path")
            result = await asyncio.to_thread(do_files_read, payload["path"])
            return envelope(None, "files.read_result", msg_id, result)
        if msg_type == "files.write":
            if "path" not in payload or "content" not in payload:
                return error_reply(
                    None, msg_id,
                    "ambiguous request: files.write needs payload.path "
                    "and payload.content")
            result = await asyncio.to_thread(
                do_files_write, payload["path"], payload["content"],
                payload.get("encoding", "utf-8"))
            return envelope(None, "files.write_result", msg_id, result)
        if msg_type in KNOWN_UNSUPPORTED:
            return error_reply(
                None, msg_id,
                f"{msg_type} is not supported by laptop-agent-v2 "
                "(command+files+screenshot bridge only)")
        return None  # truly unknown type: ignore per PROTOCOL.md section 3
    except FlagOff as e:
        return error_reply(None, msg_id, str(e))
    except FileNotFoundError as e:
        return error_reply(None, msg_id, str(e))
    except ValueError as e:
        return error_reply(None, msg_id, str(e))
    except Exception as e:  # never leak a stack trace to the peer
        return error_reply(None, msg_id, f"internal error: {type(e).__name__}")


async def bridge_connection(ws):
    """One WebSocket connection: auth first, then the envelope loop."""
    from websockets.exceptions import ConnectionClosed

    path = getattr(getattr(ws, "request", None), "path", None)
    if path is not None and path != CONFIG.bridge_path:
        log_line(f"BRIDGE rejected wrong path {path!r}")
        await ws.close(code=1008, reason="wrong path")
        return

    # --- auth: first message, within 10s, token only (never logged)
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
    except (asyncio.TimeoutError, ConnectionClosed):
        log_line("BRIDGE auth timeout / closed before auth")
        return
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if len(raw.encode("utf-8")) > MAX_WS_BYTES:
        return  # oversize first message: drop silently per protocol
    try:
        first = json.loads(raw)
    except json.JSONDecodeError:
        first = None
    if not isinstance(first, dict) or first.get("type") != "auth" \
            or not hmac.compare_digest(str(first.get("token", "")),
                                       CONFIG.token):
        try:
            await ws.send(json.dumps({"type": "auth_fail"}))
        except ConnectionClosed:
            pass
        log_line("BRIDGE auth_fail (bad/missing token)")
        await ws.close(code=1008, reason="auth failed")
        return

    await ws.send(json.dumps({"type": "auth_ok", "server": SERVER_NAME,
                              "protocol": PROTOCOL_VERSION}))
    log_line("BRIDGE auth_ok")

    # One connection at a time: the newest authenticated connection wins.
    with BRIDGE.current_lock:
        old = BRIDGE.current
        BRIDGE.current = ws
    if old is not None and old is not ws:
        try:
            await old.close(code=1000, reason="superseded by newer connection")
        except Exception:
            pass
        log_line("BRIDGE superseded older connection")

    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if len(raw.encode("utf-8")) > MAX_WS_BYTES:
                log_line("BRIDGE dropped oversize message (>5 MiB)")
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps(
                    error_reply(None, None, "message is not valid JSON")))
                continue
            if not isinstance(msg, dict) or "type" not in msg:
                await ws.send(json.dumps(
                    error_reply(None, None,
                                "message must be a JSON object with a type")))
                continue
            msg_id = msg.get("id")
            if not msg_id or not isinstance(msg_id, str):
                await ws.send(json.dumps(
                    error_reply(None, None,
                                "ambiguous request: every bridge message "
                                "needs a string id — not guessing one")))
                continue
            log_line(f"BRIDGE id={msg_id[:8]} type={msg.get('type')}")
            # Idempotency (PROTOCOL.md section 8): same id -> same
            # result, never re-run. Exec is the case that matters.
            cached = BRIDGE.replay(msg_id)
            if cached is not None:
                log_line(f"BRIDGE id={msg_id[:8]} duplicate — replayed, "
                         "not re-run")
                await ws.send(json.dumps(cached))
                continue
            reply = await handle_bridge_request(msg)
            if reply is None:
                log_line(f"BRIDGE id={msg_id[:8]} unknown type "
                         f"{msg.get('type')!r} — ignored")
                continue
            BRIDGE.record(msg_id, reply)
            await ws.send(json.dumps(reply))
    except ConnectionClosed:
        pass
    finally:
        with BRIDGE.current_lock:
            if BRIDGE.current is ws:
                BRIDGE.current = None
        log_line("BRIDGE connection closed")


async def run_bridge():
    import websockets
    from websockets.asyncio.server import serve

    async with serve(bridge_connection, CONFIG.host, CONFIG.bridge_port,
                     max_size=MAX_WS_BYTES + 1024):
        log_line(f"BRIDGE listening on ws://{CONFIG.host}:"
                 f"{CONFIG.bridge_port}{CONFIG.bridge_path} "
                 "(protocol 1, TEST ONLY — do not expose publicly)")
        await asyncio.Future()  # run forever


def main():
    global CONFIG
    ap = argparse.ArgumentParser(description="Throwaway-laptop agent v2 "
                                 "(HTTP v1 + bridge v1)")
    ap.add_argument("--config", default="agent_config.json")
    args = ap.parse_args()
    CONFIG = AgentConfig(args.config)

    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()
    log_line(f"remote_control_enabled={remote_enabled()} "
             f"(flag off refuses exec/files/screenshot on both transports)")
    try:
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        log_line("stopped")


if __name__ == "__main__":
    main()
