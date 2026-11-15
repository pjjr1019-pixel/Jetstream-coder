"""Fail-fast submit gate (100.020, C07) — pure route-aware check.

Reads a diagnostics health snapshot (deskhand_diagnostics.py contract:
exactly the 13 component keys, each {status, last_ok, version, error},
status one of OK / DEGRADED / DOWN / UNKNOWN / IDLE) plus a turn
context {route, agent_task_id, origin} and returns either None (allow)
or a refusal dict {path, message, dead_since, cause}.

Pure: no I/O, no imports from the service, no live PC needed. The VM
tests exercise this module against fake snapshots.

Rules implemented (coordinator decisions D-020-1..D-020-11, 2026-11-10;
the Step 0 audit is the live-code truth and overrides the spec where
they disagree):

- D-020-1 / spec D9: a STALE snapshot (as_of age > 6 s, the codebase's
  established threshold) is all-UNKNOWN: allow, never refuse on it.
  UNKNOWN on any row also allows (never block on ignorance).
- D-020-2: 401/502 STRUCK — not implemented. The submit path carries
  no HTTP status (zero 401/502 literals in live code); no string-match
  is invented. There is no refusal path for auth/session errors here.
- D-020-3: STOP STRUCK — the latch is dead (created RESET, never set).
  No STOP gate exists in this module.
- D-020-4: paths 1+3 MERGED. The gateway row is permanently UNKNOWN
  (no probe exists), so the worker row (RelayConnected boolean +
  last_ok) is the live signal. Refuse when worker is DOWN AND
  last_ok age > 6 s (prevents single-blip refusals without inventing
  a counter). Wording is spec §5 row 3 with a cause line when known,
  else row 1's "last OK HH:MM" form. "Dead since" is derived from
  last_ok, NOT a first-miss timestamp (none exists in code).
- D-020-5: tunnel refuses on DOWN only, NOT DEGRADED. DEGRADED =
  ephemeral + echo passing (may still be alive); DOWN = echo failing
  >= 2x or stale LAN (dead).
- D-020-6: agent row DOWN -> refuse as-is (documented limitation:
  no counting exists in code; a single slow agent probe can produce
  a DOWN snapshot and a refusal. Cost of a false refusal is low:
  draft kept, re-send, automatic recovery).
- D-020-7: grant refuses on DOWN or IDLE (paused). DOWN -> §5 row 2
  wording ("Full PC access is off... Generation N"); IDLE -> paused
  variant. UNKNOWN -> allow. Grant gates gateway/bridge turns only
  (route assistant muse/codex), NEVER agent turns.
- D-020-8: in-flight trip (see inflight_trip()): a dispatching turn
  whose path reads DOWN (worker/agent, last_ok > 6 s) is marked
  uncertain. The real window is the 15 s stuck-turn watchdog
  cadence, not the spec's ~3 s (no 3 s detection rule exists in
  code).
- D-020-9: route-awareness. Agent-task turns (agent_task_id set):
  agent row only. Chat turns: worker (merged), tunnel
  (phone-originated), grant (gateway/bridge, not agent). If the
  gate cannot determine a turn's transport/origin, it refuses
  nothing on that axis (spec §3) — except the tunnel axis, where
  the coordinator's documented approximation applies: no origin
  marker exists in the submit command at all (verified: the command
  carries op/id/text/assistant/model/account/effort/agent, plus
  agent_task_id added by C#), so the tunnel gate applies to chat
  (non-agent-task) turns.
- D-020-10: wording single source of truth = this service module.
  The gate generates the §5 refusal text; C#/Android display the
  server-provided text.
- 100.037 (E12): a second, independent refusal/trip signal — the
  Muse worker transport, as observed by the worker watchdog
  (deskhand_worker_watchdog.muse_signal()). It is NOT the relay-based
  worker row above (different session, §5.2/R2): both signals refuse,
  for different failures. ``muse_worker_refusal`` carries E12's
  verbatim message; ``inflight_trip`` takes an optional ``muse_worker``
  signal and trips a dispatching chat turn on it without a snapshot.
"""

import time

