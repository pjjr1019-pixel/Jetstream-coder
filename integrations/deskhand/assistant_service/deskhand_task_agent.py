"""Deskhand fallback client for the laptop agent (Task-agent).

When Deskhand's gateway worker is disconnected (action routes disabled),
the Deskhand service can route a PC action through the laptop agent
instead of failing. The gateway worker stays the primary path when it is
healthy; this module is the FALLBACK only.

The laptop agent is a standalone HTTP server on the PC (port 8899) with
endpoints /health, /exec, /files/search|read|write, /screenshot. It does
exec/files/screenshot ONLY -- no mouse, keyboard, window control, or
typing. So this fallback covers opening apps, running commands, files,
and screenshots. Reliable typing/clicking stays with the gateway worker.

Authorization (both gates must be on):
  1. Deskhand's own Full PC access grant -- the CALLER passes
     deskhand_grant_on=True only after checking Deskhand's grant state
     (access_status / FullPcAccess). This module never reads Deskhand's
     grant itself.
  2. The agent's remote_control_enabled flag -- read from the agent's
     /health response. If false, every control call is refused.

The agent's Bearer token is read from agent_config.json in the Task-agent
folder and held in memory only. It is NEVER logged, never put in an
error message, and never sent anywhere except as the Authorization
header to the agent itself.

v2 (2026-11-08, fallback-hook-design-v2.md):
  * ``dispatch_guard``: an optional callable the service op installs,
    run immediately before each acting request (after the gate check,
    before the HTTP send). It re-checks host STOP + grant at the
    dispatch boundary; it raises DispatchGuardRefused when the
    boundary check fails, and the request is never sent.
  * ``request_count()``: reads the agent-side acting-request counter
    (``requests_total`` in /health, acting endpoints only). The
    counter is specified here; the deployed agent does not report it
    yet (agent-side change still to build/deploy). Returns None
    when the field is absent.
  * Error taxonomy for the op's outcome classification: config and
    health-check failures are distinct subclasses (proven
    non-execution); a bad screenshot is TaskAgentBadScreenshot
    (read-only, safe to retry). A failure of an acting call itself
    stays TaskAgentUnavailable / TaskAgentError (uncertain).

Standard library only (urllib). No new dependencies.

Prototype only. GPT should review this before it goes on the PC.
"""

import base64
import binascii
import json
import os
import threading
import time
import urllib.error
import urllib.request

# Default location of the laptop agent on the PC (Phil, 2026-11-08).
DEFAULT_TASK_AGENT_DIR = (
    r"C:\Users\Phlip\Desktop\Deskhand\Deskhand-windows\Task-agent"
)
# The agent listens on BOTH its Tailscale IP (for the VM) and loopback
# (for on-PC callers). This client runs on the PC inside the Deskhand
# service, so it uses LOOPBACK. Do NOT point it at the Tailscale IP:
# a process on the PC cannot reliably connect to the PC's own Tailscale
# IP -- Tailscale hairpins that connection and it times out (proven live
# 2026-11-08). Loopback is the correct address for a local caller.
DEFAULT_AGENT_BASE = "http://127.0.0.1:8899"
DEFAULT_TIMEOUT = 30
_OPEN_PS1_PATH = r"C:\agent\deskhand_task_agent_open.ps1"

# Characters that must never appear in an exe name / argument passed to
# the CIM launcher. This blocks shell metacharacters and command
# injection through an action payload.
_FORBIDDEN_CHARS = set(";&|$`<>\"'\n\r")


class TaskAgentError(RuntimeError):
    """Base error for the laptop-agent fallback. Never contains the token."""


class TaskAgentUnavailable(TaskAgentError):
    """The agent is not reachable (down, wrong address, Tailscale down)."""


class TaskAgentNotEnabled(TaskAgentError):
    """The agent's remote_control_enabled flag is off."""


class TaskAgentDenied(TaskAgentError):
    """Deskhand's Full PC access grant is off (caller-side gate)."""


class TaskAgentAuthError(TaskAgentError):
    """The agent rejected the token (401). The token may have rotated."""


class TaskAgentConfigError(TaskAgentError):
    """The agent config/token could not be read (missing file, bad
    JSON, placeholder token). Raised before any request is sent, so
    the action provably never ran -- the op classifies this as
    ``failed`` (retry allowed after fixing the config)."""


