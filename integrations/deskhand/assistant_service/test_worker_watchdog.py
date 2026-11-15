#!/usr/bin/env python3
"""VM tests for Deskhand 100.037 worker-disconnected watchdog (E12).

Run: python3 test_worker_watchdog.py   (exit 0 = all pass)
Or under pytest in the verify overlay (tests/ dir, PYTHONPATH=<overlay>/service).

No PC, no network beyond loopback, no token. Three layers:
  pure watchdog — deskhand_worker_watchdog.WorkerWatchdog against a
    fake clock + fake reconnect (no service, no I/O);
  pure failfast — deskhand_failfast.muse_worker_refusal / inflight_trip
    with a muse_worker signal, against fake snapshots;
  service wiring — the patched-service loader (same pattern as
    test_failfast.py: sibling modules stubbed, __init__ bypassed) for
    the gate hook, the in-flight trip, the worker_reconnect op, the
    runtime_stream worker source, the event/journal sinks, and the
    1 s evaluation tick; plus one real-ChatService construction test
    for the timer thread (overlay only — skipped standalone).

Coverage map (task Gate 3):
  W-T1  fire after 3 consecutive not-ready reads; disconnected_since
        is the FIRST miss; 1-2 misses do not fire; ready resets misses
  W-T2  backoff schedule: delays 2s/5s/10s/30s/60s after attempts 1-5
  W-T3  cap: stops after 10 failed attempts; detection continues
        (a later ready read + clean reconcile still recovers)
  W-T4  stop-early: grant-off stops after the first failed attempt;
        re-arms only on positive grant-on evidence (never on unknown)
  W-T5  stop-early: reconcile persistently failing with the bridge
        healthy stops after 3 failed attempts (not when unknown/down)
  W-T6  no-replay: a dispatching turn is marked uncertain on the trip
        and is never re-sent on reconnect
  W-T7  recovery verification: a stale ready flag alone is NOT a
        recovery (ensure_chat_ready raising blocks it); clean
        reconcile + ready True recovers
  W-T8  gate: muse_worker_refusal carries E12's verbatim message
  W-T9  trip: inflight_trip trips a chat turn on the Muse-worker
        signal without a snapshot; agent turns never trip on it
  W-T10 service gate hook: refuse-while-down before dispatcher.submit,
        journaled as failfast_refusal path muse_worker; refused turn
        never sent; new turn accepted after recovery
  W-T11 manual op: one immediate reconcile, single-flight (a press
        while an attempt is in flight joins it), works after the cap;
        the op return is FLAT top-level keys (D1 fix — the shape
        MainForm.ApplyWorkerReconnectResult parses), no nested
        attempt/watchdog grouping
  W-T12 runtime_stream sources map gains a "worker" key
        (ok / disconnected / missing)
  W-T13 episode record: worker.* events in vm_events.jsonl + the
        audit journal (component "watchdog"), with the §9 fields
  W-T14 service tick: three 1 s ticks fire the watchdog; the next
        tick attempts; a ready tick verifies and recovers
  W-T15 real ChatService construction: the 1 s watchdog thread exists
        (daemon, named "worker-watchdog"); interval=None disables it

STRUCK — recorded here, NOT written as passing tests:
  STOP pause (design §3.4 / old-spec test 7) — MOOT. No STOP latch
    exists in the live tree (0 stop_latched hits; see spec §12.8/R4).
    Nothing to pause on, nothing to test.
"""
import sys
import tempfile
import threading
import time
import types
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = HERE.parent / "service"
sys.path.insert(0, str(SERVICE))

import deskhand_worker_watchdog as wwd
import deskhand_failfast as ff

try:
    import deskhand_diagnostics as diag
except ImportError:
    # Standalone runs (work/service holds only the changed files): the
    # tests only need make_row + COMPONENT_KEYS, so a minimal stub
    # stands in. The real module is used whenever it is beside this
    # file (verify overlay).
    class _DiagStub:
        COMPONENT_KEYS = ["windows_app", "service", "gateway", "worker",
                          "relay", "tunnel", "node", "agent", "bridge",
                          "grant", "muse", "lifecycle", "phone"]

        @staticmethod
        def make_row(status, last_ok, version="", error=""):
            return {"status": status, "last_ok": last_ok,
                    "version": version, "error": error}

    diag = _DiagStub()

try:
    import deskhand_service
    REAL_SERVICE = True
except ImportError:
    deskhand_service = None
    REAL_SERVICE = False

JOURNAL_AVAILABLE = (SERVICE / "deskhand_task_agent_op.py").is_file()

RESULTS = []  # (item, name, ok, detail)

NOW = 1792345678  # fixed epoch for deterministic HH:MM(:SS) in messages


def check(item, name, cond, detail=""):
    RESULTS.append((item, name, bool(cond), detail))
    if not cond:
        print("FAIL [%s] %s %s" % (item, name, detail))


def item_ok(item):
    return all(r[2] for r in RESULTS if r[0] == item)