# The codebase's established STALE threshold (diagnostics export,
# MainForm footer, activity age): a snapshot older than this is
# UNKNOWN, never read as dead or alive.
STALE_SECS = 6

# Route assistants that travel over the gateway/bridge (the grant
# gates these only). "local" routes run on the PC's own runtime and
# are not gated by the grant; an unknown/empty route gates nothing.
GATEWAY_BRIDGE_ASSISTANTS = {"muse", "codex"}

# Origin values that mean the turn travelled over the phone relay
# (tunnel gate applies). None means "no marker in the command" — the
# documented approximation is that the tunnel gate applies to chat
# turns then (D-020-9). Any other explicit value means the turn did
# not come over the phone relay, so the tunnel gate does not apply.
PHONE_ORIGINS = {"phone", "relay", "tunnel", "trycloudflare"}

_TIMEOUT_MARKERS = ("timed out", "timeout")


def is_timeout_error(error_text):
    """True when an agent/bridge probe error is a timeout, not an answer.

    Used by the bounded snapshot read (D-020-1): a probe that could
    not be determined within budget is UNKNOWN, never DOWN. A fast
    refusal ("Connection refused") is a real DOWN signal and stays DOWN.
    """
    if not isinstance(error_text, str):
        return False
    lowered = error_text.lower()
    return any(marker in lowered for marker in _TIMEOUT_MARKERS)


