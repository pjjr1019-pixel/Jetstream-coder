"""Muse-worker disconnect watchdog (100.037, E12).

Pure state machine + single-flight attempt driver for the Muse worker
transport. It owns detection, the backoff schedule, the cap, the
stop-early conditions, and the episode record (via the ``on_event``
callback); it never touches a process, a tunnel, a grant, or any STOP
flag — there is no STOP latch in this tree (verified 100.037 Step 0),
so there is nothing to read, record, or reintroduce.

Detection (spec §5.1): the service calls ``observe(ready, cause,
grant_on)`` once per evaluation tick with the live
``MuseConnection.status()`` ready flag (signal 1) — which is also signal 2, "last good
``muse_status`` ready read", because no heartbeat exists in the live
tree. The diagnostics "worker" row is the RELAY/phone link, never
the Muse worker, and is never used here. Fire only after
``MISS_THRESHOLD`` consecutive not-ready reads; ``disconnected_since``
is the time of the FIRST miss, not the third.

Reconnect (spec §6): the watchdog drives ``reconcile()`` through the
injected ``reconnect`` callable — directly, or via
``MuseService.ensure_chat_ready()`` — and never calls a force-restart
directly. A force-restart only happens inside ``reconcile()`` when
the wedged threshold is met, under the live dispatcher idle guard;
uncertain turns never block reconnect (§6, R7). Recovery requires
both a clean reconcile AND ready afterwards (§5.4, H4): the
``reconnect`` callable must return ``(True, None)`` only when both
held — a stale ready flag alone is never a recovery.

Backoff (§6.1): attempt 1 immediately on fire, then 2 s / 5 s /
10 s / 30 s / 60 s; never faster than 2 s apart (matches
``ENSURE_RECONCILE_THROTTLE_SECS = 2.0``). Cap (§6.2): automatic
reconnect stops after ``MAX_ATTEMPTS`` failed attempts; detection
never stops. Stop-early (§6.3): grant-off (or grant-paused) stops
after the first failed attempt and re-arms when the grant returns;
reconcile persistently failing with the bridge healthy stops after
``RECONCILE_FAILING_STOP_ATTEMPTS`` failed attempts.

Events (spec §10.4): ``worker_disconnected`` (on fire),
``worker_reconnect_attempt`` (each attempt), ``worker_auto_reconnect_stopped``
(on cap / stop-early), ``worker_reconnected`` (on verified recovery).
One extra notification, ``worker_auto_reconnect_rearmed``, is delivered
to the same callback when a grant stop-early re-arms; it is journal-only,
never a stream event (the next attempt event shows the resumption).
Event ``detail`` carries only summaries — no token, file contents, or
screenshot data.

``reconnect`` signature: ``reconnect(kind) -> (ok, error)`` where
``kind`` is ``"auto"`` or ``"manual"``. Concurrent ``attempt()``
callers join the in-flight reconcile and receive its outcome; only
the runner records an attempt. A manual attempt runs one reconcile
immediately, outside the backoff schedule — including after the cap,
when it is the only way to retry — but it never re-arms automatic
reconnect and never counts toward the cap trigger (the cap stops
*automatic* attempts).

Threading: all state is guarded by one lock; the ``reconnect`` call
itself runs outside it. ``clock`` / ``monotonic`` are injectable for
tests (defaults: ``time.time`` / ``time.monotonic``).
"""
import threading
import time

# Fire only after this many consecutive not-ready reads (spec §5.1).
MISS_THRESHOLD = 3
# Automatic reconnect stops after this many failed attempts (spec §6.2).
MAX_ATTEMPTS = 10
# Delays BEFORE attempts 2..6+ (attempt 1 is immediate on fire).
# attempt 2 after 2 s, attempt 3 after 5 s, attempt 4 after 10 s,
# attempt 5 after 30 s, attempt 6+ after 60 s each (spec §6.1).
BACKOFF_DELAYS = (2.0, 5.0, 10.0, 30.0, 60.0)
# Causes that make retrying futile until the cause changes (spec §6.3).
GRANT_STOP_CAUSES = ("grant_off", "grant_paused")
# Reconcile persistently failing with the bridge healthy stops after
# this many failed attempts (spec §6.3, grant-off-equivalent).
RECONCILE_FAILING_STOP_ATTEMPTS = 3
# How long a concurrent attempt caller waits for the in-flight
# reconcile before giving up (matches RECONCILE_SINGLE_FLIGHT_WAIT_SECS).
SINGLE_FLIGHT_WAIT_SECS = 10.0