def hhmmss(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def hhmm(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClock:
    """Injectable clock: time() and monotonic() move together."""

    def __init__(self, start=NOW, mono=1000.0):
        self.now = float(start)
        self.mono = float(mono)

    def time(self):
        return self.now

    def monotonic(self):
        return self.mono

    def advance(self, secs):
        self.now += secs
        self.mono += secs


class FakeReconnect:
    """reconnect(kind) -> (ok, error); outcomes pop from a queue."""

    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or [])

    def __call__(self, kind):
        self.calls.append(kind)
        if self.outcomes:
            return self.outcomes.pop(0)
        return False, "fake reconnect failed"

    def queue(self, *outcomes):
        self.outcomes.extend(outcomes)


class BlockingReconnect:
    """Blocks inside the reconcile until released (single-flight test)."""

    def __init__(self, result=(True, None)):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = result

    def __call__(self, kind):
        self.calls.append(kind)
        self.entered.set()
        self.release.wait(5)
        return self.result


def make_watchdog(reconnect=None, clock=None):
    clock = clock or FakeClock()
    events = []
    wd = wwd.WorkerWatchdog(reconnect or FakeReconnect(),
                            on_event=events.append,
                            clock=clock.time, monotonic=clock.monotonic)
    return wd, events, clock


def fire(wd, clock, cause=None, grant_on=None):
    """Drive three consecutive not-ready reads; returns the fire time."""
    t0 = clock.now
    for _ in range(3):
        wd.observe(False, cause=cause, grant_on=grant_on, now=clock.now,
                   now_mono=clock.mono)
    return t0


def types_of(events):
    return [e["type"] for e in events]


# ---------------------------------------------------------------------------
# W-T1: fire rule
# ---------------------------------------------------------------------------

def test_W_T1():
    wd, events, clock = make_watchdog()
    check("W-T1", "not fired initially", wd.state()["fired"] is False)
    check("W-T1", "1 miss: no fire, no event",
          wd.observe(False, now=clock.now, now_mono=clock.mono) is None
          and wd.state()["fired"] is False and events == [])
    check("W-T1", "1 miss counted", wd.state()["misses"] == 1)
    check("W-T1", "2 misses: still no fire",
          wd.observe(False, now=clock.now, now_mono=clock.mono) is None
          and wd.state()["fired"] is False and events == [])
    clock.advance(1)
    wd.observe(False, now=clock.now, now_mono=clock.mono)
    state = wd.state()
    check("W-T1", "3rd miss fires", state["fired"] is True)
    check("W-T1", "disconnected_since is the FIRST miss, not the third",
          state["disconnected_since"] == NOW, repr(state["disconnected_since"]))
    check("W-T1", "one worker_disconnected event",
          types_of(events) == ["worker_disconnected"], repr(types_of(events)))
    ev = events[0]["detail"]
    check("W-T1", "event carries disconnected_since/signal/cause",
          ev.get("disconnected_since") == NOW
          and ev.get("signal") == "muse_status"
          and ev.get("cause") == "unknown", repr(ev))
    # No second fire on further misses.
    wd.observe(False, now=clock.now, now_mono=clock.mono)
    check("W-T1", "further misses do not re-fire",
          types_of(events) == ["worker_disconnected"])
    # A ready read resets the miss counter and asks for verification.
    check("W-T1", "ready read while down asks for verification",
          wd.observe(True, now=clock.now, now_mono=clock.mono) is True)
    check("W-T1", "still fired until verified",
          wd.state()["fired"] is True)
    check("W-T1", "miss counter reset by the ready read",
          wd.state()["misses"] == 0)
    assert item_ok("W-T1")


# ---------------------------------------------------------------------------
# W-T2: backoff schedule
# ---------------------------------------------------------------------------

def test_W_T2():
    wd, events, clock = make_watchdog()
    fire(wd, clock)
    check("W-T2", "attempt 1 is due immediately on fire",
          wd.attempt_due(now_mono=clock.mono) is True)
    expected = [2.0, 5.0, 10.0, 30.0, 60.0, 60.0]
    seen = []
    for n in (1, 2, 3, 4, 5, 6):
        result = wd.attempt("auto")
        check("W-T2", "attempt %d runs and fails" % n,
              result["ran"] is True and result["ok"] is False
              and result["attempt"] == n, repr(result))
        delay = wd.state()["next_delay_s"]
        seen.append(delay)
        check("W-T2", "attempt %d schedules the right next delay" % n,
              delay == expected[n - 1], repr(delay))
        check("W-T2", "not due before the delay",
              wd.attempt_due(now_mono=clock.mono) is False)
        clock.advance(delay)
        check("W-T2", "due after the delay",
              wd.attempt_due(now_mono=clock.mono) is True)
    check("W-T2", "delays are 2/5/10/30/60/60",
          seen == expected, repr(seen))
    attempt_events = [e for e in events
                      if e["type"] == "worker_reconnect_attempt"]
    check("W-T2", "one attempt event per attempt",
          len(attempt_events) == 6, repr(len(attempt_events)))
    check("W-T2", "attempt events carry attempt/kind/next_delay_s",
          all(e["detail"].get("attempt") == i + 1
              and e["detail"].get("kind") == "auto"
              and e["detail"].get("next_delay_s") == expected[i]
              for i, e in enumerate(attempt_events)),
          repr([e["detail"] for e in attempt_events]))
    assert item_ok("W-T2")


# ---------------------------------------------------------------------------
# W-T3: cap + detection continues
# ---------------------------------------------------------------------------

def test_W_T3():
    wd, events, clock = make_watchdog()
    fire(wd, clock)
    auto = 0
    while wd.attempt_due(now_mono=clock.mono):
        result = wd.attempt("auto")
        auto += 1
        delay = wd.state()["next_delay_s"]
        if delay is not None:
            clock.advance(delay)
        if auto > 20:
            break
    state = wd.state()
    check("W-T3", "exactly 10 failed auto attempts",
          auto == 10, repr(auto))
    check("W-T3", "stopped_reason is cap", state["stopped_reason"] == "cap",
          repr(state["stopped_reason"]))
    check("W-T3", "no attempt due after the cap",
          wd.attempt_due(now_mono=clock.mono) is False)
    stopped = [e for e in events
               if e["type"] == "worker_auto_reconnect_stopped"]
    check("W-T3", "one stopped event, attempts=10, reason=cap",
          len(stopped) == 1
          and stopped[0]["detail"].get("attempts") == 10
          and stopped[0]["detail"].get("reason") == "cap",
          repr(stopped))
    # Detection never stops: a later ready read still verifies.
    check("W-T3", "ready read after the cap still asks for verification",
          wd.observe(True, now=clock.now, now_mono=clock.mono) is True)
    wd._reconnect.queue((True, None))
    result = wd.attempt("auto")
    check("W-T3", "clean reconcile after the cap recovers",
          result["ok"] is True and result["recovered"] is True, repr(result))
    state = wd.state()
    check("W-T3", "connected again after recovery",
          state["connected"] is True and state["fired"] is False)
    rec = [e for e in events if e["type"] == "worker_reconnected"]
    check("W-T3", "worker_reconnected carries down_seconds + time",
          len(rec) == 1
          and rec[0]["detail"].get("down_seconds", -1) > 0
          and rec[0]["detail"].get("reconnected_at") == clock.now,
          repr(rec))
    assert item_ok("W-T3")


# ---------------------------------------------------------------------------
# W-T4: grant-off stop-early + re-arm
# ---------------------------------------------------------------------------

def test_W_T4():
    wd, events, clock = make_watchdog()
    fire(wd, clock, cause="grant_off", grant_on=False)
    check("W-T4", "fired with cause grant_off",
          wd.state()["cause"] == "grant_off")
    result = wd.attempt("auto")
    check("W-T4", "first failed attempt still runs",
          result["attempt"] == 1 and result["ok"] is False)
    state = wd.state()
    check("W-T4", "grant-off stops after the first failed attempt",
          state["stopped_reason"] == "grant-off", repr(state["stopped_reason"]))
    check("W-T4", "no attempt due after grant-off stop",
          wd.attempt_due(now_mono=clock.mono) is False)
    check("W-T4", "no second auto attempt ran",
          len([e for e in events
               if e["type"] == "worker_reconnect_attempt"]) == 1)
    # An unknown cause does NOT re-arm: only positive grant-on does.
    wd.observe(False, cause=None, grant_on=None, now=clock.now,
               now_mono=clock.mono)
    check("W-T4", "unknown cause does not re-arm",
          wd.state()["stopped_reason"] == "grant-off")
    # The grant observed back on re-arms, with a fresh budget.
    wd.observe(False, cause="tunnel_down", grant_on=True, now=clock.now,
               now_mono=clock.mono)
    state = wd.state()
    check("W-T4", "grant back on re-arms auto-reconnect",
          state["stopped_reason"] is None and state["attempts"] == 0,
          repr(state))
    check("W-T4", "attempt due immediately after re-arm",
          wd.attempt_due(now_mono=clock.mono) is True)
    rearm = [e for e in events
             if e["type"] == "worker_auto_reconnect_rearmed"]
    check("W-T4", "re-arm notification delivered (journal-only type)",
          len(rearm) == 1 and rearm[0]["type"] not in wwd.STREAM_EVENT_TYPES)
    assert item_ok("W-T4")


# ---------------------------------------------------------------------------
# W-T5: reconcile-failing stop-early
# ---------------------------------------------------------------------------

def test_W_T5():
    # Bridge healthy: stops after 3 failed attempts.
    wd, events, clock = make_watchdog()
    fire(wd, clock)
    healthy_calls = []
    healthy = lambda: healthy_calls.append(1) or True
    for _ in range(3):
        wd.attempt("auto", bridge_healthy=healthy)
        clock.advance(wd.state()["next_delay_s"] or 0)
    check("W-T5", "bridge-healthy stop after 3 failed attempts",
          wd.state()["stopped_reason"] == "reconcile-failing",
          repr(wd.state()["stopped_reason"]))
    check("W-T5", "bridge health consulted lazily (only at attempt 3)",
          len(healthy_calls) == 1, repr(len(healthy_calls)))
    # Bridge unknown: no stop at 3 — runs to the cap instead.
    wd2, _, clock2 = make_watchdog()
    fire(wd2, clock2)
    for _ in range(3):
        wd2.attempt("auto")
        clock2.advance(wd2.state()["next_delay_s"] or 0)
    check("W-T5", "unknown bridge health does not stop at 3",
          wd2.state()["stopped_reason"] is None
          and wd2.state()["attempts"] == 3)
    # Bridge down: no stop at 3 either.
    wd3, _, clock3 = make_watchdog()
    fire(wd3, clock3)
    for _ in range(3):
        wd3.attempt("auto", bridge_healthy=False)
        clock3.advance(wd3.state()["next_delay_s"] or 0)
    check("W-T5", "bridge-down does not stop at 3",
          wd3.state()["stopped_reason"] is None
          and wd3.state()["attempts"] == 3)
    assert item_ok("W-T5")


# ---------------------------------------------------------------------------
# Pure failfast: W-T8 (gate) + W-T9 (trip)
# ---------------------------------------------------------------------------

def test_W_T8():
    signal = {"down": True, "since": NOW, "cause": "grant_off"}
    refusal = ff.muse_worker_refusal(signal)
    check("W-T8", "refusal returned for a down worker",
          refusal is not None, repr(refusal))
    check("W-T8", "verbatim E12 message",
          refusal is not None
          and refusal["message"] == ("Worker disconnected since %s \u2014 your "
                                     "message was not sent" % hhmm(NOW)),
          repr(refusal))
    check("W-T8", "path is muse_worker (distinct from the relay worker row)",
          refusal is not None and refusal["path"] == "muse_worker")
    check("W-T8", "dead_since is the disconnect time",
          refusal is not None and refusal["dead_since"] == NOW)
    check("W-T8", "cause passes through",
          refusal is not None and refusal["cause"] == "grant_off")
    check("W-T8", "up worker -> no refusal",
          ff.muse_worker_refusal({"down": False, "since": NOW}) is None)
    check("W-T8", "no signal -> no refusal",
          ff.muse_worker_refusal(None) is None)
    unknown = ff.muse_worker_refusal({"down": True, "since": None})
    check("W-T8", "unknown since stays honest, never 00:00",
          unknown is not None and "unknown" in unknown["message"]
          and unknown["dead_since"] is None, repr(unknown))
    assert item_ok("W-T8")


def _row(status="OK", last_ok=NOW - 60, version="", error=""):
    return diag.make_row(status, last_ok, version, error)


def _snap(**overrides):
    comps = {key: _row() for key in diag.COMPONENT_KEYS}
    comps["gateway"] = _row("UNKNOWN", 0, "", "not checked yet")
    comps.update(overrides)
    return {"ok": True, "as_of": NOW, "as_of_human": "2026-11-10 02:00:00",
            "components": comps}


def test_W_T9():
    signal = {"down": True, "since": NOW - 100, "cause": None}
    turn = {"turn_id": "t1", "agent_task_id": None}
    trip, reason = ff.inflight_trip(None, turn, now=NOW, muse_worker=signal)
    check("W-T9", "chat turn trips on the Muse signal with NO snapshot",
          trip is True, repr((trip, reason)))
    check("W-T9", "reason names the disconnect time + never replayed",
          reason is not None and hhmmss(NOW - 100) in reason
          and "never replayed" in reason, repr(reason))
    agent = {"turn_id": "t2", "agent_task_id": "task-9"}
    trip, _ = ff.inflight_trip(None, agent, now=NOW, muse_worker=signal)
    check("W-T9", "agent turn never trips on the Muse signal",
          trip is False)
    trip, _ = ff.inflight_trip(_snap(), turn, now=NOW, muse_worker=None)
    check("W-T9", "healthy snapshot, no Muse signal -> no trip",
          trip is False)
    trip, reason = ff.inflight_trip(
        _snap(worker=_row("DOWN", NOW - 3600, "", "worker disconnected")),
        turn, now=NOW, muse_worker=None)
    check("W-T9", "relay worker DOWN still trips (existing path intact)",
          trip is True and reason is not None
          and hhmmss(NOW - 3600) in reason, repr(reason))
    assert item_ok("W-T9")


# ---------------------------------------------------------------------------
# Service-level wiring (patched service, stubbed siblings)
# ---------------------------------------------------------------------------

class _Route:
    def __init__(self, assistant, model, account, effort):
        self.assistant = assistant
        self.model = model
        self.account = account
        self.effort = effort

    def __eq__(self, other):
        return (isinstance(other, _Route) and
                (self.assistant, self.model, self.account, self.effort) ==
                (other.assistant, other.model, other.account, other.effort))

    def __hash__(self):
        return hash((self.assistant, self.model, self.account, self.effort))


class _FakeDispatcher:
    def __init__(self):
        self.submits = []
        self.adapters = {}
        self._running = None

    def admission_ticket(self):
        return "ticket-1"

    def submit(self, turn_id, text, **kw):
        self.submits.append({"turn_id": turn_id, "text": text, **kw})
        return {"id": turn_id, "status": "queued"}

    def admission_state(self):
        return {"stopped": False, "epoch": 0}

    def running_turn_info(self):
        return self._running


class _FakeBox:
    def __init__(self):
        self.uncertain_calls = []
        self._uncertain_exc = None

    def recent(self, limit=100):
        return []

    def get(self, turn_id):
        return None

    def uncertain(self, turn_id, error=None):
        if self._uncertain_exc is not None:
            raise self._uncertain_exc
        self.uncertain_calls.append((turn_id, error))


class _FakeMuse:
    """MuseService-shaped fake: status() + ensure_chat_ready()."""

    def __init__(self, ready=True):
        self.ready = ready
        self.ensure_error = None
        self.ensure_calls = []

    def status(self):
        return {"browser": "fixture", "bridge": {"enabled": True},
                "https_connection": {"ready": self.ready, "error": None,
                                     "paused": False}}

    def ensure_chat_ready(self):
        self.ensure_calls.append(time.time())
        if self.ensure_error is not None:
            raise RuntimeError(self.ensure_error)
        if not self.ready:
            raise RuntimeError(
                "Muse connection is not ready: HTTPS endpoint is connecting.")

    def recent_file_activity(self, limit=8):
        return []


def _load_patched_service():
    created = []
    for name, attrs in {
        "deskhand_dispatch": ["Dispatcher", "Reply", "StopConfirmed"],
        "deskhand_mailbox": ["Mailbox"],
        "deskhand_codex": ["CodexChat", "catalog"],
        "deskhand_host_rpc": ["HostRpc"],
        "deskhand_agent_session": ["run_agent_session"],
        "deskhand_local": ["LocalChat"],
        "deskhand_local_tools": ["LocalToolAdapter", "LocalToolTransport"],
        "deskhand_openai_local": ["OpenAILocalChat", "OpenAILocalToolTransport"],
        "deskhand_runtime": ["ManagedRuntime"],
        "deskhand_cloud_tools": ["HostToolChat"],
        "deskhand_router": ["classify", "suggest_route"],
    }.items():
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        for attr in attrs:
            setattr(module, attr, object)
        sys.modules[name] = module
        created.append(name)
    # Route needs value semantics (the hook builds a fresh Route from
    # the command and looks it up in dispatcher.adapters) — but only
    # on a freshly stubbed module. When the real deskhand_dispatch is
    # already loaded (full-suite runs), the service already bound the
    # real Route at ITS import; overwriting the module attribute then
    # would not change the service's binding and would only confuse
    # later tests, so it is left alone. Adapters below are keyed with
    # the service-bound Route either way (see _adapter_key).
    if "deskhand_dispatch" in created:
        sys.modules["deskhand_dispatch"].Route = _Route
    import deskhand_service as svc_module
    return svc_module


def _adapter_key(svc_module):
    """The Route class handle() itself uses (its own import binding).

    Keying the fake dispatcher's adapters with this class is what
    keeps the submit path from building a CodexChat adapter — in
    standalone runs it is the stub _Route, in the overlay it is the
    real Route. Either way it is exactly what handle() constructs.
    """
    return svc_module.Route("muse", "muse", "deskhand", "")


def _make_service(tmp, dispatcher, box):
    svc_module = _load_patched_service()
    svc = svc_module.ChatService.__new__(svc_module.ChatService)
    svc.data = Path(tmp)
    svc.host_rpc = None
    svc.bridge_config = Path(tmp) / "deskhand_bridge_config.json"
    svc.bridge_proc = None
    svc.task_agent_lifecycle = None
    svc.stopping = threading.Event()
    svc.wake = threading.Event()
    svc.dispatcher = dispatcher
    svc.box = box
    svc.muse = None
    svc.muse_route = None
    svc.routes = [{"assistant": "muse", "model": "muse", "account": "deskhand",
                   "effort": "", "efforts": [""], "name": "Muse"}]
    svc.local_routes = {}
    svc.local_origins = {}
    svc.fixture = False
    svc._health_last_ok = {}
    svc._health_cache = None
    svc._health_cache_lock = threading.Lock()
    svc._failfast_tripped = set()
    svc.worker_watchdog = None
    svc.worker_watchdog_interval = 1.0
    svc._worker_watchdog_thread = None
    return svc


def _fresh_snapshot(**overrides):
    s = _snap(**overrides)
    s["as_of"] = time.time()
    return s


def _submit_cmd(turn_id, text="hello"):
    return {"op": "submit", "id": turn_id, "text": text,
            "assistant": "muse", "model": "muse", "account": "deskhand",
            "effort": ""}


def _attach_watchdog(svc, muse, clock=None):
    """Attach a real watchdog driving the service's reconnect path."""
    clock = clock or FakeClock()
    wd = wwd.WorkerWatchdog(reconnect=svc._worker_watchdog_reconnect,
                            on_event=svc._worker_watchdog_event,
                            clock=clock.time, monotonic=clock.monotonic)
    svc.worker_watchdog = wd
    svc.muse = muse
    return wd, clock


def _vm_events(tmp):
    path = Path(tmp) / "vm_events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _journal_lines(tmp):
    path = Path(tmp) / "task_agent_fallback.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# W-T6: no-replay (trip marks uncertain; reconnect never re-sends)
# ---------------------------------------------------------------------------

def test_W_T6():
    with tempfile.TemporaryDirectory() as tmp:
        dispatcher = _FakeDispatcher()
        box = _FakeBox()
        svc = _make_service(tmp, dispatcher, box)
        dispatcher.adapters[_adapter_key(sys.modules["deskhand_service"])] = (
            lambda request: None)
        muse = _FakeMuse(ready=False)
        wd, clock = _attach_watchdog(svc, muse)
        svc._health_cache = {"snapshot": _fresh_snapshot(),
                             "as_of": time.time()}
        # A turn is dispatching when the worker drops.
        dispatcher._running = {"turn_id": "t-dispatch",
                               "agent_task_id": None, "route": "muse"}
        fire(wd, clock)
        svc._failfast_trip_check()
        check("W-T6", "dispatching turn marked uncertain on the trip",
              len(box.uncertain_calls) == 1
              and box.uncertain_calls[0][0] == "t-dispatch",
              repr(box.uncertain_calls))
        check("W-T6", "reason names the disconnect + never replayed",
              box.uncertain_calls
              and "worker disconnected at" in box.uncertain_calls[0][1]
              and "never replayed" in box.uncertain_calls[0][1],
              repr(box.uncertain_calls))
        # The trip is once-only: a second check does not re-mark.
        svc._failfast_trip_check()
        check("W-T6", "trip does not re-mark the same turn",
              len(box.uncertain_calls) == 1)
        # Recovery: the turn is NOT re-sent.
        muse.ready = True
        svc._worker_watchdog_tick()
        check("W-T6", "worker recovered after a clean reconcile",
              wd.state()["connected"] is True)
        check("W-T6", "the uncertain turn was never re-sent",
              dispatcher.submits == [], repr(dispatcher.submits))
        check("W-T6", "no replay path exists in the watchdog",
              not hasattr(wd, "replay") and not hasattr(wd, "resubmit"))
    assert item_ok("W-T6")


# ---------------------------------------------------------------------------
# W-T7: recovery verification (stale ready flag alone is not recovery)
# ---------------------------------------------------------------------------

def test_W_T7():
    with tempfile.TemporaryDirectory() as tmp:
        dispatcher = _FakeDispatcher()
        box = _FakeBox()
        svc = _make_service(tmp, dispatcher, box)
        muse = _FakeMuse(ready=False)
        wd, clock = _attach_watchdog(svc, muse)
        svc._health_cache = {"snapshot": _fresh_snapshot(),
                             "as_of": time.time()}
        fire(wd, clock)
        # The status flag now reads ready — but the reconcile fails.
        muse.ready = True
        muse.ensure_error = "Muse connection is not ready: last reconcile failed."
        svc._worker_watchdog_tick()
        check("W-T7", "stale ready + failed reconcile is NOT a recovery",
              wd.state()["fired"] is True, repr(wd.state()["fired"]))
        check("W-T7", "no worker_reconnected event on the failed verify",
              "worker_reconnected" not in types_of(_vm_events(tmp)))
        # Clean reconcile + ready True: verified recovery.
        muse.ensure_error = None
        svc._worker_watchdog_tick()
        check("W-T7", "clean reconcile + ready True recovers",
              wd.state()["connected"] is True)
        check("W-T7", "worker_reconnected event emitted",
              "worker_reconnected" in types_of(_vm_events(tmp)))
    assert item_ok("W-T7")


# ---------------------------------------------------------------------------
# W-T10: service gate hook
# ---------------------------------------------------------------------------

def test_W_T10():
    with tempfile.TemporaryDirectory() as tmp:
        dispatcher = _FakeDispatcher()
        box = _FakeBox()
        svc = _make_service(tmp, dispatcher, box)
        dispatcher.adapters[_adapter_key(sys.modules["deskhand_service"])] = (
            lambda request: None)
        muse = _FakeMuse(ready=False)
        wd, clock = _attach_watchdog(svc, muse)
        svc._health_cache = {"snapshot": _fresh_snapshot(),
                             "as_of": time.time()}
        # Healthy worker: submit accepted.
        out = svc.handle(_submit_cmd("turn-1"))
        check("W-T10", "healthy submit accepted",
              out.get("status") == "queued", repr(out))
        # Worker down: refused BEFORE dispatcher.submit().
        fire(wd, clock)
        refused = None
        try:
            svc.handle(_submit_cmd("turn-2", "refused text"))
        except RuntimeError as error:
            refused = str(error)
        check("W-T10", "refused while down",
              refused is not None, repr(refused))
        check("W-T10", "refusal is the verbatim E12 message",
              refused is not None
              and refused.startswith("Worker disconnected since ")
              and refused.endswith("your message was not sent"),
              repr(refused))
        check("W-T10", "refused turn never entered the dispatcher",
              [s["turn_id"] for s in dispatcher.submits] == ["turn-1"],
              repr(dispatcher.submits))
        feed = _vm_events(tmp)
        refusals = [e for e in feed if e.get("type") == "failfast_refusal"]
        check("W-T10", "refusal journaled in the runtime feed",
              len(refusals) == 1, repr(feed))
        check("W-T10", "journal line names the muse_worker path + turn",
              refusals and refusals[0]["detail"].get("path") == "muse_worker"
              and refusals[0]["detail"].get("turn_id") == "turn-2",
              repr(refusals))
        # Recovery re-arm: a NEW turn is accepted; the refused turn is
        # NOT re-sent (it stays a draft for Phil).
        muse.ready = True
        svc._worker_watchdog_tick()
        check("W-T10", "worker recovered",
              wd.state()["connected"] is True)
        out = svc.handle(_submit_cmd("turn-3", "new text after recovery"))
        check("W-T10", "new turn accepted after recovery",
              out.get("status") == "queued", repr(out))
        check("W-T10", "refused draft NOT sent automatically on recovery",
              [s["turn_id"] for s in dispatcher.submits] == ["turn-1", "turn-3"],
              repr(dispatcher.submits))
    assert item_ok("W-T10")


# ---------------------------------------------------------------------------
# W-T11: manual op (single-flight, works after the cap)
# ---------------------------------------------------------------------------

def test_W_T11():
    with tempfile.TemporaryDirectory() as tmp:
        dispatcher = _FakeDispatcher()
        box = _FakeBox()
        svc = _make_service(tmp, dispatcher, box)
        muse = _FakeMuse(ready=False)
        wd, clock = _attach_watchdog(svc, muse)
        svc._health_cache = {"snapshot": _fresh_snapshot(),
                             "as_of": time.time()}
        # No muse at all: the op raises like its muse_status sibling.
        svc.muse = None
        try:
            svc.handle({"op": "worker_reconnect"})
            raised = None
        except RuntimeError as error:
            raised = str(error)
        check("W-T11", "no Muse attached -> op raises",
              raised is not None and "not enabled" in raised, repr(raised))
        svc.muse = muse
        fire(wd, clock)
        out = svc.handle({"op": "worker_reconnect"})
        check("W-T11", "manual attempt runs one reconcile immediately",
              out["ran"] is True
              and out["kind"] == "manual"
              and out["ok"] is False, repr(out))
        check("W-T11", "op returns the watchdog state, FLAT (D1 fix: "
              "no nested attempt/watchdog grouping)",
              "watchdog" not in out
              and isinstance(out.get("attempt"), int)
              and out["attempt"] == 1
              and out["attempts"] == 1
              and out["disconnected_since"] == clock.now,
              repr(out))
        check("W-T11", "flat shape matches MainForm's parse "
              "(attempts/since/next-delay/cause + recovery signals)",
              out["attempts"] == 1
              and out["next_delay_s"] == 2.0
              and out["cause"] is None
              and out["ready"] is False
              and out["reconcile_ok"] is False
              and out["status"] == "disconnected"
              and out["auto_reconnect_stopped"] is False
              and out["stopped_reason"] is None, repr(out))
        check("W-T11", "not reconnected while the worker is still down",
              out["reconnected"] is False)
        # Single-flight: a second press while one is in flight joins it.
        blocker = BlockingReconnect(result=(True, None))
        wd2 = wwd.WorkerWatchdog(reconnect=blocker,
                                 on_event=svc._worker_watchdog_event,
                                 clock=clock.time, monotonic=clock.monotonic)
        svc.worker_watchdog = wd2
        fire(wd2, clock)
        results = []
        t = threading.Thread(target=lambda: results.append(wd2.manual_attempt()))
        t.start()
        check("W-T11", "first manual attempt entered the reconcile",
              blocker.entered.wait(5))
        joined = wd2.manual_attempt()
        blocker.release.set()
        t.join(5)
        check("W-T11", "in-flight attempt ran exactly once",
              len(blocker.calls) == 1, repr(blocker.calls))
        check("W-T11", "joiner got the runner's outcome, ran=False",
              joined["ran"] is False and joined["ok"] is True, repr(joined))
        check("W-T11", "runner recovered",
              results and results[0]["ran"] is True
              and results[0]["recovered"] is True, repr(results))
        # After the cap, the manual button still works.
        wd3, _, clock3 = make_watchdog()
        wd3._reconnect = svc._worker_watchdog_reconnect
        svc.worker_watchdog = wd3
        svc.muse = _FakeMuse(ready=False)
        svc._health_cache = {"snapshot": _fresh_snapshot(),
                             "as_of": time.time()}
        fire(wd3, clock3)
        for _ in range(10):
            wd3.attempt("auto")
            clock3.advance(wd3.state()["next_delay_s"] or 0)
        check("W-T11", "cap reached in the manual-after-cap setup",
              wd3.state()["stopped_reason"] == "cap")
        out = svc.handle({"op": "worker_reconnect"})
        check("W-T11", "manual attempt still runs after the cap",
              out["ran"] is True
              and out["kind"] == "manual", repr(out))
        check("W-T11", "manual failure after the cap leaves auto stopped",
              wd3.state()["stopped_reason"] == "cap")
        check("W-T11", "flat stopped flags after the cap",
              out["auto_reconnect_stopped"] is True
              and out["stopped_reason"] == "cap"
              and out["next_delay_s"] is None
              and out["status"] == "disconnected", repr(out))
    assert item_ok("W-T11")


# ---------------------------------------------------------------------------
# W-T12: runtime_stream sources worker key
# ---------------------------------------------------------------------------

def test_W_T12():
    with tempfile.TemporaryDirectory() as tmp:
        dispatcher = _FakeDispatcher()
        box = _FakeBox()
        svc = _make_service(tmp, dispatcher, box)
        stream = svc.runtime_stream()
        check("W-T12", "no Muse attached -> worker source is missing",
              stream["sources"].get("worker") == "missing",
              repr(stream["sources"]))
        muse = _FakeMuse(ready=True)
        wd, clock = _attach_watchdog(svc, muse)
        stream = svc.runtime_stream()
        check("W-T12", "worker up -> worker source is ok",
              stream["sources"].get("worker") == "ok",
              repr(stream["sources"]))
        fire(wd, clock)
        stream = svc.runtime_stream()
        check("W-T12", "worker down -> worker source is disconnected",
              stream["sources"].get("worker") == "disconnected",
              repr(stream["sources"]))
        check("W-T12", "other sources untouched by the new key",
              set(stream["sources"]) == {"chat", "bridge", "vm_events", "worker"},
              repr(stream["sources"]))
    assert item_ok("W-T12")


# ---------------------------------------------------------------------------
# W-T13: episode record (stream events + audit journal)
# ---------------------------------------------------------------------------

def test_W_T13():
    with tempfile.TemporaryDirectory() as tmp:
        dispatcher = _FakeDispatcher()
        box = _FakeBox()
        svc = _make_service(tmp, dispatcher, box)
        muse = _FakeMuse(ready=False)
        wd, clock = _attach_watchdog(svc, muse)
        svc._health_cache = {"snapshot": _fresh_snapshot(),
                             "as_of": time.time()}
        t0 = fire(wd, clock, cause="tunnel_down")
        wd.attempt("auto")  # attempt 1, fails
        muse.ready = True
        wd.attempt("auto")  # attempt 2, verifies recovery
        feed = _vm_events(tmp)
        seq = types_of(feed)
        check("W-T13", "episode events in order",
              seq == ["worker_disconnected", "worker_reconnect_attempt",
                      "worker_reconnect_attempt", "worker_reconnected"],
              repr(seq))
        disc = feed[0]
        check("W-T13", "disconnected event is a worker source WARN",
              disc.get("source") == "worker" and disc.get("level") == "WARN",
              repr(disc))
        check("W-T13", "disconnected detail: since/signal/cause",
              disc["detail"].get("disconnected_since") == t0
              and disc["detail"].get("signal") == "muse_status"
              and disc["detail"].get("cause") == "tunnel_down",
              repr(disc["detail"]))
        att = feed[1]
        check("W-T13", "attempt event: n of 10, kind, next delay",
              att["detail"].get("attempt") == 1
              and att["detail"].get("max_attempts") == 10
              and att["detail"].get("kind") == "auto"
              and att["detail"].get("next_delay_s") == 2.0,
              repr(att["detail"]))
        rec = feed[3]
        check("W-T13", "reconnected detail: down_seconds + time",
              rec["detail"].get("down_seconds") == 0
              and rec["detail"].get("reconnected_at") == clock.now,
              repr(rec["detail"]))
        check("W-T13", "event summaries carry no token-shaped data",
              all("x" * 40 not in (e.get("summary") or "")
                  for e in feed))
        if JOURNAL_AVAILABLE:
            journal = _journal_lines(tmp)
            codes = [e.get("code") for e in journal]
            check("W-T13", "audit journal has the episode, component watchdog",
                  len(journal) == 4
                  and all(e.get("component") == "watchdog" for e in journal),
                  repr(codes))
            check("W-T13", "journal codes match the event types",
                  codes == ["worker.disconnected", "worker.reconnect_attempt",
                            "worker.reconnect_attempt", "worker.reconnected"],
                  repr(codes))
        else:
            print("W-T13: journal assertions skipped (no task_agent_op module)")
    assert item_ok("W-T13")


# ---------------------------------------------------------------------------
# W-T14: service tick drives fire + attempt + verify
# ---------------------------------------------------------------------------

def test_W_T14():
    with tempfile.TemporaryDirectory() as tmp:
        dispatcher = _FakeDispatcher()
        box = _FakeBox()
        svc = _make_service(tmp, dispatcher, box)
        muse = _FakeMuse(ready=False)
        wd, clock = _attach_watchdog(svc, muse)
        svc._health_cache = {"snapshot": _fresh_snapshot(),
                             "as_of": time.time()}
        svc._worker_watchdog_tick()
        svc._worker_watchdog_tick()
        check("W-T14", "two ticks: still only suspect, not fired",
              wd.state()["fired"] is False
              and types_of(_vm_events(tmp)) == [])
        svc._worker_watchdog_tick()
        check("W-T14", "third tick fires (3-miss rule) and runs attempt 1",
              wd.state()["fired"] is True
              and types_of(_vm_events(tmp)) == ["worker_disconnected",
                                                "worker_reconnect_attempt"])
        check("W-T14", "attempt 1 failed (worker still down)",
              wd.state()["attempts"] == 1
              and wd.state()["next_delay_s"] == 2.0)
        svc._worker_watchdog_tick()
        check("W-T14", "next tick does nothing before the 2 s delay",
              types_of(_vm_events(tmp)) == ["worker_disconnected",
                                            "worker_reconnect_attempt"])
        muse.ready = True
        svc._worker_watchdog_tick()
        check("W-T14", "ready tick verifies and recovers",
              wd.state()["connected"] is True
              and "worker_reconnected" in types_of(_vm_events(tmp)))
    assert item_ok("W-T14")


# ---------------------------------------------------------------------------
# W-T15: real ChatService construction (overlay only)
# ---------------------------------------------------------------------------

def test_W_T15():
    if not REAL_SERVICE:
        print("W-T15 skipped: no real service tree (standalone run)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        svc = deskhand_service.ChatService(tmp, fixture=True)
        try:
            check("W-T15", "watchdog wired on construction",
                  svc.worker_watchdog is not None)
            thread = svc._worker_watchdog_thread
            check("W-T15", "1 s evaluation thread exists and runs",
                  thread is not None and thread.is_alive()
                  and thread.daemon is True
                  and thread.name == "worker-watchdog",
                  repr(thread))
            check("W-T15", "evaluation interval is 1.0 s (O4 default)",
                  svc.worker_watchdog_interval == 1.0)
        finally:
            svc.close()
        svc2 = deskhand_service.ChatService(
            tmp, fixture=True, worker_watchdog_interval=None)
        try:
            check("W-T15", "interval=None disables the timer thread",
                  svc2._worker_watchdog_thread is None
                  and svc2.worker_watchdog is not None)
        finally:
            svc2.close()
    assert item_ok("W-T15")


def main():
    tests = [test_W_T1, test_W_T2, test_W_T3, test_W_T4, test_W_T5,
             test_W_T8, test_W_T9, test_W_T6, test_W_T7, test_W_T10,
             test_W_T11, test_W_T12, test_W_T13, test_W_T14, test_W_T15]
    for fn in tests:
        before = len(RESULTS)
        try:
            fn()
        except Exception as error:  # noqa: BLE001 - report, do not hide
            RESULTS.append((fn.__name__, "unexpected exception", False,
                            repr(error)))
            print("FAIL [%s] unexpected exception: %r"
                  % (fn.__name__, error))
        if len(RESULTS) == before:
            print("note: %s recorded no checks" % fn.__name__)
    failed = [r for r in RESULTS if not r[2]]
    counts = {}
    for item, _name, ok, _detail in RESULTS:
        total, passed = counts.get(item, (0, 0))
        counts[item] = (total + 1, passed + (1 if ok else 0))
    print("\n--- test_worker_watchdog.py ---")
    for item in sorted(counts):
        total, passed = counts[item]
        print("item %s: %d/%d" % (item, passed, total))
    print("TOTAL: %d/%d checks passed" % (
        len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("FAILURES:")
        for item, name, _ok, detail in failed:
            print("  [%s] %s %s" % (item, name, detail))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
