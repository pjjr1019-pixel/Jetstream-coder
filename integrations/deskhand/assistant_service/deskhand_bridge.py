#!/usr/bin/env python3
"""
deskhand_bridge.py — Deskhand assistant_service bridge (PROTOTYPE).

Port of the laptop-agent v2 bridge (laptop_agent_v2.py: HTTP on 8899 +
WebSocket bridge speaking PROTOCOL.md v1) into Deskhand's Python
assistant_service. Phil lifted the Nov 3 no-JettPad-code-import rule
for this port on 2026-11-07 ("Port the bridge code in").

PROTOTYPE ONLY. Do NOT deploy, install, or treat as production.
Nothing in this module is action-ready on its own: see the safety
section below.

What it is
----------
One process, two transports sharing the same code, exactly like v2:
HTTP endpoints (health / exec / files / screenshot / desktop.open)
plus a WebSocket bridge (auth first, envelope id/ts/in_reply_to,
ping->pong, get_status, get_capabilities, get_screenshot, bridge
exec + files + desktop.open).

What changed vs v2
------------------
* Names/config: server "deskhand-bridge-prototype", config file
  `deskhand_bridge_config.json`, log `deskhand_bridge.log`.
* Loopback ONLY. v2 bound the Tailscale IP or 127.0.0.1; this port
  refuses any non-loopback host at startup (including 0.0.0.0).
  Deskhand is two-apps-only: no Tailscale, no VPN, no external
  relay, no helper process. This HTTP/WebSocket transport over
  loopback is for proof only and must be replaced by Deskhand's own
  embedded channel during tightening (handoff-4).
* Default ports moved to 8999 (HTTP) / 8775 (bridge) so this
  prototype never collides with a laptop agent (8899/8765) running
  on the same machine. Ports are config, not behavior.
* NEW: `desktop.open` — a detached app launch (see below). This is
  the improvement requested after the 2026-11-07 live test, where
  `start notepad <file>` returned exit 0 twice while no Notepad
  process ever existed: an exec exit code is not proof an app
  opened.

desktop.open
------------
Launches a GUI app detached from the exec session and VERIFIES the
process actually exists before reporting success:
* Windows: writes a small .ps1 launcher and runs it with
  `powershell -NoProfile -ExecutionPolicy Bypass -File <script>`
  (unquoted path — inline quoted `powershell -Command "..."` gets
  mangled by the cmd shell, proven live 2026-10-21). The script
  uses `Invoke-CimMethod Win32_Process Create` (the method that
  produced a real window on Phil's PC, PID 9156, 2026-11-05) and
  then checks `Get-Process -Id <pid>` in the same script.
  Reply: {"opened": bool, "pid": int|null,
          "process_name": str|null, "verified": bool, "detail": str}.
  `opened` is true ONLY when the process was verified running.
  A launch whose process is not there afterwards is reported
  opened=false, never upgraded to success.
* Any other platform: honest error, never fake success.
  ("desktop.open is Windows-only; this host is <platform>.")

Safety core — READ THIS BEFORE ENABLING
---------------------------------------
* MuseBridge permissions, kept exactly from v2: shared token on
  every request (HTTP Bearer, bridge `auth` first message),
  constant-time compare, token never logged; `remote_control_enabled`
  defaults to FALSE and is enforced server-side on BOTH transports
  (flag off -> HTTP 403 / bridge `error`, while status/capabilities
  still answer and report the flag); every command/file/screenshot/
  launch is appended to deskhand_bridge.log with its message id and
  type (file contents and the token are never logged); duplicate
  message ids replay the stored result without re-running; messages
  >5 MiB ignored; unknown types ignored; ambiguous requests get an
  `error`, never a guess; submit_task/chat honestly not implemented.
  Closing the process (Ctrl+C) kills access instantly.
* Deskhand safety core is NOT bypassed — because it is NOT PRESENT
  in assistant_service. The files ported alongside this module
  (deskhand_dispatch.py, deskhand_local.py) contain NO usable gate
  for desktop actions: Dispatcher.stop() is chat-only and its own
  docstring says it "must never be used as the desktop STOP
  authority", and assistant_service/README.md states there is "no
  proposal parser, correlated action ledger, or executor" and that
  "a future action integration must use Deskhand-owned state and
  connect to a single host-enforced authorization/STOP path before
  any action can be enabled." This module therefore gates ONLY on
  its own `remote_control_enabled` flag. That is a prototype stand-in,
  not Deskhand authorization. Do NOT treat flag-on as permission to
  act: no host STOP/grant gate was ever wired in (the `set_host_gate`
  hook was removed in 100.030, B2-D -- zero callers), so it must stay
  flag-off outside a supervised test, and it must never be reachable
  from the phone app or the Muse chat. `get_status` reports
  "host_gate: none (flag only)".

Usage (prototype, on the PC, supervised test only):
  1. cp deskhand_bridge_config.example.json deskhand_bridge_config.json
     (set token, remote_control_enabled = true for the test only)
  2. pip install websockets mss pillow   (mss/pillow only for screenshots)
  3. python deskhand_bridge.py [--config deskhand_bridge_config.json]
  4. Turn the flag back to false (or close the window) when done.
"""

