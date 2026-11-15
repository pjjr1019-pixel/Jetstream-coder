"""Laptop-agent fallback op for the Deskhand service (Option A hook) -- v2.

REVIEW NOTE (2026-11-08, GPT verdict CHANGES REQUESTED on the v1
design): GPT reviewed the v1 design doc + limited code and did NOT see
this module. Several of its points were ALREADY closed by this code
-- the v1 doc lagged it -- and several were genuinely open. v2 starts
from this code, keeps what was right, and closes the rest. See
fallback-hook-design-v2.md for the point-by-point change log.

What v1 of this module already did (verified against this file):
  * Two entry points sharing one implementation: in-process
    ``ChatService.handle()`` op ``task_agent_action`` and the on-PC
    runner (``run_task_agent_fallback.py``). The route does not change
    the authorization.
  * Host authority first (vendor ``ensure_host_allows`` in-process;
    bridge operator state in the runner), Full PC access grant from
    persisted ``%LOCALAPPDATA%\\Deskhand\\config.json``
    (``FullPcAccess.Enabled`` + ``Paused``), agent
    ``remote_control_enabled`` inside the client.
  * Fallback-capable kinds only; type/click/key/window refused.
  * Journal ``task_agent_fallback.jsonl`` incl. refusals.

What v2 changes (GPT gaps 1-9):
  * PRE-CHECK-ONLY ROUTING (adopted; the VM caller already did this
    -- the v1 doc's trigger (2b), fallback after a failed
    ``executor.submit``, is DROPPED, not allowlisted: no error set
    was ever proven, and any response that might follow acceptance
    is uncertain. The submit-error allowlist question is MOOT.)
  * ATOMIC ACTION CLAIMS replace journal-based dedup
    (``deskhand_action_claim.py``): one task ID binds exactly one
    payload (``action_id`` = sha256 of canonical task_id + action),
    claimed with O_EXCL + fsync before dispatch, settled to
    executed / failed / uncertain. A task ID whose claim is
    ``executed`` is never re-executed; ``uncertain`` (incl. a claim
    left ``claimed`` by a crash) never auto-retries -- operator
    resolution only. The old ``_executed_task_ids`` journal scan is
    gone: the journal is audit only, the claim store enforces.
  * DISPATCH-BOUNDARY RECHECK: STOP + grant are re-checked
    immediately before dispatch AND before every acting agent call
    (``dispatch_guard`` on the client). An in-flight action cannot
    be interrupted -- the agent has no cancel endpoint -- so a STOP
    that lands mid-flight is recorded as a post-dispatch anomaly on
    an ``executed`` receipt, and the NEXT action is refused.
  * OUTCOME CLASSIFICATION (fail closed): only errors raised BEFORE
    an acting request is sent settle ``failed`` (retry allowed):
    validation, gate refusals, health-check failure, config error,
    dispatch-guard refusal, bad screenshot (read-only). ANY error
    from an acting call itself -- timeout, transport error, 401 --
    settles ``uncertain``: no replay until an operator resolves the
    claim with independent evidence (agent request counter).
  * APPROVAL BINDING under Phil's local model (no per-action phone
    approval; Phil's 2026-11-07 choice): the claim binds the
    host-issued task ID to the exact payload hash computed BY THE OP,
    and the journal/claim record the approval basis. A caller-supplied
    action hash is verified when present but is NOT proof of Phil's
    approval (the caller computed it); task-ID authenticity itself
    rests on the transport (agent token / host process). The
    phone-approval question is an explicit re-review decision, not
    silently waived -- see the v2 design doc.
  * The gateway route's journal is vendor-owned; no VM copy exists,
    so journal/receipt equivalence with the gateway route CANNOT be
    demonstrated here. Cross-route safety rests on pre-check-only
    routing, not on a shared store (gateway claims through the claim
    store would need vendor cooperation -- open item).

Gates (all checked on the PC, before dispatch, every call):
  * Host authority. In-process: the vendor ``ensure_host_allows()``
    (same check prepare_host_tools / ensure_chat_ready use -- it reads
    the named host STOP event, which still exists but is never set).
    Runner: the vendor check CANNOT run in a fresh process -- the STOP
    event name is a fresh GUID per app start (HostEngine creates
    ``Local\\Deskhand.MuseStop.<guid>`` and passes it to the service
    process only) and the vendor check silently passes when
    DESKHAND_JETT_RUNTIME is unset. So the runner checks the bridge
    operator state on loopback instead (``bridge_stop_allows``): a
    bridge state that cannot be read refuses. Fail-closed. STOP latch
    removed (Phil 2026-11-09): no STOP field is read anywhere in this
    path, and STOP is never cleared by this path.
  * Deskhand Full PC access grant on: read from the app's persisted
    config (``%LOCALAPPDATA%\\Deskhand\\config.json``:
    ``FullPcAccess.Enabled`` true AND ``Paused`` false -- the same
    two fields InitializeFullPcAccess uses at startup). Missing or
    unreadable config refuses. The in-memory ``_accessState`` ("On")
    is not visible to the service; the persisted commit is the best
    service-side source and SetFullPcAccess saves on every change,
    including revoke. LIMIT (honest): a grant change that has not
    been persisted yet (app crash between the in-memory change and
    the save) is invisible here -- the persisted commit can be stale
    in that window. Fail closed when unreadable.
  * Agent ``remote_control_enabled``: checked inside TaskAgentClient
    via /health (second gate, unchanged from the client design).

Rules:
  * Only fallback-capable kinds run: exec, open_app, open_notepad,
    files_read, files_write, files_search, screenshot. Type / click /
    key / window kinds are refused with a clear message, never
    downgraded (no SendKeys).
  * A host-issued task/turn ID is required. The task ID binds one
    payload via the claim store (see above).
  * The action runs exactly once (``run_action`` is called once; no
    retry, including on 401 -- report and stop; an ``uncertain``
    claim blocks any retry until resolved by an operator).
  * Every call is journaled (``task_agent_fallback.jsonl`` in the
    service data dir): task ID, action_id, action kind, gateway state
    at decision, route=task_agent_fallback, outcome, claim state,
    approval basis, timestamp -- including refusals. The journal
    never holds a token or image data (screenshot receipts journal
    dimensions/bytes only).
  * 100.029 (E41 + E17): journal lines are structured (ts, level,
    component, actor, outcome, msg + code on WARN/ERROR, turn_id
    from the host-issued task id, never invented; pre-E41 outcome
    words map onto the fixed vocabulary -- executed -> ok,
    uncertain -> interrupted -- with the verbatim word kept in
    claim_state). msg/detail are scrubbed + truncated to 200 chars
    at write time, DEBUG is never written, and the file rotates
    (5 MB x 4) via the shared retention module.

Prototype only. No vendor gateway code is touched, no bridge
allowlist change, no new capability family. VM ONLY -- not wired
beyond the existing op branch, not deployed by this change.
"""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from deskhand_action_claim import (
    ClaimConflict,
    ClaimStateError,
    ClaimStore,
    canonical_action_id,
    payload_hash,
)
# 100.029 (E17 + E41): structured journal fields, write-time scrub +
# truncation, and rotation live in the shared retention module.
from deskhand_retention import (
    ACTORS, COMPONENTS, LEVELS, OUTCOMES,
    rotate_if_needed, scrub_text, truncate_text,
)
from deskhand_task_agent import (
    DEFAULT_TIMEOUT,
    DispatchGuardRefused,
    TaskAgentBadScreenshot,
    TaskAgentClient,
    TaskAgentConfigError,
    TaskAgentDenied,
    TaskAgentHealthUnavailable,
    TaskAgentNotEnabled,
)