# The four stream event types (spec §10.4). The re-arm notification
# below them is journal-only and is never a stream event.
STREAM_EVENT_TYPES = frozenset({
    "worker_disconnected",
    "worker_reconnect_attempt",
    "worker_auto_reconnect_stopped",
    "worker_reconnected",
})


def _fmt_hhmmss(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def _fmt_duration(seconds):
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs


class WorkerWatchdog:
    """Muse-worker disconnect watchdog state machine."""

    def __init__(self, reconnect, on_event=None, clock=None, monotonic=None):
        self._reconnect = reconnect
        self._on_event = on_event
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._flight_cond = threading.Condition(threading.Lock())
        self._in_flight = False
        self._flight_result = None  # result dict of the last finished attempt
        # Detection state.
        self._misses = 0
        self._first_miss_at = None
        self._last_good_at = None
        self._fired = False
        self._disconnected_since = None
        self._cause = None
        self._signal = None
        # Episode state.
        self._attempts = 0  # failed attempts in this episode (auto + manual)
        self._last_attempt_at = None
        self._next_attempt_at = None  # monotonic; None when nothing scheduled
        self._stopped_reason = None

    # -- events ---------------------------------------------------------
    def _emit(self, event):
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            pass

    # -- detection ------------------------------------------------------
    def observe(self, ready, cause=None, grant_on=None, now=None, now_mono=None):
        """Record one evaluation tick.

        ``ready`` is the live ``MuseConnection.status()['ready']`` flag;
        ``cause`` is the last known cause (``grant_off`` / ``grant_paused`` /
        ``tunnel_down`` / ``None`` = unknown) or None when unknown;
        ``grant_on`` is True / False / None — positive evidence about
        the grant, used only to re-arm a grant stop-early (an unknown
        cause never re-arms: only the grant being observed back ON
        does). Returns True when the caller should run a verification
        attempt (a ready read while disconnected — a stale ready flag
        alone is never a recovery, §5.4); otherwise None. Fires and
        re-arms are delivered through the ``on_event`` callback.
        """
        now = self._clock() if now is None else now
        now_mono = self._monotonic() if now_mono is None else now_mono
        with self._lock:
            if ready:
                self._misses = 0
                self._last_good_at = now
                if self._fired:
                    return True
                return None
            # Not ready: count the miss; the first miss sets the
            # episode start (the UI shows the first-miss time, §7.1).
            self._misses += 1
            if self._misses == 1:
                self._first_miss_at = now
            if cause is not None:
                self._cause = cause
            rearmed = self._maybe_rearm_locked(grant_on, now_mono)
            if self._misses >= MISS_THRESHOLD and not self._fired:
                self._fired = True
                self._disconnected_since = self._first_miss_at
                self._signal = "muse_status"
                self._attempts = 0
                self._stopped_reason = None
                self._next_attempt_at = now_mono  # attempt 1: immediate
                event = {
                    "type": "worker_disconnected",
                    "level": "WARN",
                    "code": "worker.disconnected",
                    "outcome": "failed",
                    "ts": now,
                    "summary": "Worker disconnected since %s"
                               % _fmt_hhmmss(self._disconnected_since),
                    "detail": {
                        "disconnected_since": self._disconnected_since,
                        "signal": self._signal,
                        "cause": self._cause or "unknown",
                    },
                }
                self._emit(event)
            elif rearmed:
                self._emit({
                    "type": "worker_auto_reconnect_rearmed",
                    "level": "INFO",
                    "code": "worker.reconnect_rearmed",
                    "outcome": "started",
                    "ts": now,
                    "summary": "Auto-reconnect re-armed: the grant is back on",
                    "detail": {"cause": cause},
                })
            return None

    def _maybe_rearm_locked(self, grant_on, now_mono):
        """Clear a grant stop-early when the grant is back (§6.3).

        Caller holds the lock. Only a grant stop-early re-arms, and
        only on positive evidence the grant is back ON (``grant_on``
        True) — an unknown cause never re-arms. The episode continues
        (``disconnected_since`` is kept) and the stale grant cause is
        cleared; the attempt budget resets so auto-reconnect gets a
        fresh 10 attempts.
        """
        if self._stopped_reason not in ("grant-off", "grant-paused"):
            return False
        if grant_on is not True:
            return False
        self._stopped_reason = None
        self._attempts = 0
        self._cause = None
        self._next_attempt_at = now_mono
        return True

    # -- attempts -------------------------------------------------------
    def attempt_due(self, now_mono=None):
        """True when an automatic attempt is scheduled and due."""
        now_mono = self._monotonic() if now_mono is None else now_mono
        with self._lock:
            if not self._fired:
                return False
            if self._stopped_reason is not None:
                return False
            if self._attempts >= MAX_ATTEMPTS:
                return False
            if self._next_attempt_at is None:
                return False
            return now_mono >= self._next_attempt_at

    def attempt(self, kind, bridge_healthy=None):
        """Run one reconnect attempt (``kind`` "auto" / "manual").

        Returns ``{"ran", "kind", "ok", "error", "attempt", "recovered"}``.
        A concurrent caller joins the in-flight attempt and receives
        the same outcome with ``ran`` False — it never starts a second
        reconcile and is never counted as an attempt. When the watchdog
        has not fired, the attempt is a bare probe: it runs the
        reconcile, reports the outcome, and records nothing.
        ``bridge_healthy`` is a bool, or a callable returning one; it
        is consulted only on failure, for the reconcile-failing
        stop-early (§6.3). None (unknown) never triggers that stop.
        """
        with self._flight_cond:
            if self._in_flight:
                deadline = self._monotonic() + SINGLE_FLIGHT_WAIT_SECS
                while self._in_flight:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        return {"ran": False, "kind": kind, "ok": False,
                                "error": "reconcile already in progress",
                                "attempt": None, "recovered": False}
                    self._flight_cond.wait(timeout=remaining)
                # The runner's exact result, marked as joined: a bare
                # probe success is never reported as a recovery.
                result = dict(self._flight_result)
                result["ran"] = False
                return result
            self._in_flight = True
        result = None
        try:
            ok, error = self._run_reconnect(kind)
            result = self._record_attempt(kind, ok, error, bridge_healthy)
        except Exception as exc:  # noqa: BLE001 - an attempt never hangs the caller
            result = {"ran": True, "kind": kind, "ok": False,
                      "error": str(exc) or "attempt failed",
                      "attempt": None, "recovered": False}
        finally:
            with self._flight_cond:
                self._in_flight = False
                self._flight_result = result
                self._flight_cond.notify_all()
        return result

    def manual_attempt(self, bridge_healthy=None):
        """One manual reconnect, outside the backoff schedule (§10.5).

        Runs even after the cap, when it is the only way to retry, and
        even when the watchdog has not fired (a bare probe then).
        """
        return self.attempt("manual", bridge_healthy=bridge_healthy)

    def _run_reconnect(self, kind):
        try:
            ok, error = self._reconnect(kind)
        except Exception as exc:  # noqa: BLE001 - a raising reconnect is a failed attempt
            return False, str(exc) or "reconnect failed"
        if ok:
            return True, None
        return False, str(error) if error else "reconnect failed"

    def _bridge_healthy_value(self, bridge_healthy):
        if callable(bridge_healthy):
            try:
                return bool(bridge_healthy())
            except Exception:
                return None
        if bridge_healthy is None:
            return None
        return bool(bridge_healthy)

    def _record_attempt(self, kind, ok, error, bridge_healthy):
        now = self._clock()
        now_mono = self._monotonic()
        with self._lock:
            if not self._fired:
                # Bare probe while connected: report the outcome,
                # record nothing, emit nothing.
                return {"ran": True, "kind": kind, "ok": ok, "error": error,
                        "attempt": None, "recovered": False}
            self._last_attempt_at = now
            if ok:
                # Recovery is verified here: ok means a clean reconcile
                # AND ready afterwards (§5.4) — the reconnect callable
                # guarantees both; a stale ready flag never reaches us.
                ordinal = self._attempts + 1
                down_seconds = now - self._disconnected_since
                events = [{
                    "type": "worker_reconnect_attempt",
                    "level": "INFO",
                    "code": "worker.reconnect_attempt",
                    "outcome": "ok",
                    "ts": now,
                    "summary": "Reconnect attempt %d of %d (%s) succeeded"
                               % (ordinal, MAX_ATTEMPTS, kind),
                    "detail": {"attempt": ordinal, "max_attempts": MAX_ATTEMPTS,
                               "kind": kind, "next_delay_s": None,
                               "outcome": "ok", "error": None},
                }, {
                    "type": "worker_reconnected",
                    "level": "INFO",
                    "code": "worker.reconnected",
                    "outcome": "recovered",
                    "ts": now,
                    "summary": "Worker reconnected %s (was down %s)"
                               % (_fmt_hhmmss(now), _fmt_duration(down_seconds)),
                    "detail": {"down_seconds": down_seconds,
                               "reconnected_at": now},
                }]
                self._reset_locked(now)
                for event in events:
                    self._emit(event)
                return {"ran": True, "kind": kind, "ok": True, "error": None,
                        "attempt": ordinal, "recovered": True}
            self._attempts += 1
            ordinal = self._attempts
            delay = BACKOFF_DELAYS[min(self._attempts - 1,
                                       len(BACKOFF_DELAYS) - 1)]
            self._next_attempt_at = now_mono + delay
            next_delay = delay
            stopped = None
            if self._stopped_reason is None:
                if self._cause in GRANT_STOP_CAUSES:
                    stopped = ("grant-off" if self._cause == "grant_off"
                               else "grant-paused")
                elif (self._attempts >= RECONCILE_FAILING_STOP_ATTEMPTS
                        and self._bridge_healthy_value(bridge_healthy) is True):
                    stopped = "reconcile-failing"
                elif kind == "auto" and self._attempts >= MAX_ATTEMPTS:
                    stopped = "cap"
                if stopped is not None:
                    self._stopped_reason = stopped
                    self._next_attempt_at = None
                    next_delay = None
            events = [{
                "type": "worker_reconnect_attempt",
                "level": "INFO" if kind == "manual" else "WARN",
                "code": "worker.reconnect_attempt",
                "outcome": "failed",
                "ts": now,
                "summary": "Reconnect attempt %d of %d (%s) failed"
                           % (ordinal, MAX_ATTEMPTS, kind),
                "detail": {"attempt": ordinal, "max_attempts": MAX_ATTEMPTS,
                           "kind": kind, "next_delay_s": next_delay,
                           "outcome": "failed", "error": error},
            }]
            if stopped is not None:
                events.append({
                    "type": "worker_auto_reconnect_stopped",
                    "level": "WARN",
                    "code": "worker.auto_reconnect_stopped",
                    "outcome": "stopped",
                    "ts": now,
                    "summary": "Auto-reconnect stopped after %d attempts (%s)"
                               % (ordinal, stopped),
                    "detail": {"attempts": ordinal, "reason": stopped},
                })
            for event in events:
                self._emit(event)
            return {"ran": True, "kind": kind, "ok": False, "error": error,
                    "attempt": ordinal, "recovered": False}

    def _reset_locked(self, now):
        """Return to the connected state after verified recovery."""
        self._fired = False
        self._misses = 0
        self._first_miss_at = None
        self._last_good_at = now
        self._disconnected_since = None
        self._cause = None
        self._signal = None
        self._attempts = 0
        self._next_attempt_at = None
        self._stopped_reason = None

    # -- state ----------------------------------------------------------
    def state(self):
        """Current state for the UI / the ``worker_reconnect`` op."""
        now = self._clock()
        now_mono = self._monotonic()
        with self._lock:
            next_delay = None
            if self._fired and self._stopped_reason is None \
                    and self._next_attempt_at is not None:
                next_delay = max(0.0, self._next_attempt_at - now_mono)
            return {
                "connected": not self._fired,
                "fired": self._fired,
                "disconnected_since": self._disconnected_since,
                "disconnected_since_hhmmss": (
                    _fmt_hhmmss(self._disconnected_since)
                    if self._disconnected_since is not None else None),
                "cause": self._cause,
                "signal": self._signal,
                "attempts": self._attempts,
                "max_attempts": MAX_ATTEMPTS,
                "last_attempt_at": self._last_attempt_at,
                "last_attempt_hhmmss": (
                    _fmt_hhmmss(self._last_attempt_at)
                    if self._last_attempt_at is not None else None),
                "next_delay_s": next_delay,
                "next_attempt_at": self._next_attempt_at,
                "stopped_reason": self._stopped_reason,
                "last_good_at": self._last_good_at,
                "misses": self._misses,
            }

    def muse_signal(self):
        """Fired state for the fail-fast gate/trip, or None when up.

        The relay-based snapshot path is untouched — both signals
        refuse, for different failures (spec §16).
        """
        with self._lock:
            if not self._fired:
                return None
            return {"down": True, "since": self._disconnected_since,
                    "cause": self._cause}
