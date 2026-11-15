"""Deskhand-owned lifecycle for the laptop agent (Task-agent).

Phil asked (2026-11-08 ~13:20 EST, phone): Deskhand should own the
agent lifecycle so no separate launcher click is needed -- Deskhand
starts the agent when Deskhand starts (or when the fallback is first
needed), monitors it, and restarts it if it dies.

This module is the service-side owner. It mirrors the pattern the
Deskhand service already uses for its bridge child process
(ChatService._start_bridge / _stop_bridge / _bridge_status in
deskhand_service.py): a child process owned by the service, health
checked on loopback, explicit-PID stop, log + state file in the
service data dir. No C# app change is needed for v1.

OWNERSHIP MODEL -- adopt, don't duplicate
------------------------------------------
The agent may already be running when Deskhand starts (Startup
shortcut DeskhandTaskAgent.lnk, or Phil's launcher). Starting a
second copy would fight over ports 8899/8765, so ensure_started()
first checks /health:
  - healthy  -> ADOPT it: monitor-only, owned=False. Deskhand never
    kills an adopted agent (it was started by Phil / logon, not by
    Deskhand). If an adopted agent later dies, its replacement IS
    owned by Deskhand.
  - down     -> start an owned child (EXE preferred, else .py),
    wait for /health, record its explicit PID.
The agent's own self-heal probe (laptop_agent_v2.py, VM-built,
33/33 tests, NOT yet deployed -- see SELF-HEAL-8765-DESIGN.md)
reclaims a stale port holder at agent startup, so the lifecycle
manager itself never kills by port scan: it kills only the explicit
PID of a child it started, and only via its Popen handle.

DUAL GATE -- enforced on every start, restart, and monitor poll
----------------------------------------------------------------
  1. Deskhand Full PC access grant ON. The grant lives in the C#
     HostEngine (FullPcAccess.cs), not in this service, so the
     caller supplies it as deskhand_grant_on (callable or bool).
     Default is DENY (fail-closed: no callable -> never start).
     If the grant turns off, the monitor stops the owned child.
  2. Agent remote_control_enabled true, read from /health. A flag-off
     agent is NOT restarted (flag-off is a config state, not a
     crash): it is reported as degraded_flag_off and left alone.
     Phil flips the flag in agent_config.json and restarts the agent.

STOP
----
Starting/restarting while the service is stopping is refused. The
host STOP signal itself is checked by the action-dispatch layer
(the task_agent_action op, per the fallback-hook design), not here;
this module accepts an optional stopped() callable so the host can
wire its own stop signal in (ChatService wires its stopping event).
Lifecycle start/restart never clears STOP and never bypasses it.

RESTART BUDGET
--------------
At most max_restarts (default 5) restarts in a rolling
restart_window_secs (default 3600s). Past the budget the state
becomes restart_exhausted and no further automatic restart happens
until ensure_started() is called again (a deliberate reset, e.g.
Phil / Jett re-requesting it). A crash-looping agent is reported,
not restarted forever.

STATE + LOGGING
---------------
State file <data_dir>/task_agent_lifecycle.json (PID, owned,
state, restarts, last_error, timestamps -- never the token) and
log file <data_dir>/task_agent_lifecycle.log. Every transition is
logged with its reason. The token is never read here at all (the
fallback client reads it; this module only starts/monitors the
process and checks unauthenticated /health). 100.029 (E41 + E17):
log lines are levelled plain text ([LEVEL] [lifecycle/system] code
-- msg; INFO transitions, WARN restart-cap events, ERROR failed
starts) and the log rotates at 1 MB x 2 with the file handle
reopened after a rotation, so the next write lands in the new
current file.

DESKHAND AUTOSTART -- NOT built here
-------------------------------------
This module starts the agent when DESKHAND is running. It does NOT
make Deskhand itself auto-start at logon -- Deskhand still has no
Startup entry, and that remains an open decision for Phil (see the
design doc). If Phil later approves Deskhand autostart, this module
needs no change: the agent then starts with Deskhand at logon.

Prototype only. GPT should review this before it goes on the PC.
Standard library only.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from deskhand_runtime import _make_kill_job
except Exception:  # pragma: no cover - standalone / test use
    _make_kill_job = None

# 100.029 (E17 + E41): levelled plain-text lines + rotation with
# handle reopen live in the shared retention module.
from deskhand_retention import LEVELS, format_plain_line, rotate_if_needed

DEFAULT_TASK_AGENT_DIR = (
    r"C:\Users\Phlip\Desktop\Deskhand\Deskhand-windows\Task-agent"
)
DEFAULT_AGENT_BASE = "http://127.0.0.1:8899"
DEFAULT_POLL_INTERVAL_SECS = 30
DEFAULT_MAX_RESTARTS = 5
DEFAULT_RESTART_WINDOW_SECS = 3600
DEFAULT_START_TIMEOUT_SECS = 30
HEALTH_TIMEOUT_SECS = 2
# E17 section 2 #4 (Option A): the lifecycle log rotates at 1 MB,
# 2 generations, 30 days (age is the janitor's job).
LIFECYCLE_LOG_NAME = "task_agent_lifecycle.log"
LIFECYCLE_LOG_MAX_BYTES = 1 * 1024 * 1024
LIFECYCLE_LOG_BACKUP_COUNT = 2

# States reported by status(). Keep the names stable: the PC panel and
# the pad replies will show them verbatim.
STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"              # healthy (owned or adopted)
STATE_DEGRADED_FLAG_OFF = "degraded_flag_off"  # healthy but remote_control_enabled=false
STATE_RESTARTING = "restarting"
STATE_REFUSED_GATE = "refused_gate"    # Deskhand grant off
STATE_REFUSED_FLAG = "refused_flag"    # (reserved; flag-off is degraded, not refused)
STATE_REFUSED_CONFIG = "refused_config"  # missing config / no entry point
STATE_REFUSED_STOPPED = "refused_stopped"  # service stopping / host STOP wired in
STATE_FAILED_START = "failed_start"
STATE_RESTART_EXHAUSTED = "restart_exhausted"


# E41 section 3.4: transitions are INFO; restart-cap events WARN;
# failed starts ERROR. (Grant-off transitions are INFO -- stopping
# when the grant is off is fail-closed and correct, E41 section 2.1
# rule 2. A failed start always carries its cause in the message.)
_STATE_LEVELS = {
    STATE_STOPPED: ("INFO", "lifecycle.stopped"),
    STATE_STARTING: ("INFO", "lifecycle.starting"),
    STATE_RUNNING: ("INFO", "lifecycle.running"),
    STATE_DEGRADED_FLAG_OFF: ("INFO", "lifecycle.degraded_flag_off"),
    STATE_RESTARTING: ("INFO", "lifecycle.restarting"),
    STATE_REFUSED_GATE: ("INFO", "lifecycle.refused_gate"),
    STATE_REFUSED_FLAG: ("INFO", "lifecycle.refused_flag"),
    STATE_REFUSED_CONFIG: ("ERROR", "lifecycle.start_failed"),
    STATE_REFUSED_STOPPED: ("INFO", "lifecycle.refused_stopped"),
    STATE_FAILED_START: ("ERROR", "lifecycle.start_failed"),
    STATE_RESTART_EXHAUSTED: ("WARN", "lifecycle.restart_exhausted"),
}


class LifecycleRefused(RuntimeError):
    """Start/restart refused for a stated safety reason (never the token)."""


class LifecycleFailed(RuntimeError):
    """Start failed (entry point missing, health never answered, ...)."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskAgentLifecycle:
    """Owns the laptop-agent process lifetime for the Deskhand service.

    task_agent_dir: folder holding LaptopAgentV2.exe / laptop_agent_v2.py
        and agent_config.json (default DEFAULT_TASK_AGENT_DIR).
    base_url: agent HTTP base; loopback only -- the service runs on
        the PC, and the PC cannot reliably reach its own Tailscale IP.
    data_dir: where lifecycle.json / lifecycle.log live (service data
        dir). If None, state/log go to the log callable only.
    deskhand_grant_on: callable returning bool, or bool. The caller
        supplies Deskhand's Full PC access grant state. Default None
        means DENY (fail-closed).
    stopped: optional callable returning bool; True while the service
        (or host STOP, if wired) says stop. Start/restart refused then.
    health_fn: optional injectable health check (tests). Default is
        the real loopback /health GET.
    proc_factory: optional injectable (argv, kwargs) -> proc-like
        object with .pid/.poll()/.terminate()/.kill()/.wait() (tests).
    """

    def __init__(self, task_agent_dir=None, base_url=None, data_dir=None,
                 deskhand_grant_on=None, stopped=None,
                 poll_interval=DEFAULT_POLL_INTERVAL_SECS,
                 max_restarts=DEFAULT_MAX_RESTARTS,
                 restart_window_secs=DEFAULT_RESTART_WINDOW_SECS,
                 start_timeout=DEFAULT_START_TIMEOUT_SECS,
                 health_fn=None, proc_factory=None, log=None):
        self.task_agent_dir = Path(task_agent_dir or os.environ.get("DESKHAND_TASK_AGENT_DIR") or DEFAULT_TASK_AGENT_DIR)
        self.base_url = (base_url or DEFAULT_AGENT_BASE).rstrip("/")
        self.data_dir = Path(data_dir) if data_dir else None
        self._grant_source = deskhand_grant_on
        self._stopped = stopped
        self.poll_interval = poll_interval
        self.max_restarts = max_restarts
        self.restart_window_secs = restart_window_secs
        self.start_timeout = start_timeout
        self._health_fn = health_fn
        self._proc_factory = proc_factory
        self._log_fn = log or (lambda msg: None)
        self._lock = threading.RLock()
        # B2-D H15: a dedicated start guard so two concurrent
        # ensure_started() calls cannot start duplicate children. It
        # serializes starts only -- status()/close()/the monitor use
        # self._lock, which ensure_started() now releases during the
        # health wait, so they are never wedged behind a start.
        self._start_guard = threading.Lock()
        self._state = STATE_STOPPED
        self._proc = None
        self._proc_pid = None
        self._owned = False
        self._started_at = None
        self._last_error = None
        self._last_check = None
        self._last_health = None
        self._consecutive_failures = 0
        self._restart_times = []
        self._restart_deadline = None
        self._monitor_thread = None
        self._monitor_stop = threading.Event()
        self._log_file = None
        self._rotation_failure_logged = False
        if self.data_dir is not None:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self._log_file = open(self.data_dir / LIFECYCLE_LOG_NAME,
                                      "a", encoding="utf-8")
            except OSError:
                self._log_file = None

    # -- logging / state ----------------------------------------------
    def _log(self, msg, level="INFO", code=None):
        """Write one levelled plain-text line (E41 section 3.4).

        ``[ts] [LEVEL] [lifecycle/system] code -- msg``. Transitions
        are INFO, restart-cap events WARN, failed starts ERROR (call
        sites pass the level; this method defaults to INFO). The
        lifecycle log rotates (1 MB x 2, E17 section 2 #4) and the
        file handle is reopened after a rotation, so the next write
        lands in the NEW current file, not the renamed one. A
        rotation failure is logged once and the writer keeps
        appending to the current file.
        """
        if level not in LEVELS:
            level = "INFO"
        if code is None:
            if msg.startswith("lifecycle state -> "):
                code = "lifecycle.state"
            else:
                code = "lifecycle.event"
        line = format_plain_line(_now_iso(), level, "lifecycle",
                                 "system", code, msg)
        try:
            self._log_fn(line)
        except Exception:
            pass
        if self._log_file is not None:
            try:
                self._maybe_rotate_log()
                self._log_file.write(line + "\n")
                self._log_file.flush()
            except Exception:
                pass

    def _maybe_rotate_log(self):
        """Rotate the lifecycle log, reopening the handle (E17 V3).

        Called before each write, while the handle is still open.
        The handle is CLOSED before rotating: on Windows a rename
        of a file with an open handle fails (sharing violation), so
        rotate-while-open could never succeed there — the .1
        generation was never created and the current file grew
        unbounded (deploy-time fix, proven by
        test_v3_handle_reopen failing on the PC). After the
        rotation attempt the handle is reopened — on the new
        current file after a success, on the same file after a
        failure — so no write is ever lost. (A child started
        earlier keeps its own stdout handle into the .1 file; the
        diagnostics tail reads current + previous generation, so
        nothing is lost.)
        """
        if self.data_dir is None or self._log_file is None:
            return
        path = self.data_dir / LIFECYCLE_LOG_NAME
        try:
            if not path.is_file() or \
                    path.stat().st_size <= LIFECYCLE_LOG_MAX_BYTES:
                return
        except OSError:
            return
        try:
            self._log_file.close()
        except Exception:
            pass
        self._log_file = None
        rotated = rotate_if_needed(path, LIFECYCLE_LOG_MAX_BYTES,
                                   LIFECYCLE_LOG_BACKUP_COUNT)
        try:
            self._log_file = open(path, "a", encoding="utf-8")
        except OSError:
            self._log_file = None
        if not rotated:
            if not self._rotation_failure_logged:
                self._rotation_failure_logged = True
                failure = format_plain_line(
                    _now_iso(), "ERROR", "lifecycle", "system",
                    "lifecycle.rotation_failed",
                    "lifecycle log rotation failed; continuing to append")
                try:
                    self._log_fn(failure)
                except Exception:
                    pass
                if self._log_file is not None:
                    try:
                        self._log_file.write(failure + "\n")
                        self._log_file.flush()
                    except Exception:
                        pass

    def _save_state(self):
        if self.data_dir is None:
            return
        try:
            (self.data_dir / "task_agent_lifecycle.json").write_text(
                json.dumps(self.status(), indent=2), encoding="utf-8")
        except OSError:
            pass

    def _set_state(self, state, error=None):
        self._state = state
        if error is not None:
            self._last_error = str(error)[:300]
        level, code = _STATE_LEVELS.get(
            state, ("INFO", "lifecycle.state"))
        self._log("lifecycle state -> %s%s" % (
            state, (" : " + self._last_error) if self._last_error and state not in (STATE_RUNNING,) else ""),
            level=level, code=code)
        self._save_state()

    # -- gates ----------------------------------------------------------
    def _grant_on(self):
        g = self._grant_source
        if callable(g):
            try:
                return bool(g())
            except Exception:
                return False
        return bool(g) if g is not None else False

    def _is_stopped(self):
        if self._stopped is None:
            return False
        try:
            return bool(self._stopped())
        except Exception:
            return True  # fail-closed on a broken stop signal

    # -- health ---------------------------------------------------------
    def health(self):
        """Unauthenticated /health. Returns dict, or None if unreachable.

        No token involved: /health needs no auth.
        """
        if self._health_fn is not None:
            try:
                return self._health_fn()
            except Exception:
                return None
        try:
            with urllib.request.urlopen(self.base_url + "/health",
                                        timeout=HEALTH_TIMEOUT_SECS) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body if isinstance(body, dict) else None
        except Exception:
            return None

    def _health_ok(self):
        # B2-D H15: the HTTP probe runs outside self._lock (callers
        # hold no lock while waiting on it); only the small state
        # update takes the lock, so a slow /health cannot wedge
        # status() or close(). RLock-safe when called with it held.
        h = self.health()
        with self._lock:
            self._last_health = h
            self._last_check = _now_iso()
        return h if (h and h.get("ok")) else None

    # -- entry point ----------------------------------------------------
    def _entry_point(self):
        """(argv, kind) for the agent, or None if nothing startable.

        EXE preferred (that is what runs live on the PC); .py via the
        system python otherwise. agent_config.json must exist first --
        the agent refuses to start without a token, and this module
        never invents one.
        """
        cfg = self.task_agent_dir / "agent_config.json"
        if not cfg.is_file():
            return None, "agent_config.json not found in %s" % self.task_agent_dir
        exe = self.task_agent_dir / "LaptopAgentV2.exe"
        if exe.is_file():
            return [str(exe), "--config", str(cfg)], "exe"
        py = self.task_agent_dir / "laptop_agent_v2.py"
        if py.is_file():
            python = shutil.which("python") or sys.executable
            return [python, "-u", str(py), "--config", str(cfg)], "py"
        return None, "no agent entry point in %s (need LaptopAgentV2.exe or laptop_agent_v2.py)" % self.task_agent_dir

    def _start_owned(self):
        """Start the agent as an owned child. Returns (proc, pid)."""
        argv, kind = self._entry_point()
        if argv is None:
            raise LifecycleFailed(kind)
        kwargs = dict(cwd=str(self.task_agent_dir),
                      stdout=self._log_file,
                      stderr=subprocess.STDOUT,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if self._proc_factory is not None:
            proc = self._proc_factory(argv, kwargs)
        else:
            proc = subprocess.Popen(argv, **kwargs)
        self._proc = proc
        self._proc_pid = proc.pid
        self._owned = True
        self._started_at = _now_iso()
        close_job = None
        if _make_kill_job is not None and self._proc_factory is None:
            try:
                close_job = _make_kill_job(proc)
            except Exception:
                close_job = None
        self._close_job = close_job
        self._log("lifecycle started agent (%s) PID=%s cmd=%s" % (
            kind, proc.pid, " ".join(argv)),
            level="INFO", code="lifecycle.agent_started")
        return proc

    def _stop_owned_proc(self):
        """Terminate ONLY the owned child, by its explicit Popen handle.

        terminate -> wait 5s -> kill -> wait 5s. Never touches any
        other PID (in particular never an adopted agent).
        """
        proc, pid = self._proc, self._proc_pid
        self._proc = None
        self._proc_pid = None
        self._owned = False
        if getattr(self, "_close_job", None) is not None:
            try:
                self._close_job()
            except Exception:
                pass
            self._close_job = None
        if proc is None:
            return
        self._log("lifecycle stopping owned agent PID=%s" % pid,
                  level="INFO", code="lifecycle.agent_stopping")
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
        except Exception as e:
            self._log("lifecycle stop of PID=%s raised %s" % (pid, e),
                      level="ERROR", code="lifecycle.stop_failed")
        self._log("lifecycle owned agent PID=%s stopped" % pid,
                  level="INFO", code="lifecycle.agent_stopped")

    # -- public API -----------------------------------------------------
    def status(self):
        """Snapshot dict (safe to show on the panel / in a pad reply)."""
        with self._lock:
            proc = self._proc
            running = proc is not None and proc.poll() is None
            return {
                "state": self._state,
                "running": bool(running or self._state == STATE_RUNNING),
                "pid": self._proc_pid,
                "owned": self._owned,
                "health": bool(self._last_health and self._last_health.get("ok")),
                "remote_control_enabled": bool(
                    self._last_health and self._last_health.get("remote_control_enabled")),
                "restarts": len(self._restart_times),
                "last_error": self._last_error,
                "last_check": self._last_check,
                "started_at": self._started_at,
                "base_url": self.base_url,
                "task_agent_dir": str(self.task_agent_dir),
            }

    def ensure_started(self):
        """Start (or adopt) the agent if the gates allow. Idempotent.

        Returns status(). Raises LifecycleRefused / LifecycleFailed
        with a plain-language reason; neither ever contains a token.

        B2-D H15: the instance lock is held ONLY for brief state
        reads/writes, never across the up-to-30 s health-wait loop.
        The old code held ``self._lock`` for the whole method, which
        wedged the monitor thread, status() (panel/pad), and close()'s
        5 s monitor join for the entire start. The wait loop below
        polls the captured child proc and /health with no lock held
        and re-acquires it only to settle state. Concurrent starts
        are serialized by the dedicated ``_start_guard`` (which
        status()/close()/the monitor never take), and the monitor
        skips STATE_STARTING (see _poll_once) so it cannot restart a
        child whose start is still being awaited here.
        """
        with self._start_guard:
            with self._lock:
                if self._is_stopped():
                    self._set_state(STATE_REFUSED_STOPPED,
                                    "service is stopping; agent lifecycle refused")
                    raise LifecycleRefused("service is stopping; agent lifecycle refused")
                if not self._grant_on():
                    self._set_state(STATE_REFUSED_GATE,
                                    "Deskhand Full PC access is off; agent lifecycle refused")
                    raise LifecycleRefused(
                        "Deskhand Full PC access is off; agent lifecycle refused")
            health = self._health_ok()
            with self._lock:
                if health is not None:
                    # Already healthy: adopt (monitor-only), never duplicate.
                    if self._proc is not None and self._proc.poll() is None:
                        self._set_state(
                            STATE_RUNNING if health.get("remote_control_enabled")
                            else STATE_DEGRADED_FLAG_OFF)
                    else:
                        self._proc = None
                        self._proc_pid = None
                        self._owned = False
                        if self._started_at is None:
                            self._started_at = _now_iso()
                        self._set_state(
                            STATE_RUNNING if health.get("remote_control_enabled")
                            else STATE_DEGRADED_FLAG_OFF)
                        self._log("lifecycle adopted already-running agent "
                                  "(health ok, not started by Deskhand; "
                                  "Deskhand will monitor it but never kill it)",
                                  level="INFO", code="lifecycle.adopted")
                    self._consecutive_failures = 0
                    return self.status()
                # Down: start an owned child (the agent's own self-heal
                # probe reclaims a stale port holder at its startup).
                self._set_state(STATE_STARTING)
                try:
                    self._start_owned()
                except LifecycleFailed as e:
                    self._set_state(STATE_REFUSED_CONFIG, str(e))
                    raise
                proc = self._proc
                deadline = time.monotonic() + self.start_timeout
            # Health-wait loop WITHOUT the instance lock: status(),
            # the monitor, and close() all proceed while we wait.
            while time.monotonic() < deadline:
                if proc is not None and proc.poll() is not None:
                    err = "agent exited during startup (exit %s); see task_agent_lifecycle.log" % proc.poll()
                    with self._lock:
                        if self._proc is proc:
                            self._stop_owned_proc()
                        if self._state == STATE_STARTING:
                            self._set_state(STATE_FAILED_START, err)
                    raise LifecycleFailed(err)
                health = self._health_ok()
                if health is not None:
                    with self._lock:
                        self._consecutive_failures = 0
                        self._set_state(
                            STATE_RUNNING if health.get("remote_control_enabled")
                            else STATE_DEGRADED_FLAG_OFF)
                        return self.status()
                time.sleep(0.5)
            err = "agent did not answer /health within %ss" % self.start_timeout
            with self._lock:
                if self._proc is proc:
                    self._stop_owned_proc()
                if self._state == STATE_STARTING:
                    self._set_state(STATE_FAILED_START, err)
            raise LifecycleFailed(err)

    def ensure_stopped(self):
        """Stop the OWNED child only. An adopted agent is left alone.

        Returns status(). Safe to call when nothing is owned.
        """
        with self._lock:
            self._stop_owned_proc()
            self._set_state(STATE_STOPPED)
            return self.status()

    # -- monitor --------------------------------------------------------
    def _restart_budget_left(self):
        now = time.monotonic()
        self._restart_times = [t for t in self._restart_times
                               if now - t < self.restart_window_secs]
        return len(self._restart_times) < self.max_restarts

    def _begin_restart_attempt(self):
        """Start one owned child and arm its health deadline.

        Returns True if a child was started. Raises LifecycleFailed if
        the child cannot even be started (missing config/entry point).
        The attempt's outcome is judged on later polls (see
        _poll_restarting), NOT by blocking here: a monitor poll must
        never sit in a 30s wait loop, and a failed attempt must count
        against the budget instead of wedging the monitor in a
        terminal FAILED_START (that bug made the budget unreachable).
        """
        self._start_owned()
        self._restart_times.append(time.monotonic())
        self._restart_deadline = time.monotonic() + self.start_timeout
        self._consecutive_failures = 0
        self._set_state(STATE_RESTARTING,
                        "agent unhealthy; restart attempt started")
        return True

    def _restart_attempt_failed(self):
        """One restart attempt failed (child exited / deadline passed).

        Retries while budget remains, else stops and reports
        restart_exhausted. Returns status().
        """
        self._restart_deadline = None
        self._stop_owned_proc()
        if not self._restart_budget_left():
            self._set_state(STATE_RESTART_EXHAUSTED,
                            "restart budget exhausted (%d restarts in %ds); "
                            "manual ensure_started() needed" % (
                                self.max_restarts, self.restart_window_secs))
            return self.status()
        try:
            self._begin_restart_attempt()
        except LifecycleFailed as e:
            self._set_state(STATE_FAILED_START, str(e))
        return self.status()

    def _poll_restarting(self):
        """Judge an in-flight restart attempt. Returns status()."""
        if self._is_stopped():
            self._stop_owned_proc()
            self._set_state(STATE_REFUSED_STOPPED, "service is stopping")
            return self.status()
        if not self._grant_on():
            self._stop_owned_proc()
            self._set_state(STATE_REFUSED_GATE,
                            "Deskhand Full PC access turned off; owned agent stopped")
            return self.status()
        health = self._health_ok()
        if health is not None:
            self._restart_deadline = None
            self._consecutive_failures = 0
            self._set_state(
                STATE_RUNNING if health.get("remote_control_enabled")
                else STATE_DEGRADED_FLAG_OFF)
            self._last_error = None
            return self.status()
        proc = self._proc
        if proc is not None and proc.poll() is not None:
            self._log("lifecycle restart attempt failed: child exited "
                      "before /health answered",
                      level="ERROR", code="lifecycle.restart_failed")
            return self._restart_attempt_failed()
        if (self._restart_deadline is not None
                and time.monotonic() >= self._restart_deadline):
            self._log("lifecycle restart attempt failed: /health did not "
                      "answer within %ss" % self.start_timeout,
                      level="ERROR", code="lifecycle.restart_failed")
            return self._restart_attempt_failed()
        return self.status()

    def _poll_once(self):
        """One monitor pass. Returns status(). No-ops when stopped."""
        with self._lock:
            if self._state == STATE_RESTARTING:
                return self._poll_restarting()
            if self._state in (STATE_STOPPED, STATE_STARTING,
                               STATE_REFUSED_GATE,
                               STATE_REFUSED_CONFIG, STATE_REFUSED_STOPPED,
                               STATE_RESTART_EXHAUSTED, STATE_FAILED_START):
                # B2-D H15: STATE_STARTING is owned by ensure_started()'s
                # wait loop -- the monitor must not judge or restart a
                # child whose start is still being awaited there.
                return self.status()
            if self._is_stopped():
                self._stop_owned_proc()
                self._set_state(STATE_REFUSED_STOPPED, "service is stopping")
                return self.status()
            if not self._grant_on():
                # Grant off: stop what Deskhand started, refuse restarts.
                self._stop_owned_proc()
                self._set_state(STATE_REFUSED_GATE,
                                "Deskhand Full PC access turned off; owned agent stopped")
                return self.status()
            owned_alive = (self._proc is not None
                           and self._proc.poll() is None)
            health = self._health_ok()
            if health is not None:
                self._consecutive_failures = 0
                if not health.get("remote_control_enabled"):
                    self._set_state(STATE_DEGRADED_FLAG_OFF,
                                    "agent healthy but remote_control_enabled is off; "
                                    "enable it in agent_config.json and restart the agent")
                else:
                    self._set_state(STATE_RUNNING)
                    self._last_error = None
                return self.status()
            # Health failing.
            self._consecutive_failures += 1
            if self._consecutive_failures < 2:
                self._log("lifecycle health check failed (%d/2); waiting one more poll before restarting"
                          % self._consecutive_failures,
                          level="WARN", code="lifecycle.health_failed")
                return self.status()
            # Two consecutive failures, or owned child already exited:
            # restart (bounded, async -- judged on later polls).
            # Adopted agents are replaced by an owned child; owned
            # children are stopped first (explicit PID only).
            if not self._restart_budget_left():
                self._stop_owned_proc()
                self._set_state(STATE_RESTART_EXHAUSTED,
                                "restart budget exhausted (%d restarts in %ds); "
                                "manual ensure_started() needed" % (
                                    self.max_restarts, self.restart_window_secs))
                return self.status()
            was_owned = self._owned
            self._stop_owned_proc()
            self._log("lifecycle restarting agent (was %s)" % (
                "owned PID" if was_owned else "adopted/external"),
                level="INFO", code="lifecycle.restarting")
            try:
                self._begin_restart_attempt()
            except LifecycleFailed as e:
                self._set_state(STATE_FAILED_START, str(e))
            return self.status()

    def start_monitoring(self):
        """Start the background monitor thread (idempotent)."""
        with self._lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(target=self._monitor_loop,
                                                    daemon=True)
            self._monitor_thread.start()
            self._log("lifecycle monitor started (poll every %ss)" % self.poll_interval,
                      level="INFO", code="lifecycle.monitor_started")

    def _monitor_loop(self):
        while not self._monitor_stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                self._log("lifecycle monitor poll failed: %s" % e,
                          level="WARN", code="lifecycle.poll_failed")
            self._monitor_stop.wait(self.poll_interval)

    def stop_monitoring(self):
        self._monitor_stop.set()
        t = self._monitor_thread
        if t is not None:
            t.join(timeout=5)
        self._monitor_thread = None

    def close(self):
        """Stop monitoring and stop the owned child. Adopted agents live on."""
        self.stop_monitoring()
        with self._lock:
            self._stop_owned_proc()
            self._set_state(STATE_STOPPED)
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