import argparse
import asyncio
import base64
import fnmatch
import hmac
import io
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

MAX_BODY = 1024 * 1024            # 1 MiB HTTP request cap (v2, unchanged)
MAX_WS_BYTES = 5 * 1024 * 1024    # PROTOCOL.md section 3: 5 MiB hard cap
SCREENSHOT_MAX_BYTES = 300 * 1024  # PROTOCOL.md section 7: target < 300 KiB
SCREENSHOT_DEFAULT_EDGE = 640
PROTOCOL_VERSION = 1
SERVER_NAME = "deskhand-bridge-prototype"

# Protocol types this server knows but deliberately does NOT implement.
# These get an honest `error` reply (not silence, not a guess) so the
# peer never waits forever for an answer that will not come.
KNOWN_UNSUPPORTED = {
    "submit_task", "cancel_task", "task_accepted", "task_update",
    "task_result", "chat_request", "chat_response", "chat_accepted",
    "chat_reply", "chat_failed", "chat_proactive", "chat_cancel",
    "chat_proactive_accepted", "status", "screenshot", "capabilities",
    "exec_result", "files.search_result", "files.read_result",
    "files.write_result", "desktop.open_result", "auth_ok", "auth_fail",
    "alert",
}

CONFIG = None
LOG_LOCK = threading.Lock()
STARTED_AT = time.time()

# B2-D (100.030): the host-gate hook (HOST_GATE / set_host_gate /
# GateDenied) was removed -- defined, never wired, zero callers. The
# flag is the only gate, exactly as the module docstring warns.


# ---------------------------------------------------------------- config

class BridgeConfig:
    def __init__(self, path):
        p = Path(path)
        if not p.exists():
            sys.exit(f"config not found: {p} "
                     "(copy deskhand_bridge_config.example.json)")
        cfg = json.loads(p.read_text())
        self.token = cfg.get("token", "")
        if not self.token or self.token == "CHANGE-ME":
            sys.exit("set a real token in the config first")
        self.host = cfg.get("host", "127.0.0.1")
        # Prototype transport is loopback ONLY (two-apps-only: no
        # Tailscale, no VPN, no external relay). Any non-loopback
        # address — including 0.0.0.0 — is refused at startup.
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            sys.exit(f"host {self.host!r} is refused: this prototype "
                     "binds 127.0.0.1 only — loopback proof transport, "
                     "never expose it on a LAN or public interface")
        self.port = int(cfg.get("port", 8999))
        self.bridge_port = int(cfg.get("bridge_port", 8775))
        self.bridge_path = cfg.get("bridge_path", "/v1/bridge")
        # Master flag (mind-bridge-toggle-spec). Default FALSE on
        # purpose: access is granted by an explicit act in the
        # config, never by accident from running a copied folder.
        self.remote_control_enabled = bool(cfg.get("remote_control_enabled", False))
        self.timeout_secs = int(cfg.get("timeout_secs", 120))
        self.log_file = Path(cfg.get("log_file", "deskhand_bridge.log"))
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
    # B2-D (100.030): flag-only. The host-gate branch was removed with
    # HOST_GATE / set_host_gate (zero callers); see the module docstring.
    if not remote_enabled():
        raise FlagOff("remote control is off "
                      "(remote_control_enabled=false in "
                      "deskhand_bridge_config.json)")


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


class DesktopOpenUnavailable(Exception):
    pass


