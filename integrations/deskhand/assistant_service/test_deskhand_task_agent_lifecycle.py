"""Tests for deskhand_task_agent_lifecycle.py (Deskhand-owned agent lifecycle).

Runs entirely in the VM. No PC, no real token, no network beyond
loopback (one real fake-agent HTTP server for the health() path).
Fake processes stand in for the agent EXE/.py child; the lifecycle
never kills a real PID in these tests.

Run: python3 test_deskhand_task_agent_lifecycle.py
Exit 0 = all checks passed.
"""

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deskhand_task_agent_lifecycle import (  # noqa: E402
    LifecycleFailed,
    LifecycleRefused,
    TaskAgentLifecycle,
    STATE_DEGRADED_FLAG_OFF,
    STATE_FAILED_START,
    STATE_REFUSED_CONFIG,
    STATE_REFUSED_GATE,
    STATE_RESTART_EXHAUSTED,
    STATE_RUNNING,
    STATE_STOPPED,
)

TEST_TOKEN = "test-token-for-lifecycle-tests-only-not-real"

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print("[PASS] %s" % name)
    except Exception as e:
        FAILED.append(name)
        print("[FAIL] %s -- %r" % (name, e))


# --- fakes ------------------------------------------------------------

class FakeProc:
    """Popen stand-in. exit_code None = alive; set to exit."""
    _next_pid = 90000

    def __init__(self, argv, kwargs):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.argv = argv
        self.kwargs = kwargs
        self._exit = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = -15

    def kill(self):
        self.killed = True
        self._exit = -9

    def wait(self, timeout=None):
        if self._exit is None:
            self._exit = -15
        return self._exit


