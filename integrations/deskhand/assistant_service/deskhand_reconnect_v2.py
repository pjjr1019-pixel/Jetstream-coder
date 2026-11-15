"""Worker-reconnect fix, round 2 v2 (VM only, 2026-11-08).

STOP latch removed (Phil 2026-11-09): this module holds the v2
reconnect mechanisms only. It never reads, records, or clears any
STOP flag, and no STOP gate lives here -- the prove_stale_stop()
gate, the STOP_REASON_DENY set, and the stop-observation diagnostics
were deleted with the latch. Reconnect only re-creates the
listener/route, and every action afterwards still passes the normal
host gates again. The named host STOP event still exists (the bridge
contract requires it) but is never set by this code.

Numbers in this module are TUNABLE STARTING VALUES, not proven-safe
constants. No VM test can prove them: proving the wedged threshold /
cooldown needs a loaded live bridge (slow/overloaded process that
recovers just as it would be killed), and proving the throttle needs
load testing under concurrent callers. The load tests that would
validate them are listed in CONNECTION-RELIABILITY-NOTES.md Round 2 v2.
"""
import threading
import time

# --- Tunable starting values (NOT proven safe; see module docstring) ---
# Consecutive failed state() reads from a live process before it counts
# as wedged. 3 x 5s tick ~= 15s. A slow/overloaded bridge can legitimately
# take longer than this to answer; the value must be validated against a
# loaded live bridge before it is trusted.
WEDGED_FAILURE_THRESHOLD = 3
# At most one force-restart per minute, so a persistently wedged bridge
# degrades to a restart a minute instead of a restart loop.
FORCE_RESTART_COOLDOWN_SECS = 60.0
# At most one on-demand reconcile per 2s from ensure_chat_ready, so the
# relay's wait loop does not hammer the funnel.
ENSURE_RECONCILE_THROTTLE_SECS = 2.0
# How long a concurrent reconcile caller waits for the in-flight one to
# finish before giving up instead of starting an overlapping reconcile.
RECONCILE_SINGLE_FLIGHT_WAIT_SECS = 10.0

class ReconnectDiagnostics:
    """Thread-safe record of what reconnect did and did not do.

    Failures are recorded here, never swallowed into a ready verdict:
    callers must consult last_reconcile_ok instead of trusting a stale
    ready flag after a reconcile attempt failed.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.last_reconcile_at = None       # time.monotonic() of last attempt
        self.last_reconcile_ok = None       # True/False/None (never attempted)
        self.last_reconcile_error = None    # str or None
        self.last_force_restart_at = None
        self.last_force_restart_reason = None
        self.deferred_restarts = 0
        self.refused_restarts_no_guard = 0
        self.restarts_without_interrupted_handler = 0
        self.interrupted_marked = 0
        self.reconcile_runs = 0
        self.reconcile_joins = 0

    def record_reconcile(self, ok, error=None):
        with self._lock:
            self.last_reconcile_at = time.monotonic()
            self.last_reconcile_ok = bool(ok)
            self.last_reconcile_error = None if ok else str(error or 'unknown')
            self.reconcile_runs += 1

    def record_force_restart(self, reason):
        with self._lock:
            self.last_force_restart_at = time.monotonic()
            self.last_force_restart_reason = reason

    def record_restart_deferred(self, reason):
        with self._lock:
            self.deferred_restarts += 1

    def record_restart_refused_no_guard(self):
        with self._lock:
            self.refused_restarts_no_guard += 1

    def record_restart_without_interrupted_handler(self):
        with self._lock:
            self.restarts_without_interrupted_handler += 1

    def record_interrupted(self, count):
        with self._lock:
            if count and count > 0:
                self.interrupted_marked += count

    def record_join(self):
        with self._lock:
            self.reconcile_joins += 1

    def snapshot(self):
        with self._lock:
            return {
                'last_reconcile_at': self.last_reconcile_at,
                'last_reconcile_ok': self.last_reconcile_ok,
                'last_reconcile_error': self.last_reconcile_error,
                'last_force_restart_at': self.last_force_restart_at,
                'last_force_restart_reason': self.last_force_restart_reason,
                'deferred_restarts': self.deferred_restarts,
                'refused_restarts_no_guard': self.refused_restarts_no_guard,
                'restarts_without_interrupted_handler':
                    self.restarts_without_interrupted_handler,
                'interrupted_marked': self.interrupted_marked,
                'reconcile_runs': self.reconcile_runs,
                'reconcile_joins': self.reconcile_joins,
            }


class SingleFlight:
    """One reconcile at a time; concurrent callers join, never overlap.

    run(fn) executes fn if no reconcile is in flight and returns
    (True, True, value) on success or (True, False, exception) on
    failure. A concurrent caller waits (up to `timeout`) for the running
    reconcile to finish and then receives the same outcome as
    (False, ok, value_or_exception). It never starts a second reconcile
    and never reports a stale success. Waiting past `timeout` raises
    RuntimeError('reconcile already in progress') instead of inventing
    a result. Only the runner's outcome is the real one: callers must
    record diagnostics only when ran is True.
    """

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._busy = False
        self._outcome = None  # (ok, value_or_exception)

    def run(self, fn, timeout=RECONCILE_SINGLE_FLIGHT_WAIT_SECS):
        with self._cond:
            if not self._busy:
                self._busy = True
                runner = True
            else:
                runner = False
        if runner:
            try:
                value = fn()
                outcome = (True, value)
            except BaseException as exc:  # noqa: BLE001 -- joiners get it too
                outcome = (False, exc)
            with self._cond:
                self._outcome = outcome
                self._busy = False
                self._cond.notify_all()
            return (True, outcome[0], outcome[1])
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('reconcile already in progress')
                self._cond.wait(timeout=remaining)
            ok, value = self._outcome
        return (False, ok, value)


# B2-D (100.030): InFlightGuard + _GuardContext were removed here --
# defined, never wired, zero callers (production guard is
# DispatcherIdleGuard below, wired in deskhand_service.py).
class DispatcherIdleGuard:
    """Adapter over a dispatcher that exposes an `idle` threading.Event.

    deskhand_dispatch.Dispatcher sets idle when no turn is dispatching
    and clears it while one is. A turn is in flight exactly when the
    event is not set. This is the intended production guard source;
    wiring it from ChatService through MuseService to MuseConnection is
    still open (see CONNECTION-RELIABILITY-NOTES.md Round 2 v2).
    """

    def __init__(self, dispatcher):
        self._dispatcher = dispatcher

    def is_in_flight(self):
        idle = getattr(self._dispatcher, 'idle', None)
        if idle is None:
            return True  # unknown dispatcher state: fail closed
        return not idle.is_set()


def mark_interrupted_uncertain(mailbox, reason):
    """Mark dispatching turns uncertain after a restart, never replay.

    Returns the number of turns marked, 0 when there is nothing to
    mark, or -1 when the mailbox cannot do it (no recover method, or
    it raised). Callers must record -1 in diagnostics, not ignore it:
    the dispatcher entry path also recovers interrupted turns as
    unknown, but a failed mark here means this restart's interruption
    was not recorded at the time it happened.
    """
    if mailbox is None:
        return -1
    recover = getattr(mailbox, 'recover_interrupted', None)
    if recover is None:
        return -1
    try:
        return int(recover())
    except Exception:
        return -1