def _fmt_hhmm(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


def _fmt_hhmmss(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def _row(snapshot, key):
    if not isinstance(snapshot, dict):
        return None
    components = snapshot.get("components")
    if not isinstance(components, dict):
        return None
    row = components.get(key)
    return row if isinstance(row, dict) else None


def _status(snapshot, key):
    row = _row(snapshot, key)
    if row is None:
        return "UNKNOWN"
    status = row.get("status")
    return status if isinstance(status, str) else "UNKNOWN"


def _last_ok(snapshot, key):
    row = _row(snapshot, key)
    if row is None:
        return 0
    try:
        return int(row.get("last_ok") or 0)
    except (TypeError, ValueError):
        return 0


def _last_ok_age(snapshot, key, now):
    last_ok = _last_ok(snapshot, key)
    if last_ok <= 0:
        return None  # never seen OK: infinitely old, but no time to show
    return now - last_ok


def snapshot_is_stale(snapshot, now=None):
    """True when the snapshot must be treated as all-UNKNOWN (spec D9)."""
    if not isinstance(snapshot, dict) or snapshot.get("ok") is not True:
        return True
    as_of = snapshot.get("as_of")
    if not isinstance(as_of, (int, float)):
        return True
    now = time.time() if now is None else now
    return (now - as_of) > STALE_SECS


def _worker_refusal(snapshot, now):
    """Merged worker-down refusal (D-020-4), or None.

    Refuse only when the worker row is DOWN AND last_ok age > 6 s.
    """
    if _status(snapshot, "worker") != "DOWN":
        return None
    age = _last_ok_age(snapshot, "worker", now)
    if age is not None and age <= STALE_SECS:
        return None  # single blip: last OK within the threshold
    last_ok = _last_ok(snapshot, "worker")
    # Cause line, when known (never guessed): the grant is the usual
    # cause of a dead worker; the tunnel is the other known one.
    grant_status = _status(snapshot, "grant")
    if grant_status == "DOWN":
        cause = "Full PC access is off \u2014 turn it on in the Deskhand app on the PC."
        when = _fmt_hhmmss(last_ok) if last_ok > 0 else "unknown time"
        message = ("Couldn't send \u2014 the PC session dropped at %s. %s "
                   "Your message is saved as a draft." % (when, cause))
        return {"path": "worker", "message": message,
                "dead_since": last_ok or None, "cause": "grant_off"}
    if grant_status == "IDLE":
        cause = "Full PC access is paused \u2014 resume it in the Deskhand app on the PC."
        when = _fmt_hhmmss(last_ok) if last_ok > 0 else "unknown time"
        message = ("Couldn't send \u2014 the PC session dropped at %s. %s "
                   "Your message is saved as a draft." % (when, cause))
        return {"path": "worker", "message": message,
                "dead_since": last_ok or None, "cause": "grant_paused"}
    # Cause unknown: spec §5 row 1's "last OK HH:MM" form.
    if last_ok > 0:
        message = ("Couldn't send \u2014 the PC link isn't serving right now "
                   "(last OK %s). Your message is saved as a draft. "
                   "Cause unknown \u2014 last good %s."
                   % (_fmt_hhmm(last_ok), _fmt_hhmmss(last_ok)))
    else:
        message = ("Couldn't send \u2014 the PC link isn't serving right now "
                   "(last OK unknown). Your message is saved as a draft. "
                   "Cause unknown \u2014 never seen OK.")
    return {"path": "worker", "message": message,
            "dead_since": last_ok or None, "cause": None}


def _agent_refusal(snapshot, now):
    """Agent-wedged refusal (D-020-6, spec §5 row 5), or None."""
    if _status(snapshot, "agent") != "DOWN":
        return None
    last_ok = _last_ok(snapshot, "agent")
    when = _fmt_hhmm(last_ok) if last_ok > 0 else "unknown"
    message = ("Couldn't send \u2014 the PC helper isn't answering "
               "(last OK %s). Your message is saved as a draft. "
               "It's being restarted \u2014 try again in a couple of minutes."
               % when)
    return {"path": "agent", "message": message,
            "dead_since": last_ok or None, "cause": None}


def _is_phone_origin(origin):
    if origin is None:
        return True  # documented approximation (D-020-9): no marker exists
    if not isinstance(origin, str):
        return False
    return origin.strip().lower() in PHONE_ORIGINS


def _tunnel_refusal(snapshot, context):
    """Tunnel-stale refusal (D-020-5, spec §5 row 4), or None.

    DOWN only, never DEGRADED. Phone-originated chat turns only.
    """
    if _status(snapshot, "tunnel") != "DOWN":
        return None
    if not _is_phone_origin(context.get("origin")):
        return None
    message = ("Couldn't send \u2014 the phone's link to the PC is a "
               "temporary address that stopped working. Your message is "
               "saved as a draft. Pair via Tailscale in the Deskhand app "
               "for a stable address \u2014 the phone needs to pair once "
               "with the new address.")
    return {"path": "tunnel", "message": message,
            "dead_since": _last_ok(snapshot, "tunnel") or None,
            "cause": None}


def _grant_refusal(snapshot, context):
    """Grant refusal (D-020-7, spec §5 row 2 + paused variant), or None.

    Gateway/bridge chat turns only (route assistant muse/codex).
    NEVER agent turns. UNKNOWN -> allow.
    """
    if context.get("agent_task_id") is not None:
        return None
    if context.get("route") not in GATEWAY_BRIDGE_ASSISTANTS:
        return None
    grant_status = _status(snapshot, "grant")
    if grant_status not in ("DOWN", "IDLE"):
        return None
    row = _row(snapshot, "grant") or {}
    version = row.get("version") or ""
    generation = version if isinstance(version, str) and version else "Generation unknown"
    if grant_status == "DOWN":
        message = ("Couldn't send \u2014 Full PC access is off on the PC "
                   "(%s). Your message is saved as a draft. Turn it back "
                   "on in the Deskhand app on the PC \u2014 I can't do that "
                   "part from here." % generation)
        return {"path": "grant", "message": message,
                "dead_since": None, "cause": "grant_off"}
    message = ("Couldn't send \u2014 Full PC access is paused on the PC "
               "(%s). Your message is saved as a draft. Resume it in the "
               "Deskhand app on the PC \u2014 I can't do that part from here."
               % generation)
    return {"path": "grant", "message": message,
            "dead_since": None, "cause": "grant_paused"}


def muse_worker_refusal(signal):
    """Muse-worker refusal (100.037, E12 §7.2), or None.

    signal: {"down": bool, "since": epoch|None, "cause": str|None} —
    the worker watchdog's fired state (deskhand_worker_watchdog.
    muse_signal()). Refuses with E12's verbatim message when the Muse
    worker is known down. Independent of the diagnostics snapshot: the
    watchdog reads the worker transport in-process, so the service
    checks this even when no snapshot could be built. The refused text
    stays a draft (live behaviour); the turn is never auto-sent.
    """
    if not isinstance(signal, dict) or signal.get("down") is not True:
        return None
    since = signal.get("since")
    if isinstance(since, (int, float)) and since > 0:
        when = _fmt_hhmm(since)
        dead_since = since
    else:
        when = "unknown"
        dead_since = None
    message = ("Worker disconnected since %s \u2014 your message was not sent"
               % when)
    return {"path": "muse_worker", "message": message,
            "dead_since": dead_since, "cause": signal.get("cause")}


def evaluate_gate(snapshot, context, now=None):
    """Route-aware submit gate. Returns None (allow) or a refusal dict.

    snapshot: the diagnostics health snapshot dict ({"ok", "as_of",
    "components"}). context: {"route": assistant str or None,
    "agent_task_id": str or None, "origin": str or None}.
    """
    now = time.time() if now is None else now
    context = context if isinstance(context, dict) else {}
    if snapshot_is_stale(snapshot, now):
        return None  # STALE -> all UNKNOWN -> allow (spec D9)
    if context.get("agent_task_id") is not None:
        # Agent-task turns: agent row only (D-020-9).
        return _agent_refusal(snapshot, now)
    # The retired Cloudflare tunnel diagnostic is not the current
    # phone transport and cannot establish that a chat route is down.
    # Keep worker and grant checks; neither relies on that legacy row.
    # A positively identified plain-chat tunnel uses the laptop agent,
    # not the disconnected Muse/bridge worker. Unknown provider stays
    # fail-closed to the previous worker behavior.
    refusal = None if context.get("tunnel_served") is True else _worker_refusal(snapshot, now)
    if refusal is not None:
        return refusal
    return _grant_refusal(snapshot, context)


def inflight_trip(snapshot, turn, now=None, muse_worker=None):
    """In-flight trip decision (D-020-8).

    turn: {"turn_id": str, "agent_task_id": str or None,
    "route": assistant str or None} — as returned by the dispatcher's
    running_turn_info(). Returns (trip: bool, reason: str or None).
    Trips only when the turn's own path reads DOWN with last_ok age
    > 6 s (worker for chat turns, agent for agent turns). A stale
    snapshot never trips. The caller marks the turn uncertain via the
    existing mailbox.uncertain path; the turn is never failed and
    never replayed.

    100.037: ``muse_worker`` is the watchdog's fired state
    ({"down": bool, "since": epoch|None, "cause": str|None}). For a
    chat turn it trips WITHOUT a snapshot — the watchdog reads the
    transport in-process, so a known-down worker trips the turn even
    when no snapshot could be built. Agent turns never trip on it
    (agent row only, D-020-8).
    """
    now = time.time() if now is None else now
    if not isinstance(turn, dict) or not turn.get("turn_id"):
        return False, None
    if turn.get("agent_task_id") is not None:
        if snapshot_is_stale(snapshot, now):
            return False, None
        if _status(snapshot, "agent") != "DOWN":
            return False, None
        age = _last_ok_age(snapshot, "agent", now)
        if age is not None and age <= STALE_SECS:
            return False, None
        last_ok = _last_ok(snapshot, "agent")
        when = _fmt_hhmmss(last_ok) if last_ok > 0 else "unknown time"
        return True, ("agent not answering (last OK %s); turn is uncertain, "
                      "never replayed" % when)
    # Chat turn: the Muse-worker signal first (snapshot-independent).
    if isinstance(muse_worker, dict) and muse_worker.get("down") is True:
        since = muse_worker.get("since")
        when = (_fmt_hhmmss(since)
                if isinstance(since, (int, float)) and since > 0
                else "unknown time")
        return True, ("worker disconnected at %s; turn is uncertain, "
                      "never replayed" % when)
    if snapshot_is_stale(snapshot, now):
        return False, None
    if _status(snapshot, "worker") != "DOWN":
        return False, None
    age = _last_ok_age(snapshot, "worker", now)
    if age is not None and age <= STALE_SECS:
        return False, None
    last_ok = _last_ok(snapshot, "worker")
    when = _fmt_hhmmss(last_ok) if last_ok > 0 else "unknown time"
    return True, ("worker disconnected at %s; turn is uncertain, "
                  "never replayed" % when)