def _ps_quote(s):
    """Escape a string for a PowerShell single-quoted literal."""
    return "'" + s.replace("'", "''") + "'"


def do_desktop_open(target, args=""):
    """Launch a GUI app detached, then VERIFY the process exists.

    Returns {"opened", "pid", "process_name", "verified", "detail"}.
    `opened` is True ONLY when the launched process was verified
    running afterwards. An exec exit code is never treated as proof
    an app opened (2026-11-07 live lesson: `start notepad <file>`
    returned exit 0 twice with no Notepad process ever existing).

    Windows: a .ps1 launcher is written and run with
    `powershell -NoProfile -ExecutionPolicy Bypass -File <script>`
    (unquoted path); the script creates the process with
    Invoke-CimMethod Win32_Process Create and checks Get-Process
    in the same run. Non-Windows: honest error, never fake success.
    """
    require_enabled()
    if not isinstance(target, str) or not target.strip():
        raise ValueError("desktop.open needs a non-empty target "
                         "(exe path or app name) — not guessing one")
    if args is not None and not isinstance(args, str):
        raise ValueError("args must be a string")
    args = args or ""
    log_line(f"OPEN: {target[:200]}")
    if sys.platform != "win32":
        raise DesktopOpenUnavailable(
            "desktop.open is Windows-only; this host is "
            f"{sys.platform} — refusing, not faking success")

    command_line = target.strip()
    if args.strip():
        command_line += " " + args.strip()
    ps = (
        "$cmd = " + _ps_quote(command_line) + "\n"
        "$r = Invoke-CimMethod -ClassName Win32_Process "
        "-MethodName Create "
        "-Arguments @{ CommandLine = $cmd }\n"
        "Write-Output (\"RETURNVALUE=\" + $r.ReturnValue)\n"
        "Write-Output (\"PROCESSID=\" + $r.ProcessId)\n"
        "Start-Sleep -Milliseconds 500\n"
        "$p = Get-Process -Id $r.ProcessId -ErrorAction SilentlyContinue\n"
        "if ($null -eq $p) { Write-Output 'RUNNING=no' } else {\n"
        "  Write-Output 'RUNNING=yes'\n"
        "  Write-Output (\"PROCNAME=\" + $p.ProcessName)\n"
        "}\n"
    )
    script_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1",
                                         delete=False,
                                         encoding="utf-8") as f:
            f.write(ps)
            script_path = f.name
        # Unquoted path, per the proven pattern (inline quoted
        # powershell -Command strings get mangled by the cmd shell).
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script_path],
            capture_output=True, text=True,
            timeout=CONFIG.timeout_secs)
    except subprocess.TimeoutExpired:
        return {"opened": False, "pid": None, "process_name": None,
                "verified": False,
                "detail": "launcher timed out; app launch uncertain, "
                          "never claimed opened"}
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except OSError:
                pass
    out = proc.stdout or ""
    ret, pid, running, pname = None, None, False, None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("RETURNVALUE="):
            try:
                ret = int(line.split("=", 1)[1])
            except ValueError:
                ret = None
        elif line.startswith("PROCESSID="):
            try:
                pid = int(line.split("=", 1)[1])
            except ValueError:
                pid = None
        elif line == "RUNNING=yes":
            running = True
        elif line.startswith("PROCNAME="):
            pname = line.split("=", 1)[1]
    if proc.returncode != 0:
        log_line(f"OPEN FAILED launcher exit {proc.returncode}: "
                 f"{target[:120]}")
        return {"opened": False, "pid": pid, "process_name": pname,
                "verified": False,
                "detail": f"launcher failed (exit {proc.returncode}): "
                          f"{proc.stderr[:200]}"}
    if ret != 0:
        log_line(f"OPEN FAILED CIM ReturnValue={ret}: {target[:120]}")
        return {"opened": False, "pid": pid, "process_name": None,
                "verified": False,
                "detail": f"Win32_Process Create returned {ret} "
                          "(process not created)"}
    if not running:
        log_line(f"OPEN UNVERIFIED pid={pid}: {target[:120]}")
        return {"opened": False, "pid": pid, "process_name": None,
                "verified": False,
                "detail": "process was created but was not running "
                          "500ms later — launch reported as NOT opened"}
    log_line(f"OPEN VERIFIED pid={pid} name={pname}: {target[:120]}")
    return {"opened": True, "pid": pid, "process_name": pname,
            "verified": True,
            "detail": f"process {pid} ({pname}) verified running"}


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
            f"deskhand-bridge-prototype, protocol {PROTOCOL_VERSION}, "
            f"uptime {int(time.time() - STARTED_AT)}s, "
            f"remote_control_enabled={remote_enabled()}, "
            f"host_gate=none (flag only), "
            f"http_port={CONFIG.port}, bridge_port={CONFIG.bridge_port}"
        ),
        "remote_control_enabled": remote_enabled(),
        "host_gate": "none (flag only — prototype, not action-ready)",
    }