class FakeAgentServer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"ok": True, "server": "laptop-agent-v2",
                               "remote_control_enabled": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def start_fake_http():
    srv = HTTPServer(("127.0.0.1", 0), FakeAgentServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def make_dir(with_exe=True, with_py=True, with_config=True):
    d = Path(tempfile.mkdtemp())
    if with_exe:
        (d / "LaptopAgentV2.exe").write_bytes(b"fake-exe")
    if with_py:
        (d / "laptop_agent_v2.py").write_text("# fake agent\n")
    if with_config:
        (d / "agent_config.json").write_text(json.dumps(
            {"token": TEST_TOKEN, "host": "127.0.0.1", "port": 8899,
             "remote_control_enabled": True}))
    return d


def make_lifecycle(d, health=None, grant=True, stopped=False,
                   start_timeout=5, poll_interval=30, max_restarts=5,
                   restart_window_secs=3600, data_dir=True):
    procs = []

    def factory(argv, kwargs):
        p = FakeProc(argv, kwargs)
        procs.append(p)
        return p

    lc = TaskAgentLifecycle(
        task_agent_dir=str(d), data_dir=str(d / "data") if data_dir else None,
        deskhand_grant_on=(lambda: grant) if isinstance(grant, bool) else grant,
        stopped=(lambda: stopped) if isinstance(stopped, bool) else stopped,
        poll_interval=poll_interval, max_restarts=max_restarts,
        restart_window_secs=restart_window_secs, start_timeout=start_timeout,
        health_fn=health, proc_factory=factory)
    lc._test_procs = procs
    return lc


def health_ok(flag=True):
    return lambda: {"ok": True, "server": "laptop-agent-v2",
                    "remote_control_enabled": flag}


def health_down():
    return lambda: None


# --- tests ------------------------------------------------------------

def t_grant_off_refuses_and_never_starts():
    d = make_dir()
    lc = make_lifecycle(d, health=health_down(), grant=False)
    try:
        lc.ensure_started()
        raise AssertionError("should have refused")
    except LifecycleRefused as e:
        assert "Full PC access is off" in str(e)
    assert lc._test_procs == [], "factory must not be called when grant off"
    assert lc.status()["state"] == STATE_REFUSED_GATE
    lc.close()


def t_stopped_refuses():
    d = make_dir()
    lc = make_lifecycle(d, health=health_down(), stopped=True)
    try:
        lc.ensure_started()
        raise AssertionError("should have refused")
    except LifecycleRefused as e:
        assert "stopping" in str(e)
    assert lc._test_procs == []
    lc.close()


def t_adopts_healthy_agent_no_duplicate():
    d = make_dir()
    lc = make_lifecycle(d, health=health_ok())
    st = lc.ensure_started()
    assert st["state"] == STATE_RUNNING, st
    assert st["owned"] is False, st
    assert st["pid"] is None, st  # adopted PID is unknown; never guessed
    assert lc._test_procs == [], "must not start a duplicate"
    # Second call is idempotent, still no duplicate.
    lc.ensure_started()
    assert lc._test_procs == []
    lc.close()


def t_flag_off_is_degraded_not_restart():
    d = make_dir()
    lc = make_lifecycle(d, health=health_ok(flag=False))
    st = lc.ensure_started()
    assert st["state"] == STATE_DEGRADED_FLAG_OFF, st
    assert lc._test_procs == []
    lc._poll_once()  # monitor must NOT restart a flag-off agent
    assert lc._test_procs == []
    assert lc.status()["state"] == STATE_DEGRADED_FLAG_OFF
    lc.close()


def t_starts_owned_when_down_prefers_exe():
    d = make_dir()
    state = {"up": False}

    def health():
        return ({"ok": True, "server": "laptop-agent-v2",
                 "remote_control_enabled": True} if state["up"] else None)
    lc = make_lifecycle(d, health=health)
    orig = lc._proc_factory

    def factory(argv, kwargs):
        p = orig(argv, kwargs)
        state["up"] = True  # agent answers /health once started
        return p
    lc._proc_factory = factory
    st = lc.ensure_started()
    assert st["state"] == STATE_RUNNING, st
    assert st["owned"] is True and st["pid"] is not None, st
    assert "LaptopAgentV2.exe" in lc._test_procs[0].argv[0], lc._test_procs[0].argv
    lc.close()
    assert lc._test_procs[0].terminated, "close() must terminate the owned child"


def t_py_fallback_when_no_exe():
    d = make_dir(with_exe=False)
    state = {"up": False}

    def health():
        return ({"ok": True, "server": "laptop-agent-v2",
                 "remote_control_enabled": True} if state["up"] else None)
    lc = make_lifecycle(d, health=health)
    orig = lc._proc_factory

    def factory(argv, kwargs):
        p = orig(argv, kwargs)
        state["up"] = True
        return p
    lc._proc_factory = factory
    st = lc.ensure_started()
    assert st["state"] == STATE_RUNNING, st
    assert "laptop_agent_v2.py" in lc._test_procs[0].argv[2], lc._test_procs[0].argv
    lc.close()


def t_missing_config_refuses():
    d = make_dir(with_config=False)
    lc = make_lifecycle(d, health=health_down())
    try:
        lc.ensure_started()
        raise AssertionError("should have failed")
    except LifecycleFailed as e:
        assert "agent_config.json" in str(e)
    assert lc._test_procs == []
    assert lc.status()["state"] == STATE_REFUSED_CONFIG
    lc.close()


def t_no_entry_point_refuses():
    d = make_dir(with_exe=False, with_py=False)
    lc = make_lifecycle(d, health=health_down())
    try:
        lc.ensure_started()
        raise AssertionError("should have failed")
    except LifecycleFailed as e:
        assert "no agent entry point" in str(e).lower()
    assert lc._test_procs == []
    lc.close()


def t_start_timeout_kills_child_and_fails():
    d = make_dir()
    lc = make_lifecycle(d, health=health_down(), start_timeout=1)
    try:
        lc.ensure_started()
        raise AssertionError("should have timed out")
    except LifecycleFailed as e:
        assert "did not answer /health" in str(e)
    assert lc.status()["state"] == STATE_FAILED_START
    assert lc._test_procs[0].terminated, "timed-out child must be terminated"
    lc.close()


def t_child_exit_during_startup_fails():
    d = make_dir()
    lc = make_lifecycle(d, health=health_down(), start_timeout=5)
    orig = lc._proc_factory

    def factory(argv, kwargs):
        p = orig(argv, kwargs)
        p._exit = 1  # dies immediately
        return p
    lc._proc_factory = factory
    try:
        lc.ensure_started()
        raise AssertionError("should have failed")
    except LifecycleFailed as e:
        assert "exited during startup" in str(e)
    assert lc.status()["state"] == STATE_FAILED_START
    lc.close()


def t_monitor_restarts_owned_after_exit():
    d = make_dir()
    state = {"up": True}
    lc = make_lifecycle(d, health=lambda: (
        {"ok": True, "server": "laptop-agent-v2",
         "remote_control_enabled": True} if state["up"] else None))
    lc.ensure_started()  # adopts (healthy)
    # Adopted agent dies: health down + no owned proc.
    state["up"] = False
    lc._poll_once()  # 1st failure: wait, no restart yet
    assert lc._test_procs == []
    # 2nd failure: restart as owned child; the new child answers
    # /health once started (factory flips the flag), judged on the
    # next poll.
    orig = lc._proc_factory

    def factory(argv, kwargs):
        p = orig(argv, kwargs)
        state["up"] = True
        return p
    lc._proc_factory = factory
    lc._poll_once()
    assert len(lc._test_procs) == 1, lc._test_procs
    assert lc.status()["state"] == "restarting", lc.status()
    lc._poll_once()
    assert lc.status()["state"] == STATE_RUNNING
    assert lc.status()["owned"] is True
    lc.close()


def t_monitor_single_failure_does_not_restart():
    d = make_dir()
    state = {"up": True}
    lc = make_lifecycle(d, health=lambda: (
        {"ok": True, "server": "laptop-agent-v2",
         "remote_control_enabled": True} if state["up"] else None))
    lc.ensure_started()
    state["up"] = False
    lc._poll_once()
    assert lc._test_procs == [], "one failed poll must not restart (flap guard)"
    lc.close()


def t_monitor_restart_budget_exhausted():
    d = make_dir()
    lc = make_lifecycle(d, health=health_down(), max_restarts=2,
                        restart_window_secs=3600, start_timeout=1)
    # Force into a running state with an owned child that keeps dying:
    # start via ensure_started would fail (health never answers), so
    # drive _poll_once from a faked running state instead. Each
    # restart attempt runs async: poll to trigger it, sleep past its
    # 1s health deadline, poll to judge it failed, repeat.
    lc._state = STATE_RUNNING
    lc._proc = FakeProc(["fake"], {})
    lc._proc_pid = lc._proc.pid
    lc._owned = True
    for _ in range(12):
        lc._poll_once()
        if lc.status()["state"] == STATE_RESTART_EXHAUSTED:
            break
        time.sleep(1.2)
    assert lc.status()["state"] == STATE_RESTART_EXHAUSTED, lc.status()
    assert len(lc._test_procs) <= 2, lc._test_procs
    lc.close()


def t_monitor_grant_off_stops_owned():
    d = make_dir()
    state = {"up": False, "grant": True}
    lc = make_lifecycle(d, health=lambda: (
        {"ok": True, "server": "laptop-agent-v2",
         "remote_control_enabled": True} if state["up"] else None),
        grant=lambda: state["grant"])
    orig = lc._proc_factory

    def factory(argv, kwargs):
        p = orig(argv, kwargs)
        state["up"] = True
        return p
    lc._proc_factory = factory
    lc.ensure_started()
    assert lc.status()["owned"] is True
    state["grant"] = False
    lc._poll_once()
    assert lc.status()["state"] == STATE_REFUSED_GATE
    assert lc._test_procs[0].terminated, "grant-off must stop the owned child"
    lc.close()


def t_ensure_stopped_leaves_adopted_alone():
    d = make_dir()
    lc = make_lifecycle(d, health=health_ok())
    lc.ensure_started()  # adopted
    st = lc.ensure_stopped()
    assert st["state"] == STATE_STOPPED
    assert lc._test_procs == [], "nothing owned, nothing to kill"
    lc.close()


def t_ensure_started_idempotent_when_owned():
    d = make_dir()
    state = {"up": False}
    lc = make_lifecycle(d, health=lambda: (
        {"ok": True, "server": "laptop-agent-v2",
         "remote_control_enabled": True} if state["up"] else None))
    orig = lc._proc_factory

    def factory(argv, kwargs):
        p = orig(argv, kwargs)
        state["up"] = True
        return p
    lc._proc_factory = factory
    lc.ensure_started()
    lc.ensure_started()
    assert len(lc._test_procs) == 1, "second ensure_started must not start a duplicate"
    lc.close()


def t_state_file_has_no_token():
    d = make_dir()
    lc = make_lifecycle(d, health=health_ok())
    lc.ensure_started()
    state_path = d / "data" / "task_agent_lifecycle.json"
    assert state_path.is_file(), "state file must exist"
    text = state_path.read_text()
    assert TEST_TOKEN not in text, "token must never appear in state"
    doc = json.loads(text)
    assert doc["state"] == STATE_RUNNING and doc["owned"] is False
    log_text = (d / "data" / "task_agent_lifecycle.log").read_text()
    assert TEST_TOKEN not in log_text
    lc.close()


def t_real_health_via_fake_http_server():
    srv, base = start_fake_http()
    d = make_dir()
    lc = TaskAgentLifecycle(task_agent_dir=str(d), base_url=base,
                            deskhand_grant_on=lambda: True)
    h = lc.health()
    assert h is not None and h["ok"] is True
    assert h["remote_control_enabled"] is True
    srv.shutdown()
    assert lc.health() is None, "health must be None when the server is gone"
    lc.close()


def t_monitor_thread_runs_and_stops():
    d = make_dir()
    lc = make_lifecycle(d, health=health_ok(), poll_interval=0.2)
    lc.ensure_started()
    lc.start_monitoring()
    time.sleep(0.6)
    assert lc._monitor_thread is not None and lc._monitor_thread.is_alive()
    lc.close()
    assert lc._monitor_thread is None or not lc._monitor_thread.is_alive()


def t_status_answers_during_start_wait():
    # B2-D H15: ensure_started() used to hold the instance lock across
    # the whole up-to-30 s health wait, wedging status() (panel/pad),
    # the monitor, and close()'s join. status() must answer promptly
    # while a start is still waiting on /health.
    d = make_dir()
    lc = make_lifecycle(d, health=health_down(), start_timeout=3)
    errors = []

    def start():
        try:
            lc.ensure_started()
        except LifecycleFailed:
            pass
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    thread = threading.Thread(target=start, daemon=True)
    thread.start()
    try:
        time.sleep(0.5)  # let the start enter its health-wait loop
        assert thread.is_alive(), "start finished too early"
        before = time.monotonic()
        status = lc.status()
        waited = time.monotonic() - before
        assert waited < 1.0, "status() blocked %.2fs during start wait" % waited
        assert status["state"] == "starting", status
    finally:
        thread.join(timeout=10)
        lc.close()
    assert not errors, errors


def t_default_grant_is_deny():
    d = make_dir()
    lc = TaskAgentLifecycle(task_agent_dir=str(d), health_fn=health_down(),
                            proc_factory=lambda a, k: FakeProc(a, k))
    try:
        lc.ensure_started()
        raise AssertionError("no grant callable must deny by default")
    except LifecycleRefused:
        pass
    lc.close()


CHECKS = [
    ("grant-off refuses and never starts", t_grant_off_refuses_and_never_starts),
    ("stopped refuses", t_stopped_refuses),
    ("adopts healthy agent, no duplicate", t_adopts_healthy_agent_no_duplicate),
    ("flag-off is degraded, never restarted", t_flag_off_is_degraded_not_restart),
    ("starts owned child when down, prefers EXE", t_starts_owned_when_down_prefers_exe),
    ("py fallback when no EXE", t_py_fallback_when_no_exe),
    ("missing config refuses", t_missing_config_refuses),
    ("no entry point refuses", t_no_entry_point_refuses),
    ("start timeout kills child and fails", t_start_timeout_kills_child_and_fails),
    ("child exit during startup fails", t_child_exit_during_startup_fails),
    ("monitor restarts owned after exit", t_monitor_restarts_owned_after_exit),
    ("single failed poll does not restart", t_monitor_single_failure_does_not_restart),
    ("restart budget exhausts", t_monitor_restart_budget_exhausted),
    ("grant-off stops owned child", t_monitor_grant_off_stops_owned),
    ("ensure_stopped leaves adopted alone", t_ensure_stopped_leaves_adopted_alone),
    ("ensure_started idempotent when owned", t_ensure_started_idempotent_when_owned),
    ("state file has no token", t_state_file_has_no_token),
    ("real /health via fake HTTP server", t_real_health_via_fake_http_server),
    ("monitor thread runs and stops", t_monitor_thread_runs_and_stops),
    ("status answers during start wait", t_status_answers_during_start_wait),
    ("default grant is deny", t_default_grant_is_deny),
]

if __name__ == "__main__":
    for name, fn in CHECKS:
        check(name, fn)
    print("\n%d/%d checks passed" % (len(PASSED), len(CHECKS)))
    if FAILED:
        print("FAILED: %s" % ", ".join(FAILED))
        sys.exit(1)
    print("ALL CHECKS PASSED")