class TaskAgentHealthUnavailable(TaskAgentUnavailable):
    """The agent did not answer /health during the gate check.

    Subclass of TaskAgentUnavailable (existing handlers still catch
    it). Raised only when the gate-check health call fails, i.e.
    before any acting request is sent -- the op classifies this as
    ``failed`` (proven non-execution). A TaskAgentUnavailable raised
    by an acting call itself stays ``uncertain``: the request may
    have been sent."""


class TaskAgentBadScreenshot(TaskAgentError):
    """The agent answered /screenshot but produced no usable image
    (dimensions with empty/missing/invalid image data). The capture
    is read-only, so no side effect happened and a retry is safe --
    the op classifies this as ``failed``, with the anomaly recorded
    in the journal. Raised by normalize_screenshot (v2)."""


class TaskAgentMalformedResponse(TaskAgentError):
    """The agent answered HTTP 200 with a body that is not valid JSON
    (or not valid UTF-8). B2-D H14: the request WAS sent and may have
    executed, so this is deliberately NOT in the op's
    ``_FAILED_BEFORE_DISPATCH`` set -- the op classifies it
    ``uncertain`` (no auto-retry) instead of ``failed`` (retry
    allowed), closing a replay/duplicate-execution hole. This covers
    acting and read-only calls alike: a malformed body proves nothing
    about what ran, so the outcome stays uncertain either way."""


class DispatchGuardRefused(TaskAgentError):
    """The dispatch guard refused: STOP set or the grant went off
    at the dispatch boundary, immediately before an acting request.
    The refused request was never sent. Raised by the guard callable
    the op installs (see TaskAgentClient.dispatch_guard); the client
    itself only invokes the guard, never raises this directly."""


def _truncate(text, limit=200):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "..."


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def normalize_screenshot(result):
    """Normalize one agent /screenshot result to a canonical shape.

    ROOT CAUSE (live, 2026-11-08 ~12:50 EST): the real laptop agent
    returns the image under ``data_b64`` (the bridge/gateway
    convention everywhere: deskhand_bridge.py, vendor desktop.py),
    but the fallback client's mock fake -- and whatever read the live
    result -- looked for ``png_base64``. The live call returned
    width/height (640x360) with the image field "empty" because the
    field name mismatched, not because the capture failed:
    do_screenshot() either returns a non-empty PNG or raises.

    So: accept ``data_b64`` OR ``png_base64``, return BOTH fields
    populated with the same bytes, and validate the payload. A result
    with no usable image data is an honest error, never a hollow
    success with dimensions and an empty image.
    """
    if not isinstance(result, dict):
        raise TaskAgentError(
            "screenshot returned an unexpected result: %s"
            % _truncate(result, 120))
    data = result.get("data_b64") or result.get("png_base64") or ""
    if not isinstance(data, str) or not data:
        raise TaskAgentBadScreenshot(
            "screenshot returned dimensions %sx%s but no image data "
            "(neither data_b64 nor png_base64 present); the capture "
            "did not produce a usable frame"
            % (result.get("width"), result.get("height")))
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise TaskAgentBadScreenshot(
            "screenshot image data is not valid base64: %s"
            % _truncate(e, 120)) from e
    if len(raw) < len(_PNG_MAGIC) or not raw.startswith(_PNG_MAGIC):
        raise TaskAgentBadScreenshot(
            "screenshot image data is not a PNG (%d bytes decoded); "
            "the capture did not produce a usable frame" % len(raw))
    return {"format": result.get("format") or "png",
            "width": result.get("width"),
            "height": result.get("height"),
            "data_b64": data,
            "png_base64": data,
            "bytes": len(raw)}