FALLBACK_KINDS = frozenset({
    "exec", "open_app", "open_notepad", "files_read", "files_write",
    "files_search", "screenshot",
})
# Kinds the agent cannot do. Refused with an explicit message, never
# silently downgraded to something else.
REFUSED_KINDS = frozenset({
    "type", "click", "key", "desktop_type", "desktop_click",
    "ui_invoke", "window_activate",
})
JOURNAL_NAME = "task_agent_fallback.jsonl"
ROUTE = "task_agent_fallback"
CLAIM_DIR_NAME = "task_agent_claims"
# E17 section 2 #1 (Option A): the service journal rotates at 5 MB,
# 4 generations, 30 days (age is the janitor's job -- the writer
# enforces size here, on every write).
JOURNAL_MAX_BYTES = 5 * 1024 * 1024
JOURNAL_BACKUP_COUNT = 4
# Journal paths whose rotation failure has already been logged once
# (E17 section 3.1: a failing rotator must not become a log-spam
# source). Per path, per process.
_JOURNAL_ROTATION_FAILURE_LOGGED = set()
# E41 section 2.3: the journal's structured ``outcome`` comes from
# the fixed vocabulary. The op's pre-E41 outcome words map onto it;
# the verbatim word survives in ``claim_state`` (executed / failed /
# uncertain), so the mapping loses no information.
_JOURNAL_OUTCOME_MAP = {
    "executed": "ok",             # the action ran and completed
    "uncertain": "interrupted",   # dispatch cut off before a receipt
    "completed": "ok",
    "rejected": "refused",
    "cancelled": "stopped",
}
# Task-agent folder on the PC; the client module already defaults here.
DEFAULT_AGENT_BASE = "http://127.0.0.1:8899"