def do_capabilities():
    on = remote_enabled()
    return {
        "operations": {
            "exec": {"supported": True, "enabled": on},
            "files.search": {"supported": True, "enabled": on},
            "files.read": {"supported": True, "enabled": on},
            "files.write": {"supported": True, "enabled": on},
            "desktop.open": {"supported": True, "enabled": on,
                             "requires": "Windows host; verified by "
                                         "process existence, never by "
                                         "exit code alone"},
            "get_screenshot": {"supported": True, "enabled": on},
            "get_status": {"supported": True, "enabled": True},
            "get_capabilities": {"supported": True, "enabled": True},
            "ping": {"supported": True, "enabled": True},
            "submit_task": {"supported": False, "enabled": False,
                            "requires": "not implemented in this bridge"},
            "chat": {"supported": False, "enabled": False,
                     "requires": "not implemented in this bridge"},
        },
        "features": {
            "screenshots": {"local_available": True, "bridge_exposed": True,
                             "enabled": on},
            "exec": {"local_available": True, "bridge_exposed": True,
                     "enabled": on},
            "files": {"local_available": True, "bridge_exposed": True,
                      "enabled": on},
            "desktop.open": {"local_available": sys.platform == "win32",
                             "bridge_exposed": True, "enabled": on},
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
                          "flag off refuses exec/files/screenshot/"
                          "desktop.open; Deskhand host gate: "
                          "none — flag is the only gate"),
        "remote_control_enabled": on,
    }


# ---------------------------------------------------------------- HTTP

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
    server_version = "DeskhandBridge/1.0"

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
        elif self.path == "/desktop/open":
            self.handle_desktop_open(self.read_json_body())
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
        try:
            require_enabled()
        except FlagOff:
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

    def handle_desktop_open(self, data):
        if not data or "target" not in data:
            send_json(self, 400, {"error": 'body must be {"target": '
                                          '"...", "args": "..."}'})
            return
        if not self._flag_gate():
            return
        try:
            result = do_desktop_open(data["target"],
                                     data.get("args", ""))
        except DesktopOpenUnavailable as e:
            # Honest non-support: 501, never a faked success.
            send_json(self, 501, {"error": str(e)})
            return
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
             "(loopback prototype — do not expose publicly)")
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
        if msg_type == "desktop.open":
            if "target" not in payload:
                return error_reply(
                    None, msg_id,
                    "ambiguous request: desktop.open needs "
                    "payload.target — not guessing what to open")
            try:
                result = await asyncio.to_thread(
                    do_desktop_open, payload["target"],
                    payload.get("args", ""))
            except DesktopOpenUnavailable as e:
                return error_reply(None, msg_id, str(e))
            return envelope(None, "desktop.open_result", msg_id, result)
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
                f"{msg_type} is not supported by "
                "deskhand-bridge-prototype "
                "(command+files+screenshot+desktop.open bridge only)")
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
                 "(protocol 1, loopback prototype — do not expose publicly)")
        await asyncio.Future()  # run forever


def main():
    global CONFIG
    ap = argparse.ArgumentParser(description="Deskhand assistant_service "
                                 "bridge prototype (HTTP + bridge v1)")
    ap.add_argument("--config", default="deskhand_bridge_config.json")
    args = ap.parse_args()
    CONFIG = BridgeConfig(args.config)

    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()
    log_line(f"remote_control_enabled={remote_enabled()} "
             f"host_gate=none (flag only) "
             f"(flag off refuses exec/files/screenshot/desktop.open "
             f"on both transports)")
    try:
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        log_line("stopped")


if __name__ == "__main__":
    main()