class TaskAgentClient:
    """HTTP client for the laptop agent, with the dual-gate check.

    token_source: path to agent_config.json (default
        <task_agent_dir>/agent_config.json). The token is read once and
        cached in memory; call reload_token() after a rotation.
    deskhand_grant_on: a callable returning bool, or a bool. The caller
        (the Deskhand service) supplies this from its own grant state.
        The client refuses every control call while it is False.
    log: optional callable(msg) for audit logging. The client only ever
        logs action kinds and truncated outcomes -- never the token.
    """

    def __init__(self, task_agent_dir=None, base_url=None, token_source=None,
                 deskhand_grant_on=None, log=None, timeout=DEFAULT_TIMEOUT,
                 dispatch_guard=None):
        self.task_agent_dir = task_agent_dir or os.environ.get("DESKHAND_TASK_AGENT_DIR") or DEFAULT_TASK_AGENT_DIR
        self.base_url = (base_url or DEFAULT_AGENT_BASE).rstrip("/")
        if token_source:
            self.token_source = token_source
        else:
            self.token_source = os.path.join(self.task_agent_dir,
                                             "agent_config.json")
        self._grant_on = deskhand_grant_on
        self._log = log or (lambda msg: None)
        self.timeout = timeout
        self._lock = threading.RLock()
        self._cached_token = None
        self._token_loaded = False
        # v2: optional callable invoked immediately before each acting
        # request (after the gate check, before the HTTP send). The
        # op installs a guard that re-checks host STOP + grant at the
        # dispatch boundary; the guard raises DispatchGuardRefused
        # when the boundary check fails. None = no extra check (v1
        # behaviour, preserved for existing callers/tests).
        self.dispatch_guard = dispatch_guard

    def set_dispatch_guard(self, guard):
        """Install (or clear, with None) the dispatch-boundary guard."""
        self.dispatch_guard = guard

    def _guard(self):
        """Run the dispatch guard, if one is installed. Lets the
        guard's exception propagate unchanged (the op classifies a
        DispatchGuardRefused as proven non-execution of that call)."""
        guard = getattr(self, "dispatch_guard", None)
        if guard is not None:
            guard()

    # -- token ---------------------------------------------------------
    def reload_token(self):
        """(Re)read the agent token from agent_config.json. Memory only.

        Raises TaskAgentConfigError (v2) when the config cannot be
        read or holds no usable token -- before any request is sent.
        """
        with self._lock:
            try:
                with open(self.token_source, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError) as e:
                raise TaskAgentConfigError(
                    "cannot read agent config %s: %s"
                    % (_truncate(self.token_source, 120), _truncate(e, 120))
                ) from e
            token = cfg.get("token", "")
            if not token or token == "CHANGE-ME":
                raise TaskAgentConfigError(
                    "no usable token in agent config %s" % _truncate(
                        self.token_source, 120))
            self._cached_token = token
            self._token_loaded = True
            return True

    def _token(self):
        with self._lock:
            if not self._token_loaded:
                self.reload_token()
            return self._cached_token

    # -- gates ---------------------------------------------------------
    def _deskhand_grant_on(self):
        g = self._grant_on
        if callable(g):
            return bool(g())
        return bool(g)

    def _check_gates(self, health=None):
        """Raise unless BOTH gates are on. Returns the health dict.

        v2: a health-check failure here raises
        TaskAgentHealthUnavailable (a TaskAgentUnavailable subclass):
        the gate runs before any acting request, so a health failure
        proves the action was never sent.
        """
        if not self._deskhand_grant_on():
            raise TaskAgentDenied(
                "Deskhand Full PC access is off; agent fallback refused")
        if health is None:
            try:
                health = self.health()
            except TaskAgentUnavailable as e:
                raise TaskAgentHealthUnavailable(
                    "laptop agent health check failed before dispatch: "
                    "%s" % _truncate(e, 160)) from e
        if not health.get("remote_control_enabled"):
            raise TaskAgentNotEnabled(
                "laptop agent remote_control_enabled is off; "
                "enable it in agent_config.json and restart the agent")
        return health

    # -- HTTP ----------------------------------------------------------
    def _request(self, method, path, body=None, auth=True):
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self._token()
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read()
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError as error:
                    # B2-D H14: a 200 with a malformed body is NOT a
                    # proven non-execution -- the request was sent and
                    # may have executed. Raise the dedicated error
                    # (a TaskAgentError, never a bare ValueError) so
                    # the op classifies it "uncertain", never "failed".
                    raise TaskAgentMalformedResponse(
                        "laptop agent returned HTTP %s with a malformed "
                        "(non-JSON) body (%d bytes); the request may "
                        "have executed" % (status, len(raw))) from error
                return status, body
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get(
                    "error", e.reason)
            except (ValueError, AttributeError):
                detail = e.reason
            if e.code == 401:
                raise TaskAgentAuthError(
                    "laptop agent rejected the token (401); "
                    "it may have rotated -- reload and retry") from e
            if e.code == 403:
                raise TaskAgentNotEnabled(
                    "laptop agent refused: %s" % _truncate(detail, 200)) from e
            raise TaskAgentError(
                "laptop agent HTTP %s: %s" % (e.code, _truncate(detail, 200))
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TaskAgentUnavailable(
                "laptop agent not reachable at %s: %s"
                % (_truncate(self.base_url, 80), _truncate(e, 120))) from e

    # -- agent API -----------------------------------------------------
    def health(self):
        """GET /health. No auth. Returns {ok, server, remote_control_enabled}."""
        status, body = self._request("GET", "/health", auth=False)
        if status != 200 or not body.get("ok"):
            raise TaskAgentUnavailable(
                "laptop agent health check failed: %s" % _truncate(body, 200))
        return body

    def request_count(self, health=None):
        """Acting-request counter from the agent, or None.

        v2 contract (GPT Q6): the agent's /health SHOULD report
        ``requests_total`` -- the count of requests to ACTING
        endpoints only (POST /exec, /files/*, GET /screenshot;
        /health reads excluded) -- plus ``requests_by_kind``. The
        count is the independent agent-side proof for Case 2
        (healthy route isolation): read it before and after a
        gateway-only action; unchanged means the agent was not
        called. Returns None when the agent does not report the
        field (the deployed agent does not yet -- the agent-side
        change is specified here, not yet built or deployed).
        """
        if health is None:
            health = self.health()
        total = health.get("requests_total")
        return total if type(total) is int else None

    def wait_ready(self, timeout=30, poll=1.0):
        """Wait until /health answers. Returns the health dict.

        Raises TaskAgentUnavailable on timeout. Use after starting the
        agent (e.g. from auto-start) before submitting any action.
        """
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                return self.health()
            except TaskAgentUnavailable as e:
                last = e
                time.sleep(poll)
        raise TaskAgentUnavailable(
            "laptop agent did not become ready in %ss: %s"
            % (timeout, _truncate(last, 120)))

    def exec(self, command):
        """POST /exec. Returns {stdout, stderr, exit_code}. Gated."""
        self._check_gates()
        self._guard()
        self._log("task-agent exec: %s" % _truncate(command, 200))
        status, body = self._request("POST", "/exec",
                                     {"command": command})
        if status != 200:
            raise TaskAgentError("exec failed: %s" % _truncate(body, 200))
        return body

    def files_read(self, path):
        """POST /files/read. Returns the agent's read result. Gated."""
        self._check_gates()
        self._guard()
        self._log("task-agent files_read: %s" % _truncate(path, 200))
        status, body = self._request("POST", "/files/read",
                                     {"path": path})
        if status != 200:
            raise TaskAgentError("files_read failed: %s" % _truncate(body, 200))
        return body

    def files_write(self, path, content, encoding="utf-8"):
        """POST /files/write. Returns the agent's write result. Gated."""
        self._check_gates()
        self._guard()
        self._log("task-agent files_write: %s" % _truncate(path, 200))
        status, body = self._request(
            "POST", "/files/write",
            {"path": path, "content": content, "encoding": encoding})
        if status != 200:
            raise TaskAgentError(
                "files_write failed: %s" % _truncate(body, 200))
        return body

    def files_search(self, root, pattern="*", max_results=50):
        """POST /files/search. Returns the agent's search result. Gated."""
        self._check_gates()
        self._guard()
        status, body = self._request(
            "POST", "/files/search",
            {"root": root, "pattern": pattern, "max_results": max_results})
        if status != 200:
            raise TaskAgentError(
                "files_search failed: %s" % _truncate(body, 200))
        return body

    def screenshot(self):
        """GET /screenshot. Returns the NORMALIZED screenshot result.

        Normalized shape: {"format","width","height","data_b64",
        "png_base64","bytes"} -- both image fields carry the same
        data, so callers reading either name get the image. Raises
        TaskAgentBadScreenshot if the agent returns dimensions with
        no usable image data (the 2026-11-08 live symptom) instead
        of passing a hollow result on. Gated.
        """
        self._check_gates()
        self._guard()
        status, body = self._request("GET", "/screenshot")
        if status != 200:
            raise TaskAgentError("screenshot failed: %s" % _truncate(body, 200))
        shot = normalize_screenshot(body)
        self._log("task-agent screenshot: %sx%s %d bytes"
                  % (shot["width"], shot["height"], shot["bytes"]))
        return shot

    # -- higher-level actions ------------------------------------------
    def open_app(self, exe, args=None):
        """Open an app via the proven CIM detached launch, then verify it.

        exe: e.g. "notepad.exe", "calc.exe". args: optional list of
        argument strings (no shell metacharacters allowed).
        Returns a receipt {ok, exe, pid, title}.
        Raises TaskAgentError if the launch or the verification fails.
        """
        if not exe or not isinstance(exe, str):
            raise ValueError("exe must be a non-empty string")
        if any(c in _FORBIDDEN_CHARS for c in exe):
            raise ValueError("exe contains a forbidden character")
        arg_list = list(args or [])
        for a in arg_list:
            if not isinstance(a, str) or any(
                    c in _FORBIDDEN_CHARS for c in a):
                raise ValueError("argument contains a forbidden character")
        # Build the command line the CIM launcher will run. Quote any
        # argument containing a space.
        parts = [exe]
        for a in arg_list:
            parts.append('"%s"' % a if " " in a else a)
        command_line = " ".join(parts)
        ps1 = (
            "$res = Invoke-CimMethod -ClassName Win32_Process "
            "-MethodName Create -Arguments "
            "@{ CommandLine = \"" + command_line.replace('"', '`"') + "\" }\n"
            "Write-Output (\"CIM ReturnValue=\" + $res.ReturnValue "
            "+ \" PID=\" + $res.ProcessId)\n"
            "Start-Sleep -Seconds 3\n"
            "$p = Get-Process -Id $res.ProcessId -ErrorAction SilentlyContinue\n"
            "if ($p) { Write-Output (\"RUNNING PID=\" + $p.Id "
            "+ \" title=[\" + $p.MainWindowTitle + \"]\") } "
            "else { Write-Output \"NOT RUNNING\" }\n"
        )
        self.files_write(_OPEN_PS1_PATH, ps1)
        out = self.exec(
            "powershell -NoProfile -ExecutionPolicy Bypass -File "
            + _OPEN_PS1_PATH)
        stdout = out.get("stdout", "")
        self._log("task-agent open_app %s: %s" % (
            _truncate(exe, 80), _truncate(stdout, 200)))
        pid = None
        title = ""
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("CIM ReturnValue="):
                try:
                    pid = int(line.split("PID=")[1].strip())
                except (IndexError, ValueError):
                    pid = None
            if line.startswith("RUNNING PID="):
                title = line.split("title=[", 1)[1].rstrip("]") \
                    if "title=[" in line else ""
        if pid is None or "NOT RUNNING" in stdout:
            raise TaskAgentError(
                "open_app %s failed: %s" % (_truncate(exe, 80),
                                            _truncate(stdout, 200)))
        return {"ok": True, "exe": exe, "pid": pid, "title": title}

    def open_notepad(self, path=None):
        """Open Notepad, optionally with a file. Returns the open_app receipt."""
        if path:
            return self.open_app("notepad.exe", [path])
        return self.open_app("notepad.exe")

    # -- translation ---------------------------------------------------
    def run_action(self, action):
        """Run one Deskhand-style action through the agent.

        action is a dict with a "kind" key. Supported kinds:
          {"kind": "exec", "command": "..."}
          {"kind": "open_app", "exe": "notepad.exe", "args": [...]}
          {"kind": "open_notepad", "path": "C:\\..."}   (path optional)
          {"kind": "files_read", "path": "..."}
          {"kind": "files_write", "path": "...", "content": "..."}
          {"kind": "files_search", "root": "...", "pattern": "..."}
          {"kind": "screenshot"}
        Returns a receipt dict {ok, kind, result}. The caller turns the
        receipt into a pad reply. Raises TaskAgentError (or a subclass)
        on any failure; the caller must NOT retry an uncertain action.
        Kinds the agent cannot do (type, click, window-targeted input)
        are rejected here with a clear error -- they stay with the
        gateway worker.
        """
        if not isinstance(action, dict):
            raise ValueError("action must be a dict")
        kind = action.get("kind")
        if kind == "exec":
            result = self.exec(action.get("command", ""))
        elif kind == "open_app":
            result = self.open_app(action.get("exe", ""),
                                   action.get("args"))
        elif kind == "open_notepad":
            result = self.open_notepad(action.get("path"))
        elif kind == "files_read":
            result = self.files_read(action.get("path", ""))
        elif kind == "files_write":
            result = self.files_write(action.get("path", ""),
                                      action.get("content", ""))
        elif kind == "files_search":
            result = self.files_search(action.get("root", "."),
                                       action.get("pattern", "*"),
                                       action.get("max_results", 50))
        elif kind == "screenshot":
            result = self.screenshot()
        elif kind in ("type", "click", "key", "desktop_type",
                      "desktop_click", "ui_invoke", "window_activate"):
            raise TaskAgentError(
                "action kind %r is not available through the laptop-agent "
                "fallback (no typing/clicking); it stays with the gateway "
                "worker" % (kind,))
        else:
            raise ValueError("unknown action kind: %r" % (kind,))
        return {"ok": True, "kind": kind, "result": result}