# In-process serialization only. This lock does NOT protect the
# runner process or the gateway -- the claim store (O_EXCL) is the
# cross-process authority. The lock exists so two in-process threads
# (task_agent_action runs threaded, up to 16 in deskhand_service.py
# main()) do not interleave their claim->dispatch->settle sequences.
_LOCK = threading.Lock()


class TaskAgentFallbackRefused(RuntimeError):
    """The fallback refused to run (gate off, STOP set, wrong kind,
    missing/duplicate task ID, uncertain claim). Not an execution
    failure."""


def _truncate(text, limit=200):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "..."


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _journal_path(data_dir):
    return Path(data_dir) / JOURNAL_NAME


def _scrub_detail_value(value):
    """Scrub + truncate one detail value (str, dict, list, or other).

    Dicts/lists keep their shape -- string leaves are scrubbed and
    truncated individually, non-string leaves pass through unchanged
    (a receipt summary stays a summary dict, not a string).
    """
    if isinstance(value, dict):
        return {key: _scrub_detail_value(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_detail_value(item) for item in value]
    if value is None:
        return None
    return truncate_text(scrub_text(str(value)))


def _journal_level_and_code(entry):
    """(level, code) for one journal entry (E41 section 3.1).

    Explicit ``level`` / ``code`` in the entry win (a caller that
    knows the cause states it); otherwise both derive from the
    outcome and the reason text:
      refused   -> WARN at minimum (a refusal is the system saying no)
      failed    -> ERROR
      uncertain -> WARN (outcome unknown, needs an operator)
      executed  -> INFO, unless a post-dispatch anomaly says STOP was
                   set mid-action (WARN, code stop.engaged) or the
                   grant went off after dispatch (WARN, grant.off)
      grant.off -> INFO when the caller records grant_cause "phil"
                   (Phil toggled it), WARN otherwise (restore failure
                   or unknown cause -- visible, not silent)
    A STOP-clear attempt that was refused is ERROR: this op never
    clears STOP, so that case arrives as an explicit level/code in
    the entry, honoured here. DEBUG is never derived; an explicit
    DEBUG is rejected by _journal itself.
    """
    raw_outcome = entry.get("outcome")
    detail = entry.get("detail")
    detail_text = detail if isinstance(detail, str) else ""
    anomaly = entry.get("post_dispatch_anomaly")
    anomaly = anomaly if isinstance(anomaly, dict) else {}
    code = entry.get("code")
    code = code if isinstance(code, str) and code else None
    if code is None:
        if anomaly.get("stop_set_during_execution"):
            code = "stop.engaged"
        elif anomaly.get("grant_off_after_dispatch"):
            code = "grant.off"
        elif raw_outcome == "refused" \
                and "Full PC access is off" in detail_text:
            code = "grant.off"
        elif raw_outcome == "refused" and "STOP" in detail_text:
            code = "stop.set"
        elif raw_outcome == "refused":
            code = "journal.refused"
        elif raw_outcome == "failed":
            code = "journal.failed"
        elif raw_outcome == "uncertain":
            code = "journal.uncertain"
    level = entry.get("level")
    level = level.upper() if isinstance(level, str) else None
    if level not in LEVELS:
        level = None
    if level is None:
        if code == "grant.off":
            # The actor decides the level when the state is the same
            # (E41 section 2.1 rule 1). The op itself cannot tell a
            # Phil toggle from a restore failure, so an entry with no
            # grant_cause takes the WARN branch (visible); callers
            # that know pass grant_cause="phil" for INFO.
            level = "INFO" if entry.get("grant_cause") == "phil" \
                else "WARN"
        elif anomaly:
            level = "WARN"
        elif raw_outcome == "refused":
            level = "WARN"
        elif raw_outcome == "failed":
            level = "ERROR"
        elif raw_outcome == "uncertain":
            level = "WARN"
        else:
            level = "INFO"
    return level, code


def _journal(data_dir, entry):
    """Append one structured JSON line. Never raises (a journal
    failure must not mask the outcome); returns True when written,
    False when the entry was a DEBUG (never written to the audit
    journal, E41 section 3.1) or the append failed.

    E41 section 3.1: every entry carries the six required fields
    (ts, level, component, actor, outcome, msg) plus ``code`` on
    every WARN/ERROR and ``turn_id`` when the entry belongs to a
    turn -- ``turn_id`` is the host-issued task/turn id already in
    the entry, never invented (an entry with no usable task id
    carries no turn_id). ``msg`` / ``detail`` are scrubbed and
    truncated to 200 chars each at write time. Extra entry keys
    (task_id, action_id, kind, gateway_state, claim_state,
    approval_basis, cancellable, post_dispatch_anomaly, ...) are
    preserved verbatim after the canonical fields.
    E17 section 2 #1: the file rotates (5 MB x 4) before the write;
    a rotation failure is logged once (ERROR, code
    journal.rotation_failed) and the writer keeps appending to the
    current file.
    """
    entry = dict(entry)
    entry.setdefault("route", ROUTE)
    level, code = _journal_level_and_code(entry)
    if level == "DEBUG":
        return False
    component = entry.get("component")
    if component not in COMPONENTS:
        component = "journal"
    actor = entry.get("actor")
    if actor not in ACTORS:
        actor = "system"
    raw_outcome = entry.get("outcome")
    if isinstance(raw_outcome, str) and raw_outcome in OUTCOMES:
        outcome = raw_outcome
    elif isinstance(raw_outcome, str) \
            and raw_outcome in _JOURNAL_OUTCOME_MAP:
        outcome = _JOURNAL_OUTCOME_MAP[raw_outcome]
    else:
        outcome = "failed"  # unknown outcome word: fail visible.
    kind = entry.get("kind")
    kind_text = kind if isinstance(kind, str) and kind else "action"
    msg = entry.get("msg")
    if not isinstance(msg, str) or not msg:
        msg = "fallback %s %s" % (
            kind_text,
            raw_outcome if isinstance(raw_outcome, str) else outcome)
        if isinstance(entry.get("detail"), str) and entry["detail"]:
            msg = msg + ": " + entry["detail"]
    detail = entry.get("detail")
    if isinstance(detail, dict):
        detail = {key: _scrub_detail_value(value)
                  for key, value in detail.items()}
    elif detail is not None:
        detail = truncate_text(scrub_text(str(detail)))
    record = {
        "ts": entry.get("ts")
        if isinstance(entry.get("ts"), str) and entry["ts"] else _utcnow(),
        "level": level,
        "component": component,
        "actor": actor,
        "outcome": outcome,
    }
    if code is not None:
        record["code"] = code
    record["msg"] = truncate_text(scrub_text(msg))
    if detail is not None:
        record["detail"] = detail
    turn_id = entry.get("turn_id")
    if isinstance(turn_id, str) and turn_id.strip():
        record["turn_id"] = turn_id
    else:
        task_id = entry.get("task_id")
        if isinstance(task_id, str) and task_id.strip():
            record["turn_id"] = task_id
    for key, value in entry.items():
        if key in ("ts", "level", "component", "actor", "outcome",
                   "code", "msg", "detail", "task_id", "turn_id",
                   "grant_cause"):
            continue
        record[key] = value
    path = _journal_path(data_dir)
    try:
        needs_rotation = path.is_file() and \
            path.stat().st_size > JOURNAL_MAX_BYTES
    except OSError:
        needs_rotation = False
    if needs_rotation and not rotate_if_needed(
            path, JOURNAL_MAX_BYTES, JOURNAL_BACKUP_COUNT):
        if str(path) not in _JOURNAL_ROTATION_FAILURE_LOGGED:
            _JOURNAL_ROTATION_FAILURE_LOGGED.add(str(path))
            failure = {
                "ts": _utcnow(), "level": "ERROR", "component": "journal",
                "actor": "system", "outcome": "failed",
                "code": "journal.rotation_failed",
                "msg": "journal rotation failed; continuing to append",
            }
            try:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(failure, ensure_ascii=True)
                                 + "\n")
            except OSError:
                pass
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
        return True
    except OSError:
        return False


def read_deskhand_grant(data_dir, config_path=None):
    """True only when the persisted Deskhand grant is on.

    Reads %LOCALAPPDATA%\\Deskhand\\config.json (the service data dir's
    parent): FullPcAccess.Enabled true AND Paused false. Missing,
    unreadable, or malformed config fails closed (False).
    """
    path = Path(config_path) if config_path else Path(data_dir).parent / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(cfg, dict):
        return False
    grant = cfg.get("FullPcAccess")
    if not isinstance(grant, dict) or grant.get("Enabled") is not True:
        return False
    if cfg.get("Paused") is True:
        return False
    return True


def _default_host_allows():
    """Vendor STOP check, for the in-process service op.

    Same check prepare_host_tools / ensure_chat_ready use. Raises
    RuntimeError when STOP is set or the authority is unavailable.
    An unimportable vendor module fails closed.
    """
    try:
        from musebridge.host_stop import ensure_host_allows
    except ImportError as error:
        raise RuntimeError(
            "Deskhand STOP authority is unavailable in this process; "
            "agent fallback refused") from error
    try:
        ensure_host_allows()
    except ValueError as error:
        raise RuntimeError(
            "Host STOP is set; agent fallback refused (%s)"
            % _truncate(error, 120)) from error


def bridge_stop_allows(data_dir):
    """Bridge-reachability check for the on-PC runner (fresh process).

    The vendor check cannot run here: the STOP event name is a fresh
    GUID per app start, known only to the running app/service, and the
    vendor check silently passes when DESKHAND_JETT_RUNTIME is unset.
    Instead, read the vendor bridge operator state on loopback
    (runtime.json + operator.secret, both under the service data
    dir's muse-runtime/bridge) and refuse when the state cannot be
    read at all. STOP latch removed (Phil 2026-11-09): no STOP field
    is read or consulted here -- a bridge state without one is fine,
    which matters now the vendor bridge no longer reports one. The
    operator secret is held in memory only and never appears in any
    message.
    """
    root = Path(data_dir) / "muse-runtime"
    home = root / "bridge"
    try:
        runtime = json.loads((home / "runtime.json").read_text(encoding="utf-8"))
        port = runtime.get("operator_port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("no valid operator port")
        token = (home / "operator.secret").read_text(encoding="ascii").strip()
        if not token:
            raise ValueError("no operator secret")
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "cannot verify bridge state: bridge runtime unreadable (%s); "
            "agent fallback refused" % _truncate(error, 120)) from error
    origin = "http://127.0.0.1:%d" % port
    request = urllib.request.Request(
        origin + "/api/state?compact=1",
        headers={"Authorization": "Bearer " + token, "Origin": origin},
        method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            state = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(
            "cannot verify bridge state: bridge state unreachable (%s); "
            "agent fallback refused" % _truncate(error, 120)) from error
    if not isinstance(state, dict):
        raise RuntimeError(
            "cannot verify bridge state: bridge state is not an object; "
            "agent fallback refused")
    return True


def _receipt_summary(kind, result):
    """Short, safe journal summary of a receipt. Never image data,
    never stdout content (length only), never a token."""
    if not isinstance(result, dict):
        return {"result": _truncate(result, 120)}
    if kind in ("open_app", "open_notepad"):
        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        return {"pid": inner.get("pid"), "exe": inner.get("exe"),
                "title": _truncate(inner.get("title", ""), 80)}
    if kind == "exec":
        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        return {"exit_code": inner.get("exit_code"),
                "stdout_len": len(str(inner.get("stdout", "")))}
    if kind == "screenshot":
        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        return {"width": inner.get("width"), "height": inner.get("height"),
                "bytes": inner.get("bytes")}
    return {"ok": result.get("ok")}


# Errors raised BEFORE an acting request is sent: dispatch provably
# never started, so the claim settles ``failed`` and a retry of the
# same payload is allowed. Any other dispatch exception settles
# ``uncertain`` (no replay until operator resolution).
# B2-D H14: TaskAgentMalformedResponse is DELIBERATELY absent here --
# a malformed 200 means the request was sent and may have executed,
# so it must settle "uncertain", never "failed". Bare ValueError stays
# only for pre-dispatch validation (bad exe/args, unknown kind);
# _request() no longer lets a decode failure escape as ValueError.
_FAILED_BEFORE_DISPATCH = (
    DispatchGuardRefused,
    TaskAgentDenied,
    TaskAgentNotEnabled,
    TaskAgentHealthUnavailable,
    TaskAgentConfigError,
    TaskAgentBadScreenshot,
    ValueError,
)


def run_task_agent_action(data_dir, command, *, host_allows=None,
                          grant_reader=None, client_factory=None,
                          config_path=None, claim_store_dir=None):
    """Run one fallback action through the laptop agent (v2).

    command: {"task_id": <host-issued id, required>,
              "action": {"kind": ..., ...},
              "gateway_state": <snapshot recorded in the journal,
                                optional>,
              "approval": {"basis": "phil_explicit_request",
                           "task_id": <same task id>,
                           "action_hash": <optional caller hash>}
                           -- attestation carried from the caller;
                           the op recomputes the payload hash itself
                           and records it. A caller hash that does not
                           match the presented action is refused
                           (transport corruption / substitution); a
                           matching caller hash proves nothing about
                           Phil's approval, because the caller
                           computed it.}
    host_allows: callable raising unless host STOP is clear (default:
        vendor ensure_host_allows). grant_reader: callable(data_dir)
        returning bool (default: read_deskhand_grant).
        client_factory: callable(grant_on_bool) -> TaskAgentClient-like
        with run_action(action) (default: real client on loopback).
        claim_store_dir: claim store location (default:
        <data_dir>/task_agent_claims).
    Returns the receipt {"ok", "route", "task_id", "kind",
    "action_id", "result", "cancellable": False}. The fallback has no
    cancel endpoint, so an in-flight action cannot be interrupted
    (100.025 S4); the receipt says so on every executed action, not
    only on anomalies. Raises TaskAgentFallbackRefused for
    refusals, the original client error for execution failures.
    Every call is journaled; dispatch outcomes also settle the claim.
    """
    data_dir = Path(data_dir)
    command = command if isinstance(command, dict) else {}
    task_id = command.get("task_id")
    action = command.get("action")
    gateway_state = command.get("gateway_state", "unknown")
    approval = command.get("approval")
    kind = action.get("kind") if isinstance(action, dict) else None
    # action_id is computable as soon as task_id + action are present,
    # so even early refusals can carry it in the journal.
    action_id = None
    if isinstance(task_id, str) and task_id.strip() \
            and isinstance(action, dict):
        try:
            action_id = canonical_action_id(task_id, action)
        except Exception:
            action_id = None

    def journal(outcome, detail=None, claim_state=None):
        entry = {"task_id": task_id if isinstance(task_id, str) else None,
                 "action_id": action_id, "kind": kind,
                 "gateway_state": gateway_state, "outcome": outcome,
                 "claim_state": claim_state}
        if approval is not None:
            entry["approval_basis"] = approval.get("basis") \
                if isinstance(approval, dict) else "malformed"
        if detail is not None:
            entry["detail"] = detail
        _journal(data_dir, entry)

    def refuse(reason, claim_state=None):
        journal("refused", _truncate(reason, 200), claim_state)
        raise TaskAgentFallbackRefused(reason)

    checker = host_allows or _default_host_allows
    reader = grant_reader or read_deskhand_grant

    def read_grant():
        try:
            if config_path is None:
                return bool(reader(data_dir))
            return bool(reader(data_dir, config_path))
        except TypeError:
            return bool(reader(data_dir))
        except Exception:
            return False

    def check_stop():
        """Entry/boundary STOP check. Raises RuntimeError unless clear."""
        checker()

    def dispatch_guard():
        """Per-call dispatch-boundary guard (installed on the client).

        Re-checks STOP + grant immediately before an acting request
        is sent. Raises DispatchGuardRefused when the boundary check
        fails -- the request is then never sent.
        """
        try:
            check_stop()
        except RuntimeError as error:
            raise DispatchGuardRefused(
                "STOP set at dispatch boundary; agent fallback refused "
                "(%s)" % _truncate(error, 120)) from error
        if not read_grant():
            raise DispatchGuardRefused(
                "Deskhand Full PC access went off at dispatch boundary; "
                "agent fallback refused")

    with _LOCK:
        # -- Phase 0: validation (no claim; retry is free) ----------
        if not isinstance(task_id, str) or not task_id.strip():
            refuse("a host-issued task/turn ID is required; agent fallback refused")
        if not isinstance(action, dict) or not isinstance(kind, str) or not kind:
            refuse("action kind is required; agent fallback refused")
        if kind in REFUSED_KINDS:
            refuse(
                "action kind %r is not available through the laptop-agent "
                "fallback (no typing/clicking); it stays with the gateway "
                "worker, which is down" % kind)
        if kind not in FALLBACK_KINDS:
            refuse("unknown action kind %r; agent fallback refused" % kind)
        approval_record = None
        if approval is not None:
            if not isinstance(approval, dict):
                refuse("approval must be an object; agent fallback refused")
            if approval.get("task_id") is not None \
                    and approval.get("task_id") != task_id:
                refuse("approval task_id does not match the task; "
                       "agent fallback refused")
            caller_hash = approval.get("action_hash")
            if caller_hash is not None \
                    and caller_hash != payload_hash(action):
                refuse("approval action_hash does not match the presented "
                       "action; agent fallback refused")
            approval_record = {
                "basis": approval.get("basis") or "task_id_only",
                "task_id": task_id,
                # Hash computed BY THE OP from the presented action.
                "action_hash": payload_hash(action),
                "caller_action_hash_match":
                    None if caller_hash is None else True,
            }
        else:
            approval_record = {
                "basis": "task_id_only",
                "task_id": task_id,
                "action_hash": payload_hash(action),
                "caller_action_hash_match": None,
            }

        # -- Phase 1: entry gates (no claim; retry is free) ---------
        try:
            check_stop()
        except RuntimeError as error:
            refuse(str(error))
        if not read_grant():
            refuse("Deskhand Full PC access is off; agent fallback refused")

        # -- Phase 2: atomic claim ----------------------------------
        store = ClaimStore(
            claim_store_dir or (data_dir / CLAIM_DIR_NAME))
        try:
            claim, owned = store.claim(
                task_id, action, route=ROUTE,
                gateway_state=gateway_state,
                approval=approval_record)
        except ClaimConflict as error:
            refuse(str(error))
        except (ClaimStateError, ValueError) as error:
            refuse("claim store refused the action: %s" % _truncate(error, 160))
        if not owned:
            state = claim.get("state")
            if state == "executed":
                refuse(
                    "task %s already executed via the agent fallback; "
                    "not re-executed" % _truncate(task_id, 80),
                    claim_state="executed")
            # claimed (in-flight or crashed) and uncertain are both
            # "do not assume, do not replay".
            refuse(
                "task %s has a %s fallback claim; outcome unknown -- "
                "no replay until an operator resolves it"
                % (_truncate(task_id, 80), state),
                claim_state=state)

        def settle_failed(detail):
            """Settle "failed". B2-D H13: a settle failure is SURFACED
            (RuntimeError), never swallowed into a journal line -- a
            failed settle leaves the claim "claimed" while the caller
            believes the task is retryable, which used to brick the
            task ID (see H12). Callers attach the dispatch outcome."""
            try:
                store.settle(task_id, action_id, "failed", detail)
            except ClaimStateError as error:
                journal("failed", "claim settle failed: " + _truncate(detail, 120),
                        "claimed")
                raise RuntimeError(
                    "claim settle failed (%s); claim remains 'claimed'"
                    % _truncate(error, 120)) from error

        def settle_uncertain(detail):
            """Settle "uncertain". B2-D H13: surfaces a settle failure
            (RuntimeError) like settle_failed; see above."""
            try:
                store.settle(task_id, action_id, "uncertain", detail)
            except ClaimStateError as error:
                journal("uncertain", "claim settle failed: " + _truncate(detail, 120),
                        "claimed")
                raise RuntimeError(
                    "claim settle failed (%s); claim remains 'claimed'"
                    % _truncate(error, 120)) from error

        # -- Phase 3: dispatch-boundary recheck ---------------------
        # STOP / grant may have changed between the entry check and
        # the claim. A failure here settles ``failed`` (dispatch
        # never started; retry of the same payload is allowed).
        try:
            check_stop()
        except RuntimeError as error:
            try:
                settle_failed("STOP at dispatch boundary: %s"
                              % _truncate(error, 120))
            except RuntimeError as settle_error:
                # B2-D H13: the boundary refusal stands, but the claim
                # did NOT settle -- say so instead of reporting failed.
                refuse("%s; claim settle also failed (%s)"
                       % (error, settle_error), claim_state="claimed")
            refuse(str(error), claim_state="failed")
        if not read_grant():
            try:
                settle_failed("grant off at dispatch boundary")
            except RuntimeError as settle_error:
                refuse("Deskhand Full PC access is off; agent fallback "
                       "refused; claim settle also failed (%s)"
                       % settle_error, claim_state="claimed")
            refuse("Deskhand Full PC access is off; agent fallback refused",
                   claim_state="failed")

        # -- Phase 4: dispatch (exactly one attempt) ----------------
        factory = client_factory or (lambda grant_on: TaskAgentClient(
            base_url=DEFAULT_AGENT_BASE, deskhand_grant_on=grant_on,
            timeout=DEFAULT_TIMEOUT))
        # B2-D M4: the client is constructed with the LIVE grant state,
        # not a hardcoded True -- the old factory(True) made the
        # client's own Deskhand-grant check vacuous on this path, so
        # only the op's checks enforced the grant. A grant flip between
        # the boundary recheck and this line now refuses inside the
        # client instead of being silently passed.
        client = factory(read_grant())
        setter = getattr(client, "set_dispatch_guard", None)
        if callable(setter):
            setter(dispatch_guard)
        else:
            try:
                client.dispatch_guard = dispatch_guard
            except Exception:
                pass
        try:
            # Exactly one attempt. No retry, including on 401.
            receipt = client.run_action(action)
        except _FAILED_BEFORE_DISPATCH as error:
            # Proven non-execution of the acting call (or read-only
            # screenshot). Multi-call actions (open_app) may already
            # have run a preparatory call -- say so, do not hide it.
            detail = "%s: %s (dispatch never sent this call; a " \
                     "preparatory call in the same action, if any, " \
                     "may already have run)" % (
                         type(error).__name__, _truncate(error, 160))
            try:
                settle_failed(detail)
            except RuntimeError as settle_error:
                # B2-D H13: the dispatch failed AND the settle failed.
                # Report both: the claim stays "claimed" (no retry
                # until stale-claim expiry or operator resolution),
                # which the plain dispatch error would have hidden.
                _journal(data_dir, {
                    "task_id": task_id, "action_id": action_id, "kind": kind,
                    "gateway_state": gateway_state, "outcome": "failed",
                    "claim_state": "claimed",
                    "approval_basis": approval_record["basis"],
                    "detail": detail + " -- claim settle failed: "
                              + str(settle_error)})
                raise RuntimeError(
                    "claim settle failed after dispatch failure (%s); "
                    "dispatch error was %s: %s; claim remains 'claimed'"
                    % (settle_error, type(error).__name__,
                       _truncate(error, 160))) from error
            _journal(data_dir, {
                "task_id": task_id, "action_id": action_id, "kind": kind,
                "gateway_state": gateway_state, "outcome": "failed",
                "claim_state": "failed",
                "approval_basis": approval_record["basis"],
                "detail": detail})
            raise
        except Exception as error:
            # Dispatch started (or may have started) and the outcome
            # is unknown: timeout, transport error, 401, anything
            # else. Settle UNCERTAIN -- no replay until an operator
            # resolves the claim with independent evidence.
            detail = "%s: %s" % (type(error).__name__,
                                 _truncate(error, 160))
            try:
                settle_uncertain(detail)
            except RuntimeError as settle_error:
                # B2-D H13: as in the failed path -- report the settle
                # failure on top of the uncertain dispatch outcome.
                _journal(data_dir, {
                    "task_id": task_id, "action_id": action_id, "kind": kind,
                    "gateway_state": gateway_state, "outcome": "uncertain",
                    "claim_state": "claimed",
                    "approval_basis": approval_record["basis"],
                    "detail": detail + " -- claim settle failed: "
                              + str(settle_error) + " -- outcome unknown; "
                              "no replay until an operator resolves the claim"})
                raise RuntimeError(
                    "claim settle failed after uncertain dispatch (%s); "
                    "dispatch error was %s: %s; claim remains 'claimed'"
                    % (settle_error, type(error).__name__,
                       _truncate(error, 160))) from error
            _journal(data_dir, {
                "task_id": task_id, "action_id": action_id, "kind": kind,
                "gateway_state": gateway_state, "outcome": "uncertain",
                "claim_state": "uncertain",
                "approval_basis": approval_record["basis"],
                "detail": detail + " -- outcome unknown; no replay "
                          "until an operator resolves the claim"})
            raise
        # Post-dispatch STOP/grant read: an in-flight action cannot
        # be interrupted, so a STOP that landed mid-flight does not
        # un-execute it -- but it is recorded as an anomaly on the
        # executed receipt, and the NEXT action is refused.
        post_anomaly = {}
        try:
            check_stop()
        except RuntimeError:
            post_anomaly["stop_set_during_execution"] = True
        if not read_grant():
            post_anomaly["grant_off_after_dispatch"] = True
        try:
            store.settle(task_id, action_id, "executed",
                         _receipt_summary(kind, receipt))
        except ClaimStateError as error:
            # B2-D H13: the action EXECUTED but the settle failed, so
            # the claim still says "claimed". Returning success would
            # hide that the durable record disagrees with the receipt;
            # raise instead, stating plainly that the action ran and
            # must NOT be retried (stale-claim expiry or operator
            # resolution recovers the task ID).
            journal("executed", "claim settle failed after dispatch",
                    "claimed")
            raise RuntimeError(
                "claim settle failed after dispatch: the action EXECUTED "
                "but its claim remains 'claimed' (%s); do not retry -- "
                "resolve via operator or stale-claim expiry"
                % _truncate(error, 120)) from error
        entry = {"task_id": task_id, "action_id": action_id, "kind": kind,
                 "gateway_state": gateway_state, "outcome": "executed",
                 "claim_state": "executed", "cancellable": False,
                 "approval_basis": approval_record["basis"],
                 "detail": _receipt_summary(kind, receipt)}
        if post_anomaly.get("stop_set_during_execution"):
            # 100.025 S4: human sentence for the journal expectation.
            # The post_dispatch_anomaly flag above is the machine
            # record; this is the readable line. detail is a summary
            # dict, so the sentence rides in it as "note" (its type
            # is unchanged for existing readers).
            detail = entry["detail"]
            if isinstance(detail, dict):
                entry["detail"] = {**detail, "note": "STOP arrived mid-action — completed anyway"}
        if post_anomaly:
            entry["post_dispatch_anomaly"] = post_anomaly
        _journal(data_dir, entry)
        result = {"ok": True, "route": ROUTE, "task_id": task_id,
                  "kind": kind, "action_id": action_id, "result": receipt,
                  "cancellable": False}
        if post_anomaly:
            result["post_dispatch_anomaly"] = post_anomaly
        return result
