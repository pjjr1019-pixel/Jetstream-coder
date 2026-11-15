"""App-owned JSON-lines service over inherited stdin/stdout, not a listener."""
import argparse
import json
import logging
import logging.handlers
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from deskhand_dispatch import Dispatcher, Reply, Route, StopConfirmed
from deskhand_mailbox import Mailbox
from deskhand_codex import CodexChat, catalog
from deskhand_host_rpc import HostRpc
from deskhand_agent_session import run_agent_session
from deskhand_local import LocalChat
from deskhand_local_tools import LocalToolAdapter, LocalToolTransport
from deskhand_openai_local import OpenAILocalChat, OpenAILocalToolTransport
from deskhand_runtime import ManagedRuntime
from deskhand_cloud_tools import HostToolChat
from deskhand_router import classify, suggest_route

try:
    import deskhand_diagnostics
except ImportError:  # pragma: no cover - publish includes *.py (STEP0 0.4)
    deskhand_diagnostics = None

try:
    import deskhand_failfast
except ImportError:  # pragma: no cover - publish includes *.py
    deskhand_failfast = None

try:
    import deskhand_worker_watchdog
except ImportError:  # pragma: no cover - publish includes *.py
    deskhand_worker_watchdog = None

# B42 (100.050 tunnel): the tunnel provider + HTTP client live in
# deskhand_tunnel.py, beside this file. The import is optional
# like the other sibling modules: if it is missing, the tunnel
# reads as disabled and the relay path is untouched.
try:
    import deskhand_tunnel
except ImportError:  # pragma: no cover - publish includes *.py
    deskhand_tunnel = None


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 100.029 (E17 log retention + E41 structured logging) — retention adapter
# ---------------------------------------------------------------------------
# The shared helpers live in deskhand_retention.py (Worker A): the fixed
# vocabularies, truncate/scrub, rotate_if_needed, prune_jett_journal,
# the debug-session helpers, and the canonical line formatters. This
# module imports them by exactly that interface. When the module is
# not beside this file yet (Worker A still writing it), the local
# fallbacks below implement the same interface so the service still
# runs and the VM tests still exercise the same call sites; the
# fallbacks are used only while the import fails, never alongside it.
try:
    import deskhand_retention as _retention
    _RETENTION_LEVELS = frozenset(
        getattr(_retention, "LEVELS", ("DEBUG", "INFO", "WARN", "ERROR")))
except Exception:  # pragma: no cover - retention module not present yet
    _retention = None
    _RETENTION_LEVELS = frozenset({"DEBUG", "INFO", "WARN", "ERROR"})


def _truncate_text(text, limit=200):
    """Truncate to `limit` chars (retention helper when available)."""
    if _retention is not None:
        try:
            return _retention.truncate_text(text, limit=limit)
        except Exception:
            pass
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit]


def _scrub_text(text):
    """Redact token-shaped values (retention helper when available)."""
    if _retention is not None:
        try:
            return _retention.scrub_text(text)
        except Exception:
            pass
    return "" if text is None else str(text)

def _coerce_sent_at(value, received_at):
    """Validate a phone-stamped send time (100.039, fail-soft).

    Returns the epoch-millis int, or None when the value is missing
    or invalid: not an int (bool excluded), <= 0, or more than 5 min
    ahead of the PC receipt time (clock-skew tolerance). A bad
    sent_at must never raise the submit — the mailbox stores NULL.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > received_at + 300_000:
        return None
    return value


# Event-stream rotation (E17 §2 #2): vm_events.jsonl, 5 MB x 3 / 7 days
# (the age cap is the janitor's job, not the writer's).
VM_EVENTS_MAX_BYTES = 5 * 1024 * 1024
VM_EVENTS_BACKUP_COUNT = 3
_ROTATION_WARNED = set()


def _log_rotation_failure_once(path, error):
    """A rotation failure is an ERROR-level event, logged once (E41 §2.1).

    A failing rotator must not become a log-spam source: one warning
    per path per process, and the writer keeps appending.
    """
    key = str(path)
    if key in _ROTATION_WARNED:
        return
    _ROTATION_WARNED.add(key)
    logger.warning("Log rotation failed for %s (%s); keep appending",
                   path, error)


def _rotate_log_file(path, max_bytes, backup_count):
    """Rotate one log file via the retention helper (or the local
    rename-shift fallback when it is not beside this file yet).

    Rename, never truncate in place (E17 §3.1): generations shift
    ``.1`` -> ``.2`` -> ... and the oldest past the count is deleted;
    the current file plus ``.1`` are always present. A failure keeps
    appending to the current file and warns once. Never raises.
    Returns True when no rotation was needed or it succeeded.
    """
    if _retention is not None:
        try:
            rotated = _retention.rotate_if_needed(path, max_bytes,
                                                  backup_count)
            if rotated:
                return True
            # False means "no rotation needed" OR "rotation refused".
            # Tell them apart by size: a file still over the cap
            # after a declined rotation is a failure (warn once, keep
            # appending); a file under the cap needed nothing.
            try:
                target = Path(path)
                if (target.exists()
                        and target.stat().st_size > max_bytes):
                    _log_rotation_failure_once(
                        path, "rotate_if_needed declined")
                    return False
            except OSError:
                pass
            return True
        except Exception as error:  # noqa: BLE001 - rotation must not raise
            _log_rotation_failure_once(path, error)
            return False
    try:
        target = Path(path)
        if not target.exists() or target.stat().st_size <= max_bytes:
            return True
        oldest = target.with_name("%s.%d" % (target.name, backup_count))
        if oldest.exists():
            oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = target.with_name("%s.%d" % (target.name, generation))
            if src.exists():
                src.replace(target.with_name(
                    "%s.%d" % (target.name, generation + 1)))
        target.replace(target.with_name(target.name + ".1"))
        return True
    except Exception as error:  # noqa: BLE001 - rotation must not raise
        _log_rotation_failure_once(path, error)
        return False


# --- Service-stderr sink (E41 §3.10, D2) --------------------------------
# SINK CHOICE (b): the service gains a RotatingFileHandler itself at
# <service data dir>/deskhand_service.log (maxBytes 5 MB, backupCount 2,
# 7 days). The host writes nothing (one sink, not both). This is the
# only sink where DEBUG is written by default-allowed: Python `logging`
# output lands here with its levels intact, in the plain-text shape
# (§2.4) via the standard logging formatter. Default level INFO;
# DEBUG is session-scoped (§4). The existing
# TRANSPORT_LOG.setLevel(logging.INFO) in the vendor bridge stays.
SERVICE_LOG_NAME = "deskhand_service.log"
SERVICE_LOG_MAX_BYTES = 5 * 1024 * 1024
SERVICE_LOG_BACKUP_COUNT = 2
_SERVICE_LOG_HANDLER = None
_SERVICE_LOG_PATH = None


def _service_log_formatter():
    """Plain-text shape (§2.4): [ts] [LEVEL] [component] msg, UTC ISO."""
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ")
    formatter.converter = time.gmtime
    return formatter


def _ensure_service_log_handler(data_dir):
    """Attach the service-stderr sink to the root logger. Never raises.

    Rebinds when the data dir changes (tests construct several
    services in one process); a second call for the same path is a
    no-op. The root level is INFO by default so INFO records from
    every module logger propagate; a live debug session (§4) raises
    it to DEBUG via _apply_debug_levels().
    """
    global _SERVICE_LOG_HANDLER, _SERVICE_LOG_PATH
    try:
        path = Path(data_dir) / SERVICE_LOG_NAME
        if _SERVICE_LOG_HANDLER is not None and _SERVICE_LOG_PATH == path:
            return _SERVICE_LOG_HANDLER
        root = logging.getLogger()
        if _SERVICE_LOG_HANDLER is not None:
            try:
                root.removeHandler(_SERVICE_LOG_HANDLER)
                _SERVICE_LOG_HANDLER.close()
            except Exception:
                pass
            _SERVICE_LOG_HANDLER = None
            _SERVICE_LOG_PATH = None
        for handler in list(root.handlers):
            if (isinstance(handler, logging.handlers.RotatingFileHandler)
                    and getattr(handler, "baseFilename", "") == str(path)):
                _SERVICE_LOG_HANDLER = handler
                _SERVICE_LOG_PATH = path
                return handler
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=SERVICE_LOG_MAX_BYTES,
            backupCount=SERVICE_LOG_BACKUP_COUNT, encoding="utf-8")
        handler.setFormatter(_service_log_formatter())
        handler.setLevel(logging.DEBUG if _debug_is_enabled("service")
                         else logging.INFO)
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        _SERVICE_LOG_HANDLER = handler
        _SERVICE_LOG_PATH = path
        return handler
    except Exception:  # noqa: BLE001 - a missing sink must not break start
        return None


def _release_service_log_handler(data_dir):
    """Detach the service-stderr sink bound to `data_dir`. Never raises."""
    global _SERVICE_LOG_HANDLER, _SERVICE_LOG_PATH
    try:
        path = Path(data_dir) / SERVICE_LOG_NAME
        if _SERVICE_LOG_HANDLER is None or _SERVICE_LOG_PATH != path:
            return
        root = logging.getLogger()
        try:
            root.removeHandler(_SERVICE_LOG_HANDLER)
            _SERVICE_LOG_HANDLER.close()
        except Exception:
            pass
        _SERVICE_LOG_HANDLER = None
        _SERVICE_LOG_PATH = None
    except Exception:
        pass


# --- Debug sessions (E41 §4) --------------------------------------------
# DEBUG is a session, never a setting: one sink, one reason, expires
# after 1 hour or at component restart, whichever comes first. Sessions
# are in-memory only — a restart resets them. DEBUG is never enabled
# remotely without Phil asking; the `debug_session` op is the
# mechanism, the chat request is the authorization. Only Phil may
# enable one (actor "phil").
_DEBUG_SESSION_TTL_SECS = 3600
_DEBUG_SESSIONS = {}  # fallback store, used only when deskhand_retention
# is not importable: sink -> record dict (in-memory, restart resets).


def _debug_normalize_sink(sink):
    """The sink names this service owns: the service log only."""
    if sink in ("service", "service_log", "service-log", SERVICE_LOG_NAME,
                "deskhand_service"):
        return "service"
    raise ValueError(
        "unknown debug sink %r (this service owns the 'service' sink only)"
        % (sink,))


def _debug_iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _debug_public_record(record):
    """The session record as returned to a caller (no private keys)."""
    if not isinstance(record, dict):
        return None
    return {key: value for key, value in record.items()
            if not str(key).startswith("_")}


def _debug_coerce_record(record, sink, reason=None, actor="phil"):
    """Normalize a retention-helper record to the op's record shape."""
    if not isinstance(record, dict):
        record = {}
    now = time.time()
    started = record.get("started_at")
    expires = record.get("expires_at")
    if isinstance(started, (int, float)):
        started = _debug_iso(started)
    if isinstance(expires, (int, float)):
        expires = _debug_iso(expires)
    merged = {
        "sink": record.get("sink") or sink,
        "actor": record.get("actor") or actor,
        "reason": record.get("reason") or reason or "",
        "started_at": started or _debug_iso(now),
        "expires_at": expires or _debug_iso(now + _DEBUG_SESSION_TTL_SECS),
        "level": "DEBUG",
        "enabled": True,
    }
    merged.update({key: value for key, value in record.items()
                   if key in merged and record.get(key) not in (None, "")})
    return merged


def _debug_enable_session(sink, reason, actor="phil"):
    """Enable a 1-hour debug session; returns the session record."""
    sink = _debug_normalize_sink(sink)
    if _retention is not None:
        try:
            record = _retention.enable_debug_session(
                sink, reason, actor=actor)
            if isinstance(record, dict):
                return _debug_coerce_record(record, sink, reason, actor)
        except Exception:
            pass  # fall through to the local store, never fail the op
    now = time.time()
    record = {"sink": sink, "actor": actor, "reason": reason,
              "started_at": _debug_iso(now),
              "expires_at": _debug_iso(now + _DEBUG_SESSION_TTL_SECS),
              "level": "DEBUG", "enabled": True,
              "_expires_epoch": now + _DEBUG_SESSION_TTL_SECS}
    _DEBUG_SESSIONS[sink] = record
    return _debug_public_record(record)


def _debug_is_enabled(sink):
    """True only while a debug session for `sink` is live (not expired)."""
    try:
        sink = _debug_normalize_sink(sink)
    except ValueError:
        return False
    if _retention is not None:
        try:
            return bool(_retention.is_debug_enabled(sink))
        except Exception:
            pass
    record = _DEBUG_SESSIONS.get(sink)
    if not isinstance(record, dict):
        return False
    if time.time() >= record.get("_expires_epoch", 0):
        del _DEBUG_SESSIONS[sink]
        return False
    return True


def _debug_session_record(sink):
    """The live session record for `sink`, or None when there is none."""
    try:
        sink = _debug_normalize_sink(sink)
    except ValueError:
        return None
    if _retention is not None:
        try:
            record = _retention.debug_session_record(sink)
            # The helper returns {} when there is no live session;
            # an empty record is "no session", never a session to
            # coerce defaults into (that would invent a live one).
            if not record:
                return None
            if isinstance(record, dict):
                return _debug_coerce_record(record, sink)
            return None
        except Exception:
            pass
    if not _debug_is_enabled(sink):
        return None
    return _debug_public_record(_DEBUG_SESSIONS.get(sink))


def _apply_debug_levels():
    """Apply the current debug-session state to the service log sink.

    Called on every op and on the watchdog cadence so an expired
    session reverts the sink to INFO within seconds of its 1-hour
    cap (and a restart resets it outright: the store is in-memory).
    Never raises.
    """
    try:
        handler = _SERVICE_LOG_HANDLER
        if handler is None:
            return
        enabled = _debug_is_enabled("service")
        handler.setLevel(logging.DEBUG if enabled else logging.INFO)
        logging.getLogger().setLevel(
            logging.DEBUG if enabled else logging.INFO)
    except Exception:
        pass


# --- Jett journal prune (E17 §3.4) ---------------------------------------
# Pruning is a service-side job, not a file operation: a weekly
# `DELETE ... WHERE ts < <30 days ago>` + `VACUUM`, run by the service
# itself (it already owns the connection). Never by the janitor — the
# janitor touching a live WAL database from a second process is a
# corruption risk. Prune only when the service is idle (no turn
# running). Never delete the .db file itself, never delete the WAL
# files by hand. The prune is level-blind: it deletes by age only.
_JETT_PRUNE_INTERVAL_SECS = 7 * 24 * 3600
_JETT_PRUNE_STATE_NAME = "jett-prune-state.json"


def _jett_journal_db_paths(data_dir):
    """Existing jett-operator.db candidates (service dir, then parent)."""
    base = Path(data_dir)
    return [path for path in (base / "jett-operator.db",
                              base.parent / "jett-operator.db")
            if path.exists()]


def _prune_jett_journal(db_path, max_age_days=30):
    """Delete Jett-journal rows older than `max_age_days`, then VACUUM.

    Delegates to deskhand_retention.prune_jett_journal when it is
    available; the local fallback handles the same schema (an
    `events` table with a `ts` column, else the live schema's
    `happened_utc` ISO-text column). Never deletes the .db or WAL
    files. Returns {"pruned": n, "db": path}; a missing file or a
    table with no rows prunes nothing and returns 0. Raises
    RuntimeError ONLY when the live append-only trigger refuses the
    DELETE (Worker A contract): that refusal is C#-owned and must
    not be silently recorded as a successful prune -- the caller
    logs it and retries next week. Any other failure is logged and
    returns {"pruned": None, ...}.
    """
    if _retention is not None:
        try:
            result = _retention.prune_jett_journal(
                db_path, max_age_days=max_age_days)
            if isinstance(result, dict):
                return result
            if isinstance(result, int):
                return {"pruned": result, "db": str(db_path)}
            return {"pruned": None, "db": str(db_path)}
        except RuntimeError:
            raise
        except Exception as error:  # noqa: BLE001 - prune must not raise
            logger.warning("Jett journal prune failed for %s: %s",
                           db_path, error)
            return {"pruned": None, "db": str(db_path), "error": str(error)}
    try:
        cutoff_epoch = time.time() - max_age_days * 86400
        connection = sqlite3.connect(str(db_path), timeout=10)
        try:
            has_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='events'").fetchone()
            if not has_table:
                return {"pruned": 0, "db": str(db_path),
                        "skipped": "no events table"}
            columns = [row[1] for row in connection.execute(
                "PRAGMA table_info(events)").fetchall()]
            if "ts" in columns:
                column = "ts"
            elif "happened_utc" in columns:
                column = "happened_utc"
            else:
                return {"pruned": None, "db": str(db_path),
                        "error": "events table has no ts / happened_utc column"}
            first = connection.execute(
                "SELECT %s FROM events WHERE %s IS NOT NULL LIMIT 1"
                % (column, column)).fetchone()
            pruned = 0
            if first is not None:
                sample = first[0]
                if isinstance(sample, (int, float)):
                    cutoff = cutoff_epoch
                    query = "DELETE FROM events WHERE %s < ?" % column
                elif isinstance(sample, str) and sample.strip().replace(
                        ".", "", 1).isdigit():
                    cutoff = cutoff_epoch
                    query = ("DELETE FROM events WHERE CAST(%s AS REAL) < ?"
                             % column)
                else:
                    cutoff = datetime.fromtimestamp(
                        cutoff_epoch, tz=timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%S")
                    query = ("DELETE FROM events WHERE substr(%s, 1, 19) < ?"
                             % column)
                try:
                    cursor = connection.execute(query, (cutoff,))
                    pruned = cursor.rowcount
                except sqlite3.Error as error:
                    if "append-only" in str(error):
                        raise RuntimeError(
                            "Jett journal prune refused by the live "
                            "append-only trigger: pruning needs a C#-side "
                            "decision first; no rows were deleted (%s)"
                            % error) from error
                    raise
            connection.commit()
            connection.execute("VACUUM")
            connection.commit()
        finally:
            connection.close()
        return {"pruned": pruned, "db": str(db_path)}
    except RuntimeError:
        raise
    except Exception as error:  # noqa: BLE001 - prune must not raise
        logger.warning("Jett journal prune failed for %s: %s", db_path, error)
        return {"pruned": None, "db": str(db_path), "error": str(error)}


# --- Runtime-stream levels (E41 §2.1/§3.2) -------------------------------
# Grant events carry the §3.2 levels: grant.off by restore failure is
# WARN, grant.on by Phil is INFO, grant.generation_decrease is WARN
# (a suspect event, not a normal flip). DEBUG is NEVER written to the
# event stream: a DEBUG line is dropped, never served, never coerced.
GRANT_EVENT_LEVELS = {"grant.on": "INFO", "grant.off": "WARN",
                      "grant.generation_decrease": "WARN"}


def _derived_event_code(item):
    """A stable code for a WARN/ERROR event that carries no code."""
    etype = item.get("type") if isinstance(item.get("type"), str) else ""
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    if etype == "failfast_refusal":
        return "failfast.refused"
    if etype in ("turn_failed",) or detail.get("status") == "failed":
        return "turn.failed"
    if etype in ("turn_cancelled",) or detail.get("status") == "cancelled":
        return "turn.cancelled"
    if etype in ("turn_unknown",) or detail.get("status") == "unknown":
        return "turn.unknown"
    if detail.get("ok") is False:
        return "bridge.file_failed"
    return None


def _event_level_and_code(item):
    """(level, code) for one merged stream event.

    Grant codes are normative (§3.2) and win over an explicit level;
    otherwise an explicit valid level wins; otherwise the level is
    derived from the event's own outcome. code is present on every
    WARN/ERROR (derived when the writer gave none).
    """
    code = item.get("code") if isinstance(item.get("code"), str) else None
    etype = item.get("type") if isinstance(item.get("type"), str) else ""
    key = code or etype
    if key in GRANT_EVENT_LEVELS:
        return GRANT_EVENT_LEVELS[key], code or key
    level = item.get("level")
    if (isinstance(level, str) and level in _RETENTION_LEVELS
            and level != "DEBUG"):
        if level in ("WARN", "ERROR") and not code:
            code = _derived_event_code(item)
        return level, code
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    if etype == "failfast_refusal":
        return "WARN", code or "failfast.refused"
    if etype == "turn_failed" or detail.get("status") == "failed":
        return "ERROR", code or "turn.failed"
    if etype == "turn_cancelled" or detail.get("status") == "cancelled":
        return "WARN", code or "turn.cancelled"
    if etype == "turn_unknown" or detail.get("status") == "unknown":
        return "WARN", code or "turn.unknown"
    if detail.get("ok") is False:
        return "ERROR", code or "bridge.file_failed"
    return "INFO", code


def _level_event(event):
    """Add level (+ code on WARN/ERROR, + turn_id when present) to one
    stream event. Returns None for an event that must not be served
    (DEBUG). Never invents a turn_id: a line with no turn carries none.
    """
    if event.get("level") == "DEBUG":
        return None
    level, code = _event_level_and_code(event)
    event["level"] = level
    if level in ("WARN", "ERROR") and code:
        event["code"] = code
    turn_id = event.get("turn_id")
    if not turn_id:
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        turn_id = detail.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        event["turn_id"] = turn_id
    return event


# A plain (non-agent) chat turn waits on one Muse reply. Replies normally
# arrive in seconds; the relay's outer bound is 600s, which left a turn
# showing "Waiting for assistant..." for the full 10 minutes when the reply
# path was dead (gateway 401, worker disconnected). The watchdog interrupts
# a plain turn after this long with no reply, so the worst stuck wait is
# ~2 minutes instead of 10. Agent turns are excluded: host actions may
# legitimately run long, and the relay bound still applies to them.
# Tunnel-served turns are excluded too (round 4, 2026-11-12 -- see
# _running_turn_is_tunnel_served): a tunnel reply legitimately takes
# minutes (worker spin-up, long host actions), and the tunnel already
# bounds the wait at reply_timeout_secs (600s). Interrupting one at
# 120s abandoned the wait, so a reply posted afterwards was never
# fetched or acked and the phone showed "Delivery unconfirmed" forever.
STUCK_CHAT_TURN_TIMEOUT_SECS = 120
STUCK_WATCHDOG_INTERVAL_SECS = 15

# 100.037 (E12 §10.4): worker.* event types carried into runtime_stream.
# The re-arm notification (worker_auto_reconnect_rearmed) is journal-only
# and is NOT in this set — it never becomes a stream event.
WORKER_STREAM_EVENT_TYPES = frozenset({
    "worker_disconnected",
    "worker_reconnect_attempt",
    "worker_auto_reconnect_stopped",
    "worker_reconnected",
})


# Bridge-log rotation ONLY (E17 §2 #5, 100.029): 5 MB x 2 generations /
# 7 days (age: janitor). The bridge log stays UNSTRUCTURED — no [LEVEL]
# prefix is added here, and nothing about what is logged changes.
# Rotation is rename + reopen, never truncate in place. The bridge child
# owns its stdout handle, so rotation happens when the handle is
# (re)opened: before opening in _start_bridge and after closing in
# _stop_bridge. A single long bridge run can therefore grow past the cap
# until the next stop/start; the cap is enforced at every (re)start.
# A rotation failure keeps appending and is reported exactly once, as a
# plain (unlevelled) line, so the log stays unstructured.
BRIDGE_LOG_MAX_BYTES = 5 * 1024 * 1024
BRIDGE_LOG_GENERATIONS = 2
_bridge_rotation_failure_logged = False


def _rotate_bridge_log_if_needed(log_path):
    """Size rotation for deskhand_bridge.log (shared helper, E17 §3.2)."""
    global _bridge_rotation_failure_logged
    try:
        if not log_path.exists() or log_path.stat().st_size <= BRIDGE_LOG_MAX_BYTES:
            return
        for gen in range(BRIDGE_LOG_GENERATIONS, 0, -1):
            src = log_path if gen == 1 else Path(str(log_path) + ".%d" % (gen - 1))
            dst = Path(str(log_path) + ".%d" % gen)
            if src.exists():
                # replace() overwrites dst atomically; there is deliberately
                # no pre-delete of the oldest generation — a rename that
                # fails afterwards must not have already destroyed data.
                src.replace(dst)
        _bridge_rotation_failure_logged = False
    except OSError as exc:
        if not _bridge_rotation_failure_logged:
            _bridge_rotation_failure_logged = True
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write("[%s] bridge log rotation failed (%s); keeping current file\n"
                            % (datetime.now(timezone.utc).isoformat(timespec="seconds"), exc))
            except OSError:
                pass

def _task_agent_grant_on(data_dir):
    """Deskhand Full PC access grant, re-read on every call.

    Same source as the fallback hook's op (deskhand_task_agent_op.
    read_deskhand_grant): %LOCALAPPDATA%\\Deskhand\\config.json
    FullPcAccess.Enabled true AND Paused false. Missing, unreadable,
    or malformed config fails closed (False), so toggling the grant
    in the app takes effect without a service restart. The lifecycle
    module never sees a hardcoded True.
    """
    try:
        from deskhand_task_agent_op import read_deskhand_grant
    except ImportError:
        return False
    try:
        return bool(read_deskhand_grant(data_dir))
    except Exception:
        return False


class ChatService:
    def __init__(self, data, fixture=False, host_rpc=None, muse=None,
                 stale_chat_timeout=STUCK_CHAT_TURN_TIMEOUT_SECS,
                 watchdog_interval=STUCK_WATCHDOG_INTERVAL_SECS,
                 task_agent_hooks=None, worker_watchdog_interval=1.0):
        self.data = Path(data)
        # 100.029 (E41 §3.10, sink (b)): the service-stderr sink lives
        # in this data dir; installing it here also covers the
        # dispatcher/logger lines emitted during the rest of __init__.
        # Never raises: a missing sink must not break service start.
        _ensure_service_log_handler(self.data)
        # Last Jett-journal prune epoch (E17 §3.4); None until read
        # from the state file on the first weekly check.
        self._jett_prune_last = None
        # Test-only injection for the task_agent_action op branch
        # (host_allows / grant_reader / client_factory overrides).
        # Never settable from a command: the op takes no such fields.
        self._task_agent_hooks = task_agent_hooks or {}
        # 100.045 (B33): the long-running command Runner, created lazily
        # on the first runner op (see handle()); None until then.
        self._runner = None
        # 100.047 (B35): the byte-exact file Transfer, created lazily
        # on the first transfer op (see handle()); None until then.
        self._transfer = None
        # 100.048 (B36): the built-in verify-this-build Verifier,
        # created lazily on the first verify op (see handle());
        # None until then.
        self._verifier = None
        self.box = Mailbox(self.data / "turns.sqlite")
        self.dispatcher = Dispatcher(self.box, {})
        self.dispatcher.__enter__()
        self.fixture = fixture
        # An explicitly supplied instance is used only by integration fixtures
        # until the host setup wiring has passed its live gates.
        self.muse = muse
        self.muse_error = None
        # B42 (100.050 tunnel): the Muse route exists while the
        # worker is down too, when the tunnel is enabled -- the
        # tunnel provider serves it instead of the dead relay.
        # See _tunnel_enabled() / _muse_provider() below.
        self._tunnel_error = None
        self._tunnel_config_error = None
        self._tunnel_provider = None
        self._legacy_terminal_cursor = None
        self.muse_route = Route('muse', 'muse', 'deskhand', '') if (muse is not None or self._tunnel_enabled()) else None
        if self.muse_route is not None:
            self.dispatcher.adapters[self.muse_route] = HostToolChat(self._muse_provider(), self._agent_turn, self.data / 'agent-exchanges.sqlite')
        self.host_rpc = host_rpc
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.recovery_lock = threading.Lock()
        self.recovery_state = {"status": "idle"}
        # Fail-fast (100.020, D-020-1): memoised health snapshot +
        # as_of, refreshed by every health() call; the submit gate
        # reads the cache when fresh (age <= 6 s) and rebuilds
        # bounded when stale.
        self._health_cache = None
        self._health_cache_lock = threading.Lock()
        # Turn ids already tripped to uncertain by the in-flight trip
        # (D-020-8), so the 15 s watchdog does not re-trip the same turn.
        self._failfast_tripped = set()
        # 100.037 (E12): Muse-worker disconnect watchdog — the dedicated
        # module (deskhand_worker_watchdog.WorkerWatchdog), wired like
        # muse_reconnect_wiring. The 1 s in-process evaluation thread
        # (O4 default) reads MuseConnection.status() only: no HTTP, no
        # new poller, no gateway /status poll (none exists). The timer
        # is a no-op until a Muse worker is attached, and a worker
        # object without a status() signal reads as "missing", never
        # as a disconnect. worker_watchdog_interval=None disables the
        # timer (tests). The watchdog never touches a grant, a tunnel,
        # a process, or any STOP flag (none exists).
        self.worker_watchdog = None
        self.worker_watchdog_interval = worker_watchdog_interval
        self._worker_watchdog_thread = None
        if deskhand_worker_watchdog is not None:
            try:
                self.worker_watchdog = deskhand_worker_watchdog.WorkerWatchdog(
                    reconnect=self._worker_watchdog_reconnect,
                    on_event=self._worker_watchdog_event)
            except Exception as error:
                logger.warning("Worker watchdog unavailable: %s", error)
                self.worker_watchdog = None
        if self.worker_watchdog is not None and worker_watchdog_interval:
            self._worker_watchdog_thread = threading.Thread(
                target=self._worker_watchdog_loop, daemon=True,
                name="worker-watchdog")
            self._worker_watchdog_thread.start()
        self.routes = []
        self.local_routes = {}
        self.local_origins = {}
        self.local_runtime = ManagedRuntime()
        self.catalog_error = None
        self.local_config_error = None
        self._load_local_configuration()
        # MuseBridge (ported laptop-agent bridge) lifecycle. The bridge is a
        # separate child process owned by this service: starting it only starts
        # the listener (health/status answer). Exec/files stay gated by the
        # bridge's own remote_control_enabled flag (default false, local-only
        # toggle) - auto-start never enables remote control by itself.
        # Script resolves next to this file; DESKHAND_BRIDGE_SCRIPT overrides
        # it (tests). Config lives in the service data dir and must already
        # exist with a token - the service never invents one.
        # Interpreter: DESKHAND_BRIDGE_PYTHON first, else "python" on PATH
        # (the PC's system python has websockets/mss/pillow; the app's
        # embedded python does NOT), else this interpreter.
        self.bridge_script = Path(os.environ.get("DESKHAND_BRIDGE_SCRIPT") or
                                  Path(__file__).with_name("deskhand_bridge.py"))
        self.bridge_python = (os.environ.get("DESKHAND_BRIDGE_PYTHON")
                              or shutil.which("python") or sys.executable)
        self.bridge_config = self.data / "deskhand_bridge_config.json"
        self.bridge_proc = None
        self.bridge_log = None
        # Laptop-agent lifecycle (Deskhand-owned). Deskhand starts the
        # agent when Deskhand starts (or adopts the one already
        # running), monitors it, and restarts it if it dies — no
        # separate launcher click. The grant is re-read from the
        # persisted config on every call (see _task_agent_grant_on);
        # no callable / unreadable config means DENY. The module
        # never reads the agent token. If the module is missing, the
        # lifecycle stays unavailable and the fallback hook still
        # works through an agent started elsewhere.
        try:
            from deskhand_task_agent_lifecycle import TaskAgentLifecycle
        except ImportError:
            TaskAgentLifecycle = None
        if TaskAgentLifecycle is not None:
            self.task_agent_lifecycle = TaskAgentLifecycle(
                data_dir=self.data,
                deskhand_grant_on=lambda: _task_agent_grant_on(self.data),
                stopped=lambda: self.stopping.is_set())
        else:
            self.task_agent_lifecycle = None
        # B2-D M3: last lifecycle startup failure, if any (see
        # main()); None when the lifecycle started cleanly.
        self.task_agent_lifecycle_error = None
        self.stale_chat_timeout = stale_chat_timeout
        self.watchdog_interval = watchdog_interval
        # Round 4: turn id the watchdog last logged skipping as
        # tunnel-served (one log line per turn, not one per tick).
        self._tunnel_watchdog_skip_logged_for = None
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()
        self.watchdog = threading.Thread(target=self._watch_stuck_turns, daemon=True)
        self.watchdog.start()
        self._tunnel_receipts_thread = threading.Thread(target=self._reconcile_tunnel_receipts, daemon=True, name="tunnel-receipts")
        self._tunnel_receipts_thread.start()

    def _reconcile_tunnel_receipts(self):
        while not self.stopping.is_set():
            try:
                self._reconcile_tunnel_receipts_once()
            except Exception:
                logger.exception("Tunnel receipt reconciliation failed")
            if self.stopping.wait(5):
                break

    def _reconcile_tunnel_receipts_once(self):
        if deskhand_tunnel is None:
            return
        cfg = deskhand_tunnel.load_tunnel_config(self.data)
        if cfg.get("configuration_error"):
            return
        pending = self.box.pending_tunnel_receipts()
        legacy_unknown = self.box.unmarked_unknown_muse_turns()
        legacy_terminal = self.box.unmarked_terminal_muse_turns(
            self._legacy_terminal_cursor)
        if legacy_terminal:
            last = legacy_terminal[-1]
            self._legacy_terminal_cursor = (last["created"], last["id"])
        if len(legacy_terminal) < 50:
            self._legacy_terminal_cursor = None
        should_reconcile = (self._tunnel_enabled() or pending
                            or legacy_unknown or legacy_terminal)
        if not should_reconcile:
            return
        token = deskhand_tunnel.load_agent_token(self.data)
        client = deskhand_tunnel.TunnelHttpClient(
            cfg.get("agent_url", "http://127.0.0.1:8899"),
            token, timeout_secs=2)
        provider = deskhand_tunnel.TunnelProvider(client, mailbox=self.box)
        for row in legacy_unknown:
            self.box.mark_unknown_muse_for_recovery(row["id"])
        provider.reconcile_receipts(stop_event=self.stopping)
        provider.reconcile_historical_terminals(
            legacy_terminal, stop_event=self.stopping)

    def _tunnel_enabled(self):
        """True only when deskhand_tunnel_config.json says so.

        Re-read on every call (same fail-closed shape as the grant
        reader): a missing, unreadable, or malformed config reads as
        disabled, and a missing deskhand_tunnel module reads as
        disabled. The tunnel can never switch itself on."""
        if deskhand_tunnel is None:
            return False
        try:
            cfg = deskhand_tunnel.load_tunnel_config(self.data)
        except Exception:
            return False
        config_error = cfg.get("configuration_error")
        previous_error = getattr(self, "_tunnel_config_error", None)
        if config_error:
            if config_error != previous_error:
                logger.error("Tunnel disabled by invalid configuration: %s", config_error)
            self._tunnel_config_error = config_error
            self._tunnel_error = config_error
            return False
        self._tunnel_config_error = None
        if getattr(self, "_tunnel_error", None) == previous_error:
            self._tunnel_error = None
        return bool(cfg.get("enabled", False))

    def _muse_provider(self):
        """The provider for the Muse route: relay, or tunnel.

        Armed = enabled in deskhand_tunnel_config.json AND (no Muse
        attached OR force:true). When Muse is attached and force is
        false the live relay wins -- the tunnel is the fallback, and
        a human flips force at deploy time (BUILD-SPEC section 4).
        A tunnel build failure never bricks service start: it is
        stored on self._tunnel_error and the returned provider
        raises the same error on first use instead (see
        deskhand_tunnel.UnavailableProvider)."""
        if self._tunnel_enabled() and deskhand_tunnel is not None:
            cfg = deskhand_tunnel.load_tunnel_config(self.data)
            if self.muse is None or cfg.get("force", False):
                if self._tunnel_provider is None:
                    try:
                        self._tunnel_provider = deskhand_tunnel.TunnelProvider(
                            deskhand_tunnel.TunnelHttpClient(
                                cfg.get("agent_url", "http://127.0.0.1:8899"),
                                deskhand_tunnel.load_agent_token(self.data)),
                            reply_timeout_secs=cfg.get("reply_timeout_secs", 600),
                            poll_interval_secs=cfg.get("poll_interval_secs", 5),
                            mailbox=self.box)
                    except Exception as error:
                        self._tunnel_error = str(error)
                        self._tunnel_provider = deskhand_tunnel.UnavailableProvider(str(error))
                return self._tunnel_provider
        if self.muse is None or getattr(self.muse, "relay", None) is None:
            raise RuntimeError("Muse backend is not ready")
        return self.muse.relay

    def _install_muse_provider(self, provider):
        if self.muse_route is None:
            self.muse_route = Route('muse', 'muse', 'deskhand', '')
        current = self.dispatcher.adapters.get(self.muse_route)
        old_provider = getattr(current, "provider", None)
        if old_provider is provider:
            return
        if deskhand_tunnel is not None and isinstance(old_provider, deskhand_tunnel.TunnelProvider):
            stop = getattr(current, "request_stop", None)
            (stop if callable(stop) else old_provider.request_stop)()
        self.dispatcher.adapters[self.muse_route] = HostToolChat(
            provider, self._agent_turn, self.data / 'agent-exchanges.sqlite')

    def _sync_muse_provider(self):
        self._install_muse_provider(self._muse_provider())

    def _tunnel_status(self):
        """Read-only tunnel state for the tunnel_status op. Never
        includes a token or a key -- only which provider is live and
        whether the agent answers."""
        enabled = self._tunnel_enabled()
        cfg = {}
        if deskhand_tunnel is not None:
            try:
                cfg = deskhand_tunnel.load_tunnel_config(self.data)
            except Exception:
                cfg = {}
        provider = "none"
        adapter = self.dispatcher.adapters.get(self.muse_route) if self.muse_route is not None else None
        wrapped = getattr(adapter, "provider", None) if adapter is not None else None
        if deskhand_tunnel is not None and isinstance(wrapped, deskhand_tunnel.TunnelProvider):
            provider = "tunnel"
        elif wrapped is not None:
            provider = "relay"
        reachable = False
        agent_url = cfg.get("agent_url", "http://127.0.0.1:8899") if isinstance(cfg, dict) else "http://127.0.0.1:8899"
        try:
            request = urllib.request.Request(str(agent_url).rstrip("/") + "/health")
            with urllib.request.urlopen(request, timeout=2) as response:
                reachable = response.status == 200
        except Exception:
            reachable = False
        counts = {}
        if deskhand_tunnel is not None and isinstance(wrapped, deskhand_tunnel.TunnelProvider):
            counts = wrapped.status()
        return {"enabled": enabled,
                "force": bool(cfg.get("force", False)) if isinstance(cfg, dict) else False,
                "agent_url": agent_url,
                "provider": provider,
                "agent_reachable": reachable,
                "tunnel_error": self._tunnel_error,
                "counts": counts,
                "note": "Service uses the full bridge token on loopback; the phone holds no key."}

    def attach_muse(self, muse):
        if muse.relay is None:
            raise RuntimeError('Muse backend is not ready')
        with self.dispatcher.admission_gate:
            if self.muse is not None:
                raise RuntimeError('Muse is already attached')
            self.muse = muse
            self.muse_error = None
            self.muse_route = Route('muse', 'muse', 'deskhand', '')
            self._install_muse_provider(self._muse_provider())
            self.routes = []

    def _register_local(self, model, endpoint):
        if not isinstance(model, str) or not model.strip() or len(model) > 128 or model != model.strip():
            raise ValueError("An explicit local model name is required")
        if not isinstance(endpoint, str):
            raise ValueError("An explicit local server address is required")
        route = Route("local", model, "", "")
        # Managed endpoints are ephemeral. Do not start a process merely to load
        # settings; _local_turn starts or restarts the owned runtime as needed.
        chat = (OpenAILocalChat(route, endpoint[len("openai:"):]) if endpoint.startswith("openai:") else
                LocalChat(route, (self.local_runtime.endpoint or "http://127.0.0.1:1") if endpoint == "managed" else endpoint))
        self.local_routes[route] = chat
        self.local_origins[route] = endpoint
        local_turn = lambda request, configured=route: self._local_turn(request, configured)
        # 100.025 fix: tag the local lambda so _cancel_contract can tell it
        # from a fixture lambda (both are plain functions); untagged lambdas
        # still map to the fixture contract in deskhand_dispatch.
        local_turn._cancel_contract_kind = "local"
        self.dispatcher.adapters[route] = local_turn
        return route

    def _load_local_configuration(self):
        path = self.data / "local-models.json"
        if not path.exists():
            return
        try:
            if path.stat().st_size > 32768:
                raise ValueError("local model settings are too large")
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or set(document) != {"version", "models"} or document["version"] != 1 or not isinstance(document["models"], list) or len(document["models"]) > 32:
                raise ValueError("invalid local model settings")
            for item in document["models"]:
                if not isinstance(item, dict) or set(item) != {"model", "endpoint"}:
                    raise ValueError("invalid local model settings")
                self._register_local(item["model"], item["endpoint"])
        except (OSError, ValueError, TypeError) as error:
            self.local_routes.clear()
            self.local_origins.clear()
            self.dispatcher.adapters = {route: adapter for route, adapter in self.dispatcher.adapters.items() if route.assistant != "local"}
            self.local_config_error = "Local model settings could not be loaded: " + str(error)

    def _local_turn(self, request, route):
        chat = self.local_routes[route]
        if self.local_origins[route] == "managed":
            endpoint = self.local_runtime.start()
            if chat.endpoint != endpoint + "/api/chat":
                chat = self.local_routes[route] = LocalChat(route, endpoint)
        if request.agent_task_id is None:
            return chat(request)
        if self.host_rpc is None:
            raise RuntimeError("Windows action authority unavailable")
        transport = OpenAILocalToolTransport(chat) if isinstance(chat, OpenAILocalChat) else LocalToolTransport(chat)
        adapter = LocalToolAdapter(task_id=request.agent_task_id, turn_id=request.turn_id,
                                   prompt=request.text, transport=transport)
        return self._agent_turn(request, adapter)

    def _agent_turn(self, request, adapter):
        outcome = "failed"
        try:
            result = run_agent_session(task_id=request.agent_task_id, turn_id=request.turn_id,
                                       adapter=adapter, host=self.host_rpc, stopped=self.stopping.is_set)
            outcome = result.outcome
            if outcome == "cancelled":
                raise StopConfirmed(result.text)
            return Reply(request.turn_id, request.route, result.text)
        except StopConfirmed:
            outcome = "cancelled"
            raise
        except TimeoutError:
            # A timed-out exchange or host action may still have completed.
            # Preserve uncertainty in the host journal and never replay it.
            outcome = "timeout_unknown"
            raise
        finally:
            # A failed acknowledgement is not grounds to replay an action.
            try:
                self.host_rpc.call("task.end", {"task_id": request.agent_task_id, "turn_id": request.turn_id, "outcome": outcome})
            except (RuntimeError, TimeoutError):
                pass

    def _bridge_health(self, timeout=2):
        """True only when the bridge answers /health on loopback."""
        try:
            port = 8899
            if self.bridge_config.exists():
                cfg = json.loads(self.bridge_config.read_text(encoding="utf-8"))
                port = int(cfg.get("port", cfg.get("http_port", 8899)))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
                return json.loads(response.read().decode()).get("ok") is True
        except Exception:
            return False

    def _bridge_status(self, timeout=2):
        proc = self.bridge_proc
        running = proc is not None and proc.poll() is None
        if not running:
            self.bridge_proc = None
        return {"running": running, "pid": proc.pid if running else None,
                "health": self._bridge_health(timeout=timeout) if running else False}

    def _start_bridge(self):
        """Idempotent start: already running returns its status."""
        status = self._bridge_status()
        if status["running"]:
            return status
        if not self.bridge_script.exists():
            return {"running": False, "pid": None, "health": False,
                    "error": "Bridge module not installed yet (deskhand_bridge.py missing)"}
        if not self.bridge_config.exists():
            return {"running": False, "pid": None, "health": False,
                    "error": "Bridge not configured: create deskhand_bridge_config.json with a token first"}
        if self.bridge_log is not None:
            try:
                self.bridge_log.close()
            except Exception:
                pass
            self.bridge_log = None
        _rotate_bridge_log_if_needed(self.data / "deskhand_bridge.log")
        self.bridge_log = open(self.data / "deskhand_bridge.log", "ab")
        proc = subprocess.Popen([self.bridge_python, "-u", str(self.bridge_script),
                                 "--config", str(self.bridge_config)],
                                cwd=str(self.bridge_script.parent),
                                stdout=self.bridge_log, stderr=self.bridge_log)
        self.bridge_proc = proc
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.bridge_proc = None
                return {"running": False, "pid": None, "health": False,
                        "error": "Bridge exited during startup; see deskhand_bridge.log"}
            if self._bridge_health():
                return {"running": True, "pid": proc.pid, "health": True}
            time.sleep(0.5)
        return {"running": True, "pid": proc.pid, "health": False,
                "error": "Bridge started but /health did not answer within 10s"}

    def _stop_bridge(self):
        proc = self.bridge_proc
        self.bridge_proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if self.bridge_log is not None:
            try:
                self.bridge_log.close()
            except Exception:
                pass
            self.bridge_log = None
        _rotate_bridge_log_if_needed(self.data / "deskhand_bridge.log")
        return {"running": False, "pid": None, "health": False}

    def _work(self):
        while not self.stopping.is_set():
            # 100.041: wake poll 0.5 s -> 0.1 s. The wait
            # timeout is only the poll fallback for a missed wake
            # (submit sets the event, so the wait normally returns at
            # once); 0.1 s bounds a missed wake to 0.1 s instead of
            # 0.5 s before the next drain. The drain itself is
            # unchanged.
            self.wake.wait(.1)
            self.wake.clear()
            while not self.stopping.is_set():
                try:
                    row = self.dispatcher.dispatch_one()
                except Exception:
                    # Dispatcher has recorded uncertain delivery. Keep servicing
                    # reads and subsequent messages, never replay this one.
                    continue
                if row is None:
                    break

    def _running_turn_is_tunnel_served(self):
        """(turn_id, True) when the turn currently dispatching is
        served by the tunnel provider, else (None, False) -- round 4,
        2026-11-12.

        The stuck-turn watchdog (120 s) was built for the dead-relay
        case, where a turn would otherwise hang the relay's full
        600 s bound. Tunnel turns legitimately run long -- worker
        spin-up plus long host actions: a real "open notepad" turn
        took 187 s claim-to-post, and a "hi" turn 187 s behind
        duplicate workers -- and the tunnel already bounds the wait
        at reply_timeout_secs (600 s, the relay's outer bound). The
        watchdog interrupting a tunnel turn at 120 s abandons the
        wait (StopConfirmed -> turn recorded uncertain), so a reply
        posted afterwards is never fetched or acked and the phone
        shows "Delivery unconfirmed" forever. Only a positive
        identification skips the interrupt; anything unknown keeps
        the old watchdog behaviour, and relay-served turns are
        still interrupted at 120 s."""
        try:
            if deskhand_tunnel is None or self.muse_route is None:
                return None, False
            gate = getattr(self.dispatcher, "admission_gate", None)
            if gate is None:
                return None, False
            with gate:
                running = getattr(self.dispatcher, "running", None)
            if not running:
                return None, False
            turn_id, adapter = running
            muse_adapter = self.dispatcher.adapters.get(self.muse_route)
            if adapter is not muse_adapter:
                return None, False
            provider = getattr(adapter, "provider", None)
            if isinstance(provider, deskhand_tunnel.TunnelProvider):
                return turn_id, True
            return None, False
        except Exception:
            return None, False

    def _watch_stuck_turns(self):
        """Interrupt plain chat turns stuck dispatching past the timeout.

        The worker thread is blocked inside dispatch_one while a turn waits
        on a Muse reply, so only a separate thread can notice it is stuck.
        The interrupt only asks the adapter to stop; the dispatch thread
        still owns the journal entry (cancelled if nothing started, unknown
        if delivery may have begun), so nothing is replayed and the outcome
        is recorded exactly once. Agent turns are skipped here: host actions
        may legitimately run long, and the relay's outer bound still applies.
        Tunnel-served turns are skipped too (round 4): the tunnel's own
        reply timeout bounds them, and a slow tunnel reply is legitimate.
        """
        while not self.stopping.is_set():
            if self.stopping.wait(self.watchdog_interval):
                break
            # Round 4: a tunnel-served turn is never interrupted
            # here -- the tunnel's own reply_timeout_secs (600 s)
            # bounds it, and a slow tunnel reply is legitimate.
            # Anything else keeps the 120 s watchdog exactly as before.
            tunnel_turn_id, tunnel_served = self._running_turn_is_tunnel_served()
            if tunnel_served:
                if tunnel_turn_id != self._tunnel_watchdog_skip_logged_for:
                    self._tunnel_watchdog_skip_logged_for = tunnel_turn_id
                    logger.info(
                        "Stuck-turn watchdog skipping tunnel-served turn %s; "
                        "the tunnel reply timeout still bounds the wait",
                        tunnel_turn_id)
            else:
                self._tunnel_watchdog_skip_logged_for = None
                try:
                    outcome = self.dispatcher.interrupt_stale(self.stale_chat_timeout)
                except Exception as exc:
                    logger.warning("Stuck-turn watchdog check failed: %s", exc)
                    continue
                if outcome.get("interrupt_requested"):
                    logger.warning(
                        "Interrupted stale chat turn %s after %.1fs without a reply; "
                        "delivery stays uncertain and is never replayed",
                        outcome.get("turn_id"), outcome.get("age_secs", 0))
                elif outcome.get("stale") and outcome.get("error"):
                    logger.warning(
                        "Stale chat turn %s (%.1fs) cannot be interrupted: %s",
                        outcome.get("turn_id"), outcome.get("age_secs", 0), outcome.get("error"))
            # Fail-fast in-flight trip (100.020, D-020-8): same 15 s
            # cadence, label-only. A failure here never breaks the
            # stuck-turn watchdog above.
            try:
                self._failfast_trip_check()
            except Exception as exc:
                logger.warning("Fail-fast trip check failed: %s", exc)
            # 100.029: an expired debug session reverts the service
            # log sink within one watchdog tick of its 1-hour cap.
            _apply_debug_levels()
            # 100.029 (E17 §3.4): the weekly, idle-only Jett-journal
            # prune is checked on this same 15 s cadence; the check
            # itself is a cheap state-file read and returns at once
            # unless a week has passed.
            try:
                self._maybe_prune_jett_journal()
            except Exception as exc:
                logger.warning("Jett journal prune check failed: %s", exc)

    def handle(self, command, submission_ticket=None):
        # 100.029: an expired debug session reverts the service log
        # sink on the next op, not only on the watchdog cadence.
        _apply_debug_levels()
        op = command.get("op")
        if op == "access_validate":
            if command.get('assistant') == 'muse' and command.get('account') == 'deskhand':
                if self.muse is None:
                    raise RuntimeError('Needs sign-in: Muse is unavailable')
                return self.muse.prepare_host_tools()
            if command.get('assistant') == 'codex':
                if not any(route['assistant'] == 'codex' and route['account'] == command.get('account') for route in self.routes):
                    raise RuntimeError('Needs sign-in: this Codex account is unavailable')
                return {'ready': True}
            if command.get("assistant") != "local" or command.get("account") != "" or not self.local_routes:
                raise RuntimeError("No supported local AI is configured.")
            if not self.fixture:
                from urllib.request import Request
                from urllib.parse import urlsplit
                for route, chat in self.local_routes.items():
                    if self.local_origins[route] == "managed":
                        self.local_runtime.require_model(route.model)
                        continue
                    origin = urlsplit(chat.endpoint)
                    path = "/v1/models" if isinstance(chat, OpenAILocalChat) else "/api/tags"
                    with chat.client.open(Request(f"{origin.scheme}://{origin.netloc}{path}"), timeout=5) as response:
                        if response.status != 200:
                            raise RuntimeError("The configured local AI server is unavailable.")
                        raw = response.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise RuntimeError("Local model inventory is too large.")
                    inventory = json.loads(raw)
                    models = inventory.get("data" if isinstance(chat, OpenAILocalChat) else "models", [])
                    names = {item.get("id", item.get("name", item.get("model"))) for item in models if isinstance(item, dict)}
                    if route.model not in names and route.model + ":latest" not in names:
                        raise RuntimeError("The configured local model is no longer available.")
            return {"ready": True}
        if op in {'muse_fixture_begin_wait', 'muse_fixture_wait_status', 'muse_fixture_disconnect', 'muse_fixture_begin_executor', 'muse_fixture_executor_status'}:
            if not self.fixture or os.environ.get('DESKHAND_MUSE_HOST_FIXTURE') != '1' or self.muse is None:
                raise ValueError('Unknown chat operation')
            if op == 'muse_fixture_disconnect':
                self.muse.bridge.process.kill()
                self.muse.bridge.process.wait(timeout=5)
                return {'status': 'disconnected'}
            return self.muse.bridge._request(op.removeprefix('muse_'))
        if op in {'muse_status', 'muse_signin', 'muse_signin_done', 'muse_resume_local'}:
            if self.muse is None:
                raise RuntimeError('Muse integration is not enabled')
            if op == 'muse_status':
                return self.muse.status()
            if op == 'muse_signin':
                self.muse.sign_in()
                return {'status': 'Complete sign-in in the Muse window.'}
            if op == 'muse_resume_local':
                return self.muse.resume_local()
            self.muse.finish_sign_in()
            # 100.013 Item 5: auto-resume after sign-in. The service is
            # the sole auto-resume owner; no UI/panel path resumes.
            # Precondition on browser.cloud_ready, not on
            # finish_sign_in() returning: finish_sign_in only queues a
            # command to the headless browser, it does not confirm
            # sign-in. Failures are best-effort: the background
            # auto-release loop retries.
            browser = getattr(self.muse, "browser", None)
            if browser is not None and getattr(browser, "cloud_ready", False):
                try:
                    self.muse.resume_local()
                except Exception:
                    pass
                try:
                    self.muse.ensure_chat_ready()
                except Exception:
                    pass
                # Signing in can restore Muse connectivity, but must not
                # override an explicit user stop of chat admissions.
            return {'status': 'Checking Muse sign-in.'}
        if op == "worker_reconnect":
            # 100.037 (E12 §10.5): manual Reconnect — one reconcile()
            # immediately, outside the backoff schedule, single-flight
            # with any in-flight attempt. A service-side op over the
            # existing stdin/stdout channel; NOT a HostEngine op (no
            # allowlist change). Never touches a grant, an approval, a
            # tunnel, a process, or STOP.
            return self._worker_reconnect_manual()
        if op == 'muse_action':
            if self.muse is None:
                raise RuntimeError('Muse integration is not enabled')
            action, payload = command.get('action'), command.get('payload', {})
            if action not in {'configure', 'start', 'stop', 'session', 'revoke_day_authorization',
                              'connection_details', 'reconnect', 'ping_muse', 'export_audit'} or not isinstance(payload, dict):
                raise ValueError('Unsupported local Muse setting')
            if 'action' in payload:
                raise ValueError('Invalid local Muse setting')
            result = self.muse.bridge.local_action(action, payload)
            if action == 'start':
                self.muse_error = None
            return result
        if op == "local_runtime_start":
            model = command.get("model")
            if model is None:
                self.local_runtime.start()
            else:
                self.local_runtime.require_model(model)
            return {"status": "Local runtime is ready."}
        if op == "local_configure":
            model = command.get("model")
            endpoint = command.get("endpoint", "")
            # Validate everything before touching the saved configuration.
            if not isinstance(model, str) or not model.strip() or len(model) > 128 or model != model.strip() or not isinstance(endpoint, str):
                raise ValueError("An explicit local model and server address are required")
            if endpoint != "managed":
                if endpoint.startswith("openai:"):
                    OpenAILocalChat(Route("local", model, "", ""), endpoint[len("openai:"):])
                else:
                    LocalChat(Route("local", model, "", ""), endpoint)
            configs = [{"model": route.model, "endpoint": self.local_origins[route]} for route in self.local_routes if route.model != model]
            if len(configs) >= 32:
                raise ValueError("Too many local models")
            configs.append({"model": model, "endpoint": endpoint})
            path = self.data / "local-models.json"
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps({"version": 1, "models": configs}), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            self._register_local(model, endpoint)
            self.local_config_error = None
            self.routes = []
            return {"configured": True, "model": model}
        if op == "catalog":
            if not self.routes or command.get("refresh"):
                self.routes = []
                try:
                    if self.fixture:
                        self.routes += [{"assistant": "codex", "model": "fixture-model", "account": "default", "effort": "high", "efforts": ["high"], "name": "Fixture Codex"}]
                    else:
                        self.routes += catalog(os.environ.get("DESKHAND_CODEX_HOME"), refresh=bool(command.get("refresh")))
                    self.catalog_error = None
                except Exception as error:
                    self.catalog_error = str(error)
                if self.muse_route is not None:
                    for route in self.routes:
                        route['default'] = False
                    self.routes.append({**vars(self.muse_route), 'name': 'Muse', 'accountLabel': 'Muse browser account', 'efforts': [], 'default': True})
                self.routes += [{**vars(route), "name": "Local · " + route.model, "efforts": []} for route in self.local_routes]
            return {"routes": self.routes, "error": self.muse_error or self.local_config_error or self.catalog_error}
        if op == "route_suggest":
            # Batch 2, v100.004: {"text": ...} -> {kind, reason, route, always_smart}
            return self.route_suggest(command.get("text", ""))
        if op == "router_settings":
            # Batch 2: get returns the settings; set takes {"always_smart": bool}.
            if command.get("always_smart") is None:
                return self._load_router_settings()
            return self._save_router_settings({"always_smart": command.get("always_smart")})
        if op == "warm_start":
            # Batch 2: warm local models now (best-effort, never raises).
            return self.warm_start()
        if op in ("runtime_stream", "events"):
            # Batch 2: read-only event feed; {"since": seq|null, "limit": N}.
            return self.runtime_stream(since=command.get("since"), limit=command.get("limit", 100))
        if op == "timing":
            # Batch 2: per-hop timing for one turn, {"id": turn_id}.
            return {"turn_id": command.get("id"), "timing": self.turn_timing(command.get("id") or "")}
        if op == "submit":
            # 100.039: stamp PC receipt time (epoch millis) and carry
            # the phone's send time through (fail-soft — a bad sent_at
            # stores NULL, never raises the submit).
            received_at = int(time.time() * 1000)
            sent_at = _coerce_sent_at(command.get("sent_at"), received_at)
            ticket = self.dispatcher.admission_ticket() if submission_ticket is None else submission_ticket
            if command.get("auto_route") and not command.get("assistant"):
                # Batch 2: router picks the route; the exact-catalog
                # check below still applies, so a suggestion can never
                # smuggle in a route that is not in the catalog.
                suggestion = self.route_suggest(command.get("text", ""))
                picked = suggestion.get("route")
                if picked is None:
                    raise ValueError("No route available for this message")
                for key in ("assistant", "model", "account", "effort"):
                    command[key] = picked.get(key, "")
            route = Route(*(command.get(k, "") for k in ("assistant", "model", "account", "effort")))
            # Exact catalog choices; effort is selected separately by each bar.
            if not any(all(r[k] == getattr(route, k) for k in ("assistant", "model", "account")) and
                       (route.effort in r.get("efforts", []) or route.effort == r.get("effort", "")) for r in self.routes):
                raise ValueError("Refresh models and select an available route")
            if route not in self.dispatcher.adapters or (self.fixture and route.assistant not in {"local", "muse"}):
                adapter = (lambda request: Reply(request.turn_id, request.route, "Fixture reply: " + request.text)) if self.fixture else CodexChat(route, self.data / "codex", os.environ.get("DESKHAND_CODEX_HOME"))
                # Registration is atomic under Python's dict operations. Avoid
                # waiting on the dispatcher gate while an adapter is replying.
                self.dispatcher.adapters[route] = HostToolChat(adapter, self._agent_turn, self.data / 'agent-exchanges.sqlite') if route.assistant == 'codex' else adapter
            agent_task_id = command.get("agent_task_id")
            if agent_task_id is not None and (route.assistant not in {"local", "muse", "codex"} or self.host_rpc is None):
                raise ValueError("Agent mode unavailable for this route")
            if agent_task_id is not None:
                # Provider readiness is a task prerequisite, not a condition
                # for restoring the PC's independent local authority.
                readiness = self.handle({"op": "access_validate", "assistant": route.assistant, "account": route.account})
                if readiness.get("ready") is not True:
                    raise RuntimeError("The selected AI connection is not ready.")
            # Fail-fast (100.020, D-020-11): the single gate hook. The
            # route and agent_task_id are known here; a refusal raises
            # before dispatcher.submit(), so the turn never enters the
            # mailbox and never wakes the worker. The text stays a
            # draft on the phone (it is never cleared without a submit
            # acknowledgement) and the refusal is journaled.
            refusal_box = {}
            def before_admit():
                if route.assistant == "muse":
                    self._sync_muse_provider()
                refusal = self._failfast_check(command, route, agent_task_id)
                if refusal is not None:
                    refusal_box["value"] = refusal
                    raise RuntimeError(refusal["message"])
            try:
                row = self.dispatcher.submit(
                    command["id"], command["text"], **vars(route),
                    agent_task_id=agent_task_id, admission_ticket=ticket,
                    sent_at=sent_at, received_at=received_at,
                    before_admit=before_admit)
            except RuntimeError:
                refusal = refusal_box.get("value")
                if refusal is not None:
                    self._failfast_journal(refusal, command, route)
                    raise RuntimeError(refusal["message"])
                raise
            self.wake.set()
            return row
        if op == "history":
            return {"turns": self.box.recent(), "chatAdmission": self.dispatcher.admission_state()}
        if op == "activity":
            return self.activity()
        if op == "health":
            return self.health()
        if op == "tunnel_status":
            # B42 (100.050 tunnel): read-only tunnel state.
            # Deliberately NOT in the threaded-ops set below:
            # it is one config read + one 2 s /health probe.
            return self._tunnel_status()
        if op == "debug_session":
            # 100.029 (E41 §4): debug-session control. The op is the
            # mechanism; the chat request that invoked it is the
            # authorization, and only Phil may enable a session.
            return self.debug_session(command)
        if op == "diagnostics_export":
            return self.diagnostics_export(command)
        if op == 'stop_agent_work':
            # 100.025 S4/S13: wait-and-report (same 5 s wait_for_idle
            # pattern as stop_chat). The turn outcome is never
            # force-marked here; it is recorded once, by the
            # dispatching thread. Additive keys only.
            result = self.dispatcher.request_agent_stop()
            drained = self.dispatcher.wait_for_idle(5)
            running_turn_id = result.get("running_turn_id")
            active_row = self.box.get(running_turn_id) if running_turn_id is not None else None
            active_status = active_row.get("status") if active_row is not None else None
            still_running = active_status == "dispatching" if active_row is not None else (running_turn_id is not None and not drained)
            queued = result.get("queued_cancelled") or []
            contract = result.get("cancel_contract")
            if running_turn_id is None:
                status = ("Agent work stopped: nothing running; %d queued agent turn(s) cancelled." % len(queued))
            elif active_row is not None and active_row.get("agent_task_id") is None:
                status = ("No agent turn running; the running chat turn was left alone; %d queued agent turn(s) cancelled." % len(queued))
            elif isinstance(contract, str) and contract.startswith("uncancellable"):
                status = ("STOP will prevent the next action; the running one finishes (%d queued agent turn(s) cancelled)." % len(queued))
            elif isinstance(contract, str) and contract.startswith("partial"):
                status = ("Interrupt requested for the running turn: %s; %d queued agent turn(s) cancelled." % (contract, len(queued)))
                if still_running:
                    status += " Still running after the 5 s wait."
            else:
                status = ("Agent stop requested; %d queued agent turn(s) cancelled." % len(queued))
            return {**result, "drained": drained, "still_running": still_running, "status": status}
        if op == 'stop_muse':
            if self.muse is None:
                return {'muse_stop': 'not_enabled'}
            # STOP latch removed (Phil 2026-11-09): there is no Muse
            # stop to perform or confirm. Never call muse.stop_all()
            # and never report 'confirmed' for a stop that did not
            # happen. The host clears its STOP banner on muse_stop
            # 'not_enabled' (see HostEngine).
            return {'muse_stop': 'not_enabled', 'stop_removed': True}
        if op == "stop_chat":
            # STOP latch removed (Phil 2026-11-09): there is no Muse
            # stop to run alongside the chat stop, so no stop thread
            # is started and muse.stop_all() is never called. The
            # dispatcher chat stop below is what interrupts an active
            # Muse turn. musebridge_stop reports 'not_enabled' with
            # stop_removed True whenever Muse is attached.
            result = self.dispatcher.request_chat_stop()
            drained = self.dispatcher.wait_for_idle(5)
            active_turn = result.get("active_turn")
            active_row = self.box.get(active_turn) if active_turn is not None else None
            active_status = active_row.get("status") if active_row is not None else None
            # 'unknown' stays NON-terminal for the stop confirmation on
            # purpose (see test_chat_stop_keeps_active_uncertain_turn_unconfirmed):
            # the turn is no longer dispatching and the chat is unblocked,
            # but delivery may still have happened, so the stop cannot be
            # reported as certain. active_status is returned so the caller
            # (PC panel, phone StopStatus) can tell "stopped but delivery
            # unconfirmed" apart from "still stuck dispatching".
            active_terminal = active_turn is None or (
                active_row is not None and active_row.get("status") in {"cancelled", "completed", "failed"}
            )
            deskhand_stop = "confirmed" if result.get("error") is None and drained and active_terminal else "unconfirmed"
            musebridge_stop = 'not_enabled'
            stop_removed = self.muse is not None
            # 100.025 S6: the STOP latch was removed, so 'not_enabled'
            # is the stopped state for a side with no stop to perform.
            all_ai_stopped = deskhand_stop in {"confirmed", "not_enabled"} and musebridge_stop in {"confirmed", "not_enabled"}
            status = "DeskHand chat and MuseBridge STOP confirmed." if all_ai_stopped else (
                f"STOP not fully confirmed: DeskHand chat {deskhand_stop}; MuseBridge {musebridge_stop}."
            )
            return {**result, "active_status": active_status, "deskhand_stop": deskhand_stop, "musebridge_stop": musebridge_stop,
                    "muse_stop": musebridge_stop, "stop_removed": stop_removed, "all_ai_stopped": all_ai_stopped, "status": status}
        if op == "restart_chat":
            # 100.013 Item 1b: kill the AI (chat/agent) session, delete
            # queued messages, restart fresh -- one atomic server op so
            # the phone's history auto-resume cannot admit new turns
            # between the steps. This is the Deskhand chat/agent
            # session only: muse.stop_all() is never called and the
            # Muse browser session is untouched (stop_muse stays
            # not_enabled).
            result = self.dispatcher.request_chat_stop()
            agent_stop = self.dispatcher.request_agent_stop()
            # O4: delete 'queued' rows always; delete 'cancelled' rows
            # only when cancelled by this same restart (the id list
            # request_chat_stop just returned). Never delete a row whose
            # outcome/status is 'unknown'.
            deleted = self.box.delete_queued_and_cancelled(result.get("queued_cancelled") or [])
            drained = self.dispatcher.wait_for_idle(5)
            active_turn = result.get("active_turn")
            active_row = self.box.get(active_turn) if active_turn is not None else None
            active_status = active_row.get("status") if active_row is not None else None
            # Same 'unknown' handling as stop_chat above: 'unknown' is
            # non-terminal, so a restart past a still-active turn is
            # reported unconfirmed, never forced.
            active_terminal = active_turn is None or (
                active_row is not None and active_row.get("status") in {"cancelled", "completed", "failed"}
            )
            restarted = False
            restart_error = None
            if drained and active_terminal:
                try:
                    self.dispatcher.resume_chat()
                    restarted = True
                except Exception as error:
                    restart_error = str(error)[:500]
            elif not drained:
                restart_error = "Assistant work is still active; cannot resume"
            else:
                restart_error = "Active turn outcome is unknown; restart unconfirmed"
            return {**result, "agent_interrupt_requested": agent_stop.get("interrupt_requested"),
                    "deleted": deleted, "restarted": restarted, "restart_error": restart_error,
                    "active_status": active_status, "admission_epoch": self.dispatcher.admission_epoch}
        if op == "resume_chat_local":
            if self.dispatcher.admission_state()['stopped']:
                self.dispatcher.resume_chat()
            status = 'Chat is ready.'
            if self.muse is not None:
                try:
                    state = self.muse.bridge.state()
                    if not state.get('enabled') or not state.get('connected'):
                        status = 'Chat is ready; Muse is waiting for its bridge connection.'
                except Exception:
                    status = 'Chat is ready; MuseBridge status is unavailable.'
            admission = self.dispatcher.admission_state()
            return {'status': status, 'admission_epoch': admission['epoch'], 'stopped': admission['stopped']}
        if op == "get":
            return self.box.get(command["id"])
        if op == "bridge_start":
            return self._start_bridge()
        if op == "bridge_status":
            return self._bridge_status()
        if op == "bridge_stop":
            return self._stop_bridge()
        if op == "task_agent_start":
            if self.task_agent_lifecycle is None:
                return {"ok": False, "error": "task agent lifecycle unavailable"}
            return self.task_agent_lifecycle.ensure_started()
        if op == "task_agent_status":
            if self.task_agent_lifecycle is None:
                return {"ok": False, "error": "task agent lifecycle unavailable"}
            return self.task_agent_lifecycle.status()
        if op == "task_agent_stop":
            if self.task_agent_lifecycle is None:
                return {"ok": False, "error": "task agent lifecycle unavailable"}
            return self.task_agent_lifecycle.ensure_stopped()
        if op in {"run_start", "run_status", "run_cancel"}:
            # 100.045 (B33): long-running command runner. Lazy import
            # inside the branch (same pattern as task_agent_action /
            # deskhand_smarts) so a broken deskhand_runner module cannot
            # break service startup. get_runner() returns the service's
            # ONE Runner, created on first use and cached on self._runner,
            # wired to the vm-event feed and the data dir.
            from deskhand_runner import get_runner
            if op == "run_start":
                # STOP gate (pinned): mirror resume_chat_local's read of
                # the dispatcher state -- a start refuses while chat/agent
                # work is stopped. Status and cancel do NOT refuse:
                # observing or stopping a run must always work.
                if self.dispatcher.admission_state()["stopped"]:
                    raise RuntimeError("Runner stopped: chat/agent work is stopped")
            return get_runner(self).dispatch(op, command)

        if op in {"search_text", "search_files", "search_symbol"}:
            # 100.046 (B34): real indexed code search. Lazy import
            # inside the branch (same pattern as run_*) so a broken
            # deskhand_search module cannot break service startup.
            # get_searcher() returns the service's ONE Searcher,
            # created on first use and cached on self._searcher.
            from deskhand_search import get_searcher
            return get_searcher(self).dispatch(op, command)

        if op in {"transfer_put", "transfer_init", "transfer_chunk",
                  "transfer_finalize", "transfer_status", "transfer_cancel",
                  "transfer_verify", "transfer_read"}:
            # 100.047 (B35): byte-exact file transfer. Lazy import inside
            # the branch (same pattern as run_* / search_*) so a broken
            # deskhand_transfer module cannot break service startup.
            # get_transfer() returns the service's ONE Transfer, created on
            # first use and cached on self._transfer, wired to the vm-event
            # feed and the data dir, with the STOP gate wired to the
            # dispatcher state (the gate lives in the module, §1.6).
            from deskhand_transfer import get_transfer
            return get_transfer(self).dispatch(op, command)

        if op == "verify_build":
            # 100.048 (B36): built-in verify-this-build step.
            # Lazy import inside the branch (same pattern as
            # run_* / search_* / transfer_*) so a broken
            # deskhand_verify module cannot break service startup.
            # get_verifier() returns the service's ONE Verifier,
            # created on first use and cached on self._verifier.
            # No handle()-level STOP gate -- the gate lives in the
            # module (section 1.6, the transfer precedent).
            from deskhand_verify import get_verifier
            return get_verifier(self).dispatch(op, command)

        if op == "task_agent_action":
            # Laptop-agent fallback (Option A hook). Separate from
            # muse_action: it never routes through the bridge
            # local_action allowlist, which is untouched. Gates, journal,
            # and dedup live in deskhand_task_agent_op; the on-PC runner
            # (run_task_agent_fallback.py) calls the same function, so
            # the in-process op and the runner cannot drift apart.
            from deskhand_task_agent_op import run_task_agent_action
            return run_task_agent_action(self.data, command,
                                         **self._task_agent_hooks)
        if op in {"smarts_status", "grounding_fuse", "index_search", "checkpoint_save", "checkpoint_resume", "memory_list", "skill_list"}:
            from deskhand_smarts import handle_smarts_op

            return handle_smarts_op(self, command)
        raise ValueError("Unknown chat operation")

    # --- Batch 2, v100.004: router settings, warm start, timing, runtime stream ---

    def _router_settings_path(self):
        return self.data / "router.json"

    def _load_router_settings(self):
        """{"always_smart": bool}. Missing/corrupt file -> defaults."""
        try:
            settings = json.loads(self._router_settings_path().read_text(encoding="utf-8"))
            if isinstance(settings, dict):
                return {"always_smart": bool(settings.get("always_smart", False))}
        except Exception:
            pass
        return {"always_smart": False}

    def _save_router_settings(self, settings):
        if not isinstance(settings, dict) or not isinstance(settings.get("always_smart"), bool):
            raise ValueError("router settings require a boolean always_smart")
        path = self._router_settings_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"always_smart": settings["always_smart"]}, ensure_ascii=True), encoding="utf-8")
        tmp.replace(path)
        return {"always_smart": settings["always_smart"]}

    def route_suggest(self, text):
        """Suggest a route for a chat text (Batch 2, router).

        Pure suggestion over the current catalog; never changes grants
        or authority. The UI override lives in router.json.
        """
        settings = self._load_router_settings()
        decision = suggest_route(text, list(self.routes), always_smart=settings["always_smart"])
        return {"kind": decision["kind"], "reason": decision["reason"],
                "route": decision["route"], "always_smart": settings["always_smart"]}

    def warm_start(self):
        """Warm the local models so the first real turn is not a cold
        load (Batch 2). Best-effort per route: a failed warm-up only
        means that route's first turn pays the load (old behaviour).
        Never raises.
        """
        results = {}
        for route, chat in list(self.local_routes.items()):
            warmup = getattr(chat, "warmup", None)
            if not callable(warmup):
                results[route.model] = {"warmed": False, "reason": "no warmup support"}
                continue
            try:
                results[route.model] = {"warmed": bool(warmup())}
            except Exception as error:
                results[route.model] = {"warmed": False, "reason": str(error)[:200]}
        return {"results": results}

    def turn_timing(self, turn_id):
        """Per-hop timing for one turn (Batch 2, measurement).

        Merges the dispatcher's in-memory record with the timing JSON
        stored beside the turn row. Returns {} when unknown.
        """
        timing = dict(self.dispatcher.timings.get(turn_id, {}))
        try:
            row = self.box.get(turn_id)
        except Exception:
            row = None
        if row:
            stored = row.get("timing_json")
            if stored:
                try:
                    stored = json.loads(stored)
                except ValueError:
                    stored = None
            if isinstance(stored, dict):
                merged = dict(stored)
                merged.update(timing)
                timing = merged
        return timing

    def runtime_stream(self, since=None, limit=100):
        """Read-only runtime event feed (Batch 2, runtime-stream design).

        Merges, newest-last: chat turns (turn box), bridge file/command
        activity, and vm_events.jsonl when the VM has written one into
        the data dir. Events carry a process-stable sequence: the
        position in the merged, time-ordered list. `since` filters to
        seq > since; when since is older than the window, the reply
        sets resynced=True and starts at the window's first event.
        Never raises, never includes token/file contents/screenshot
        data — summaries are truncated text and paths only.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 100
        limit = min(max(limit, 1), 200)
        events = []
        sources = {"chat": "missing", "bridge": "missing", "vm_events": "missing", "worker": "missing"}
        skipped = 0
        skipped_debug = 0
        try:
            turns = self.box.recent(limit=limit)
            sources["chat"] = "ok"
            for turn in turns:
                ts = turn.get("updated") or turn.get("created") or time.time()
                event = {"ts": float(ts), "source": "chat",
                         "type": "turn_" + str(turn.get("status") or "unknown"),
                         "actor": turn.get("assistant") or "",
                         "summary": (turn.get("text") or "")[:120],
                         "detail": {"turn_id": turn.get("id"), "status": turn.get("status"),
                                    "assistant": turn.get("assistant"),
                                    "sent_at": turn.get("sent_at"),
                                    "received_at": turn.get("received_at")}}
                leveled = _level_event(event)
                if leveled is not None:
                    events.append(leveled)
        except Exception:
            pass
        if self.muse is not None:
            try:
                files = self.muse.recent_file_activity(limit=limit)
                sources["bridge"] = "ok"
                for item in files:
                    event = {"ts": float(item.get("ts") or time.time()), "source": "bridge",
                             "type": "file_" + str(item.get("operation") or "unknown"),
                             "actor": "muse",
                             "summary": "%s %s" % (item.get("operation") or "?", item.get("path") or ""),
                             "detail": {"operation": item.get("operation"), "path": item.get("path"),
                                        "ok": item.get("ok")}}
                    leveled = _level_event(event)
                    if leveled is not None:
                        events.append(leveled)
            except Exception:
                pass
        vm_path = self.data / "vm_events.jsonl"
        if vm_path.exists():
            sources["vm_events"] = "ok"
            try:
                lines = vm_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        skipped += 1
                        continue
                    if not isinstance(item, dict):
                        skipped += 1
                        continue
                    # DEBUG is NEVER written to the event stream
                    # (E41 §3.2): a DEBUG line that reached the file
                    # is dropped here, never served, and counted.
                    if item.get("level") == "DEBUG":
                        skipped_debug += 1
                        continue
                    event = {"ts": float(item.get("ts") or time.time()),
                             "source": str(item.get("source") or "vm"),
                             "type": str(item.get("type") or "vm_event"),
                             "actor": str(item.get("actor") or ""),
                             "summary": _truncate_text(
                                 str(item.get("summary") or ""), 200),
                             "detail": item.get("detail") if isinstance(item.get("detail"), dict) else {}}
                    # A level/code/turn_id the writer already recorded
                    # is preserved; _level_event derives what is
                    # missing (grant codes carry their §3.2 levels).
                    if isinstance(item.get("level"), str):
                        event["level"] = item["level"]
                    if isinstance(item.get("code"), str):
                        event["code"] = item["code"]
                    if isinstance(item.get("turn_id"), str):
                        event["turn_id"] = item["turn_id"]
                    # The Generation is always shown with a grant
                    # event (§2.2): top-level when the writer put it
                    # there, else from the detail dict.
                    if isinstance(item.get("generation"), int):
                        event["generation"] = item["generation"]
                    elif isinstance(event["detail"].get("generation"), int):
                        event["generation"] = event["detail"]["generation"]
                    leveled = _level_event(event)
                    if leveled is not None:
                        events.append(leveled)
            except Exception:
                sources["vm_events"] = "error"
        # 100.037 (E12 §10.4): the "worker" source is the Muse-worker
        # state (ok / disconnected / missing) — NOT the relay-based
        # diagnostics "worker" row. Read from the watchdog's cached
        # state; this op never probes the worker itself.
        try:
            _watchdog = getattr(self, "worker_watchdog", None)
            _muse = getattr(self, "muse", None)
            if _watchdog is not None and _muse is not None \
                    and callable(getattr(_muse, "status", None)):
                sources["worker"] = ("disconnected"
                                     if _watchdog.state()["fired"] else "ok")
        except Exception:
            pass
        events.sort(key=lambda e: (e["ts"], e["source"], e["type"]))
        events = events[-limit:]
        for seq, event in enumerate(events, start=1):
            event["seq"] = seq
        if since is None:
            selected = events
            resynced = False
        else:
            try:
                since = int(since)
            except (TypeError, ValueError):
                since = 0
            selected = [e for e in events if e["seq"] > since]
            resynced = bool(events) and since < events[0]["seq"]
        return {"events": selected,
                "latest_seq": events[-1]["seq"] if events else 0,
                "sources": sources,
                "counts": {"skipped_bad_lines": skipped,
                           "skipped_debug": skipped_debug,
                           "returned": len(selected)},
                "resynced": resynced}

    def _agent_health(self, timeout=None):
        """Laptop-agent probe (100.016, spec 4.3).

        Same shape as _bridge_health(): loopback /health, 2s timeout.
        Also probes the Tailscale path and reports loopback vs
        Tailscale disagreement in the error text (the green-while-dead
        signature). Runs on the op call, no background timer.
        Returns {"running", "server", "requests_total", "error"}.
        """
        if deskhand_diagnostics is None:
            return {"running": False, "server": "", "requests_total": 0,
                    "error": "diagnostics module not loaded"}
        loopback = deskhand_diagnostics.AGENT_LOOPBACK_URL
        lifecycle = getattr(self, "task_agent_lifecycle", None)
        base_url = getattr(lifecycle, "base_url", None) if lifecycle is not None else None
        if base_url:
            loopback = str(base_url).rstrip("/") + "/health"
        return deskhand_diagnostics.agent_probe(loopback_url=loopback, timeout=(timeout if timeout is not None else deskhand_diagnostics.AGENT_TIMEOUT_SECS))

    def health(self):
        """Unified health snapshot (100.016, spec 2).

        Read-only and side-effect-free except for the probes
        themselves. The op never fails the whole snapshot for one bad
        row (deskhand_diagnostics.build_health_snapshot guards every
        collector); ok:false only when the op itself could not run.
        Service version is the first 8 of the SHA-256 of the loaded
        deskhand_service.py file, hashed AT OP TIME (spec 4.1).
        """
        if deskhand_diagnostics is None:
            return {"ok": False, "error": "diagnostics module not loaded"}
        store = getattr(self, "_health_last_ok", None)
        if not isinstance(store, dict):
            store = {}
            self._health_last_ok = store
        snapshot = deskhand_diagnostics.build_health_snapshot(
            host_rpc=getattr(self, "host_rpc", None),
            service_file_path=Path(__file__).resolve(),
            bridge_status_fn=self._bridge_status,
            agent_probe_fn=self._agent_health,
            lifecycle=getattr(self, "task_agent_lifecycle", None),
            data_dir=getattr(self, "data", None),
            lifecycle_module_path=Path(__file__).with_name(
                "deskhand_task_agent_lifecycle.py"),
            last_ok_store=store,
            levels=self._configured_levels())
        # 100.029 (E41 §6.3): every component row carries its
        # configured level. The snapshot builder adds it; this is
        # belt-and-braces for a diagnostics module built without it.
        for _key, _row in (snapshot.get("components") or {}).items():
            if isinstance(_row, dict) and "level" not in _row:
                _row["level"] = "INFO"
        # Fail-fast (100.020, D-020-1): memoise the last snapshot +
        # as_of so the submit gate can read it without re-probing.
        # health() itself is unchanged for every other caller.
        cache = {"snapshot": snapshot, "as_of": snapshot.get("as_of")}
        lock = getattr(self, "_health_cache_lock", None)
        if lock is None:
            self._health_cache = cache
        else:
            with lock:
                self._health_cache = cache
        return snapshot

    # --- 100.029 (E41): configured levels, debug sessions, grant feed ---

    def _configured_levels(self):
        """Each health component's configured log level (E41 §6.3).

        Default INFO everywhere (§4: the default level is INFO, on
        every sink). The one level this service can know is its own:
        the service row reads DEBUG while a debug session is live on
        the service sink, INFO otherwise. Every other source's writer
        level is not visible to this process, so it reports the
        default rather than inventing one.
        """
        try:
            keys = (deskhand_diagnostics.COMPONENT_KEYS
                    if deskhand_diagnostics is not None else ())
        except Exception:
            keys = ()
        if not keys:
            keys = ("windows_app", "service", "gateway", "worker", "relay",
                    "tunnel", "node", "agent", "bridge", "grant", "muse",
                    "lifecycle", "phone")
        levels = {key: "INFO" for key in keys}
        try:
            if _debug_is_enabled("service"):
                levels["service"] = "DEBUG"
        except Exception:
            pass
        return levels

    def debug_session(self, command):
        """Debug-session control (E41 §4).

        op "debug_session", {"sink", "reason", "action"}:
          action "enable" — Phil only (actor must be "phil" or
            absent), a non-empty reason is required. Enables DEBUG on
            the named sink for 1 hour or until restart, logs the
            `debug.enabled` INFO line (code `debug.enabled`, actor
            `phil`) via the journal, and returns the session record.
          action "status" — the live record (or None) and the
            current level for the sink.
        Never enables DEBUG without Phil asking: this op is the
        mechanism, the chat request that invoked it is the
        authorization. Never raises for a bad sink/action — those are
        ValueErrors, like every other unknown op field.
        """
        if not isinstance(command, dict):
            command = {}
        # An expired session reverts the sink on the next op, not
        # only on the watchdog cadence.
        _apply_debug_levels()
        action = command.get("action", "status")
        if action == "enable":
            reason = command.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(
                    "debug_session enable requires a reason")
            reason = _truncate_text(reason.strip(), 200)
            actor = command.get("actor") or "phil"
            if actor != "phil":
                raise ValueError(
                    "only Phil may enable a debug session")
            record = _debug_enable_session(
                command.get("sink", "service"), reason, actor=actor)
            _apply_debug_levels()
            self._journal_debug_enabled(record)
            return {"ok": True, "action": "enable",
                    "sink": record.get("sink", "service"),
                    "enabled": True, "level": "DEBUG", "record": record}
        if action == "status":
            sink = _debug_normalize_sink(command.get("sink", "service"))
            enabled = _debug_is_enabled(sink)
            return {"ok": True, "action": "status", "sink": sink,
                    "enabled": enabled,
                    "level": "DEBUG" if enabled else "INFO",
                    "record": _debug_session_record(sink)}
        raise ValueError("unknown debug_session action %r" % (action,))

    def _journal_debug_enabled(self, record):
        """Log the `debug.enabled` INFO line for a new session (§4).

        Via the audit journal (#1, deskhand_task_agent_op._journal):
        level INFO, code `debug.enabled`, actor `phil`, detail
        "<sink> for <reason>". The line also goes to the service log
        (logger.info, so it lands in deskhand_service.log). When the
        journal writer is unavailable in this process, the same line
        goes to the runtime event feed instead — never lost, never
        raised. Never writes DEBUG to the journal.
        """
        sink = record.get("sink", "service") if isinstance(record, dict) else "service"
        reason = record.get("reason", "") if isinstance(record, dict) else ""
        detail = _truncate_text("%s for %s" % (sink, reason), 200)
        msg = "Debug session enabled for service log"
        try:
            logger.info("debug.enabled: %s", detail)
        except Exception:
            pass
        # The canonical JSONL record (E41 §2.4) via the retention
        # formatter: keys in order, scrubbed + truncated at write
        # time, code on the INFO line. The journal writer takes a
        # dict, so the formatted line is parsed back; when the
        # formatter is unavailable the same dict is built by hand.
        entry = {"level": "INFO", "component": "diagnostics",
                 "actor": "phil", "outcome": "ok", "code": "debug.enabled",
                 "msg": msg, "detail": detail}
        if _retention is not None:
            try:
                ts = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                entry = json.loads(_retention.format_jsonl_line(
                    ts, "INFO", "diagnostics", "phil", "ok", msg,
                    code="debug.enabled", detail=detail))
            except Exception:
                pass
        try:
            from deskhand_task_agent_op import _journal as _task_agent_journal
            if _task_agent_journal(self.data, entry):
                return True
        except Exception:
            pass
        try:
            self._append_vm_event(
                {"source": "diagnostics", "type": "debug.enabled",
                 "code": "debug.enabled", "actor": "phil", "level": "INFO",
                 "summary": msg,
                 "detail": {"sink": sink, "reason": reason}})
        except Exception:
            pass
        return False

    def _append_vm_event(self, entry):
        """Append one levelled line to vm_events.jsonl (E17 #2, E41 §3.2).

        The runtime-stream writer: entries carry `level` (DEBUG is
        never written here — a DEBUG entry is dropped, not served),
        the summary truncated to 200 chars, and the file rotates at
        5 MB x 3 generations. Never raises; a rotation failure keeps
        appending and warns once. Returns True when written.
        """
        try:
            item = dict(entry)
            if item.get("level") == "DEBUG":
                return False
            item.setdefault("ts", time.time())
            level = item.get("level")
            item["level"] = (level if isinstance(level, str)
                             and level in _RETENTION_LEVELS else "INFO")
            if "summary" in item:
                # Scrub at write time, then truncate (E41 §7 rule 2):
                # a JSONL sink must never carry a token-shaped value.
                item["summary"] = _truncate_text(
                    _scrub_text(item.get("summary") or ""), 200)
            path = self.data / "vm_events.jsonl"
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=True) + "\n")
            _rotate_log_file(path, VM_EVENTS_MAX_BYTES,
                             VM_EVENTS_BACKUP_COUNT)
            return True
        except Exception as error:  # noqa: BLE001 - the feed must not raise
            logger.warning("vm_events append failed: %s", error)
            return False

    def record_grant_event(self, code, generation=None, actor="restore",
                           detail=None, turn_id=None):
        """Append one §3.2 grant event to the runtime-stream feed.

        code is grant.on / grant.off / grant.generation_decrease and
        carries its §3.2 level (INFO / WARN / WARN). `generation` is
        the Full PC access Generation, always shown with a grant
        event. The caller is whoever observed the flip (the lifecycle
        monitor, the host); this service only records it, in the
        level the spec assigns. Returns True when written.
        """
        if code not in GRANT_EVENT_LEVELS:
            raise ValueError("unknown grant event code %r" % (code,))
        summaries = {
            "grant.on": "Full PC access turned on",
            "grant.off": "Full PC access turned off by itself",
            "grant.generation_decrease": "Full PC access generation decreased",
        }
        entry_detail = {"actor": actor}
        if isinstance(detail, dict):
            entry_detail.update(detail)
        elif detail is not None:
            entry_detail["detail"] = str(detail)[:200]
        if generation is not None:
            entry_detail["generation"] = generation
        entry = {"source": "grant", "type": code, "code": code,
                 "actor": actor, "level": GRANT_EVENT_LEVELS[code],
                 "summary": summaries[code], "detail": entry_detail}
        if generation is not None:
            entry["generation"] = generation
        if turn_id:
            entry["turn_id"] = turn_id
        return self._append_vm_event(entry)

    def _is_idle_for_prune(self):
        """True when no turn is queued/dispatching (E17 §3.4).

        Unprovable idleness fails closed: a mailbox read failure
        means "do not prune", never "prune anyway".
        """
        try:
            turns = self.box.recent(limit=5)
        except Exception:
            return False
        return not any(turn.get("status") in ("queued", "dispatching")
                       for turn in turns)

    def _maybe_prune_jett_journal(self, force=False):
        """Weekly, idle-only prune of the Jett journal (E17 §3.4).

        At most once a week (tracked in jett-prune-state.json so a
        restart does not re-prune), only while idle, via
        prune_jett_journal (30 days, level-blind). Never deletes the
        .db file or the WAL files. Never raises; returns the prune
        result dict, or None when the prune was not due, the service
        was busy, or no journal DB exists.
        """
        try:
            now = time.time()
            last = getattr(self, "_jett_prune_last", None)
            if last is None:
                last = 0
                try:
                    state = json.loads(
                        (self.data / _JETT_PRUNE_STATE_NAME).read_text(
                            encoding="utf-8"))
                    last = float(state.get("last_prune", 0) or 0)
                except Exception:
                    last = 0
                self._jett_prune_last = last
            if not force and now - last < _JETT_PRUNE_INTERVAL_SECS:
                return None
            if not self._is_idle_for_prune():
                return None
            db_paths = _jett_journal_db_paths(self.data)
            if not db_paths:
                return None
            results = [_prune_jett_journal(path, max_age_days=30)
                       for path in db_paths]
            self._jett_prune_last = now
            try:
                (self.data / _JETT_PRUNE_STATE_NAME).write_text(
                    json.dumps({"last_prune": now}), encoding="utf-8")
            except Exception:
                pass
            return {"pruned": results,
                    "db": [str(path) for path in db_paths]}
        except Exception as error:  # noqa: BLE001 - prune must not raise
            logger.warning("Jett journal prune skipped: %s", error)
            return None

    def _failfast_snapshot(self):
        """Snapshot for the fail-fast gate (100.020, D-020-1).

        The memoised snapshot when fresh (age <= 6 s, the codebase's
        STALE threshold); otherwise a bounded rebuild (short probe
        timeouts; any row that cannot be determined within budget is
        UNKNOWN, never DOWN). Returns None when no snapshot can be
        built at all -- the gate then allows the submit (UNKNOWN ->
        allow, spec D9). Never raises.
        """
        if deskhand_failfast is None:
            return None
        try:
            now = time.time()
            cache = getattr(self, "_health_cache", None)
            if isinstance(cache, dict):
                snapshot = cache.get("snapshot")
                as_of = cache.get("as_of")
                if (isinstance(snapshot, dict)
                        and isinstance(as_of, (int, float))
                        and now - as_of <= deskhand_failfast.STALE_SECS):
                    return snapshot
            return self._failfast_snapshot_bounded()
        except Exception as error:
            logger.warning("Fail-fast snapshot read failed: %s", error)
            return None

    # 100.041: a memoised C# row is usable for the
    # fail-fast gate only while health() is actually refreshing the
    # memo (phone 2.5 s poll, MainForm 2 s diagnostics tick). Older
    # than this, the rows are treated as unknown rather than
    # trusted: UNKNOWN -> allow, which is what the gate already does
    # when no snapshot can be built at all.
    FAILFAST_CSHARP_MAX_AGE_SECS = 60

    def _failfast_csharp_rows(self):
        """C# rows for the fail-fast rebuild (100.041).

        Never calls host_rpc: the submit path must not block on a
        health.snapshot round-trip (the 30 s HostRpc timeout is the
        received_at -> created stall). Returns the C# rows from the
        memoised health() snapshot when it is at most
        FAILFAST_CSHARP_MAX_AGE_SECS old and well-formed, else all
        UNKNOWN rows -- the gate treats UNKNOWN as allow, and the
        Muse-worker signal (checked first, no snapshot) still refuses
        when the worker is known down. Never raises.
        """
        if deskhand_diagnostics is None:
            return None
        try:
            cache = getattr(self, "_health_cache", None)
            lock = getattr(self, "_health_cache_lock", None)
            if lock is not None:
                with lock:
                    snapshot = (cache.get("snapshot")
                                if isinstance(cache, dict) else None)
                    as_of = (cache.get("as_of")
                             if isinstance(cache, dict) else None)
            else:
                snapshot = (cache.get("snapshot")
                            if isinstance(cache, dict) else None)
                as_of = (cache.get("as_of")
                         if isinstance(cache, dict) else None)
            if (not isinstance(snapshot, dict)
                    or not isinstance(as_of, (int, float))
                    or time.time() - as_of > self.FAILFAST_CSHARP_MAX_AGE_SECS):
                raise ValueError("no fresh memoised C# rows")
            components = snapshot.get("components")
            if not isinstance(components, dict):
                raise ValueError("memoised snapshot has no components")
            rows = {}
            for key in deskhand_diagnostics.CSHARP_KEYS:
                row = components.get(key)
                if (not isinstance(row, dict)
                        or row.get("status")
                        not in deskhand_diagnostics.VALID_STATUSES):
                    raise ValueError("memoised C# row %s malformed" % key)
                rows[key] = dict(row)
            return rows
        except Exception:
            return {key: deskhand_diagnostics.unknown_row()
                    for key in deskhand_diagnostics.CSHARP_KEYS}

    def _failfast_snapshot_bounded(self):
        """Bounded live rebuild of the health snapshot (D-020-1).

        Same builders as health(), but the bridge and agent probes run
        on a 0.5 s budget each (worst case ~1.5 s total, inside the
        ~2 s refusal budget). A probe that times out inside that
        budget is UNKNOWN, never DOWN: a timeout proves nothing about
        the path. Any other failure keeps the builder's own verdict
        (a fast refusal is a real signal). The rebuilt snapshot is
        memoised like a health() result. Never raises.

        100.041: the C# rows no longer come from a live
        host_rpc.call("health.snapshot") -- that call blocks up to
        the 30 s HostRpc timeout when the stdin loop is busy, which
        is the 30.0 s received_at -> created stall. They come from
        the memoised health() snapshot via _failfast_csharp_rows
        (freshness-bounded; stale or absent rows are UNKNOWN, and
        UNKNOWN -> allow). The submit path can never stall here,
        and the gate loses almost no fidelity: on timeout the rows
        were UNKNOWN -> allow anyway.
        """
        if deskhand_diagnostics is None:
            return None
        store = getattr(self, "_health_last_ok", None)
        if not isinstance(store, dict):
            store = {}
            self._health_last_ok = store
        snapshot = deskhand_diagnostics.build_health_snapshot(
            host_rpc=getattr(self, "host_rpc", None),
            csharp_rows_fn=self._failfast_csharp_rows,
            service_file_path=Path(__file__).resolve(),
            bridge_status_fn=lambda: self._bridge_status(timeout=0.5),
            agent_probe_fn=lambda: self._agent_health(timeout=0.5),
            lifecycle=getattr(self, "task_agent_lifecycle", None),
            data_dir=getattr(self, "data", None),
            lifecycle_module_path=Path(__file__).with_name(
                "deskhand_task_agent_lifecycle.py"),
            last_ok_store=store,
            levels=self._configured_levels())
        # 100.029: same belt-and-braces level key as health().
        # A bounded probe that timed out is UNKNOWN, never DOWN
        # (D-020-1): the row could not be determined within budget.
        try:
            agent = snapshot.get("components", {}).get("agent", {})
            if (isinstance(agent, dict) and agent.get("status") == "DOWN"
                    and deskhand_failfast.is_timeout_error(agent.get("error", ""))):
                snapshot["components"]["agent"] = deskhand_diagnostics.make_row(
                    "UNKNOWN", agent.get("last_ok", 0), agent.get("version", ""),
                    "bounded probe timed out; agent state unknown")
        except Exception:
            pass
        cache = {"snapshot": snapshot, "as_of": snapshot.get("as_of")}
        lock = getattr(self, "_health_cache_lock", None)
        if lock is None:
            self._health_cache = cache
        else:
            with lock:
                self._health_cache = cache
        return snapshot

    def _failfast_check(self, command, route, agent_task_id):
        """Fail-fast gate for one submit (100.020, D-020-11).

        Returns the refusal dict from deskhand_failfast.evaluate_gate,
        or None to allow. Never raises: a gate failure allows the
        submit (the in-flight trip is the backstop) and is logged.
        """
        if deskhand_failfast is None:
            return None
        try:
            # No origin/transport marker exists in the submit command
            # (verified: op/id/text/assistant/model/account/effort/
            # agent, plus agent_task_id added by C#); the gate module
            # applies the tunnel check to chat turns as the documented
            # approximation (D-020-9). An explicit marker, if one is
            # ever added, is honoured here.
            origin = (command.get("origin") or command.get("source")
                      or command.get("transport"))
            context = {"route": getattr(route, "assistant", None),
                       "agent_task_id": agent_task_id,
                       "origin": origin}
            # Identify the actual registered provider, rather than merely
            # an enabled configuration or a requested assistant name.
            adapter = self.dispatcher.adapters.get(route)
            provider = getattr(adapter, "provider", None)
            tunnel_served = (agent_task_id is None and deskhand_tunnel is not None
                             and isinstance(provider, deskhand_tunnel.TunnelProvider))
            context["tunnel_served"] = tunnel_served
            # 100.037 (E12 §7.2): the Muse-worker refusal is checked
            # FIRST and does not depend on the diagnostics snapshot —
            # the watchdog reads the worker transport in-process — so
            # a missing/stale snapshot must not let a send through
            # while the worker is known down. Chat turns only
            # (agent-task turns are gated on the agent row, D-020-9).
            # The relay-based worker refusal below is untouched: both
            # signals refuse, for different failures (spec §16).
            if agent_task_id is None and not tunnel_served:
                signal = self._muse_worker_gate_signal()
                if signal is not None:
                    refusal = deskhand_failfast.muse_worker_refusal(signal)
                    if refusal is not None:
                        return refusal
            snapshot = self._failfast_snapshot()
            if not isinstance(snapshot, dict):
                return None
            return deskhand_failfast.evaluate_gate(snapshot, context)
        except Exception as error:
            logger.warning("Fail-fast gate check failed: %s", error)
            return None

    def _failfast_journal(self, refusal, command, route):
        """Record one gate refusal (100.020, spec section 6.1 / D5).

        Journal gap (recorded, not invented): the work tree has no
        journal store the service can write a refusal into. The
        `activity` op reads the turn box (deskhand_mailbox is
        snapshot-only, not in this tree) and the Muse bridge audit --
        neither offers an append API in-tree, and a refused turn must
        NOT be written as a turn row (it never entered the mailbox).
        So a refusal is recorded three ways, none invented: (1) the
        service log, (2) one JSON line in the existing vm_events.jsonl
        event feed (same format runtime_stream() already parses, so
        the refusal surfaces in the runtime feed), (3) the refusal
        text itself, returned as the submit error. Best-effort: never
        raises, never blocks the refusal.
        """
        try:
            logger.warning(
                "Fail-fast refused submit %s (%s): path=%s dead_since=%s cause=%s",
                command.get("id"), getattr(route, "assistant", ""),
                refusal.get("path"), refusal.get("dead_since"),
                refusal.get("cause"))
        except Exception:
            pass
        # 100.029: the refusal is a levelled WARN event (code
        # failfast.refused) in the runtime feed, via the rotating
        # writer — never a bare append, so a refusal can no longer
        # grow vm_events.jsonl without bound.
        self._append_vm_event(
            {"ts": time.time(), "source": "failfast",
             "type": "failfast_refusal", "level": "WARN",
             "code": "failfast.refused",
             "actor": getattr(route, "assistant", "") or "",
             "summary": "refused submit: %s" % refusal.get("path", ""),
             "detail": {"path": refusal.get("path"),
                        "turn_id": command.get("id"),
                        "dead_since": refusal.get("dead_since"),
                        "cause": refusal.get("cause"),
                        "message": refusal.get("message", "")}})

    def _failfast_trip_check(self):
        """In-flight trip (100.020, D-020-8).

        On the 15 s stuck-turn watchdog cadence (the real window --
        the spec's ~3 s detection rule has no live-code grounding):
        when the dispatching turn's own path reads DOWN (worker for
        chat turns, agent for agent turns, last_ok > 6 s), mark it
        uncertain via the EXISTING mailbox.uncertain path. The turn
        is never failed and never replayed; the label change is the
        whole trip. The dispatch thread still owns the outcome: if
        the turn finished between the check and the write,
        uncertain() raises and the trip is a no-op.
        dispatcher.running_turn_info() is snapshot-only (not in this
        tree); if it does not exist on the live dispatcher, the trip
        is skipped and logged -- Gate 4 re-verifies it. Never raises.
        """
        if deskhand_failfast is None:
            return
        try:
            getter = getattr(self.dispatcher, "running_turn_info", None)
            if not callable(getter):
                logger.debug("Fail-fast trip skipped: dispatcher has no running_turn_info")
                return
            info = getter()
            if not isinstance(info, dict) or not info.get("turn_id"):
                return
            turn_id = info["turn_id"]
            # Ignore stale worker-health signals only for the exact running
            # plain-chat turn already proved to be served by the tunnel.
            tunnel_turn_id, tunnel_served = self._running_turn_is_tunnel_served()
            if tunnel_served and tunnel_turn_id == turn_id and info.get("agent_task_id") is None:
                return
            tripped = getattr(self, "_failfast_tripped", None)
            if not isinstance(tripped, set):
                tripped = set()
                self._failfast_tripped = tripped
            if turn_id in tripped:
                return
            # 100.037 (E12 §7.2): the Muse-worker signal trips
            # without a snapshot (the watchdog reads the transport
            # in-process); the relay path still needs its snapshot.
            # inflight_trip decides which signal applies.
            snapshot = self._failfast_snapshot()
            muse_worker = self._muse_worker_gate_signal()
            if not isinstance(snapshot, dict) and muse_worker is None:
                return
            trip, reason = deskhand_failfast.inflight_trip(
                snapshot, info, muse_worker=muse_worker)
            if not trip:
                return
            uncertain = getattr(self.box, "uncertain", None)
            if not callable(uncertain):
                logger.warning("Fail-fast trip skipped: mailbox has no uncertain path")
                return
            try:
                uncertain(turn_id, reason)
            except Exception:
                # Already terminal (finished/cancelled between the
                # check and the write): nothing left to trip.
                return
            if len(tripped) > 512:
                tripped.clear()
            tripped.add(turn_id)
            logger.warning("Fail-fast tripped dispatching turn %s to uncertain: %s",
                           turn_id, reason)
        except Exception as error:
            logger.warning("Fail-fast trip check failed: %s", error)

    # ------------------------------------------------------------------
    # 100.037 (E12): Muse-worker disconnect watchdog — service wiring.
    # Detection reads MuseConnection.status() in-process on a 1 s
    # timer (O4 default); the watchdog drives reconcile() on the §6.1
    # backoff and emits worker.* events + journal records. Nothing
    # here touches a grant, a tunnel, a process, or any STOP flag.
    # ------------------------------------------------------------------

    def _worker_watchdog_loop(self):
        """1 s in-process evaluation (O4 default).

        Same shape as the stuck-turn watchdog thread: wait on the
        stopping event, tick, never raise. The tick reads
        connection.status() only — no HTTP, no new poller.
        """
        interval = self.worker_watchdog_interval or 1.0
        while not self.stopping.is_set():
            if self.stopping.wait(interval):
                break
            try:
                self._worker_watchdog_tick()
            except Exception as exc:
                logger.warning("Worker watchdog tick failed: %s", exc)

    def _worker_watchdog_tick(self):
        """One evaluation tick: read, observe, attempt when due."""
        watchdog = getattr(self, "worker_watchdog", None)
        if watchdog is None:
            return
        ready, cause, grant_on = self._worker_watchdog_read()
        if ready is None:
            return  # no Muse worker attached or no status signal: missing
        if watchdog.observe(ready, cause, grant_on=grant_on):
            # A ready read while disconnected: verify with one
            # reconcile (§5.4) — a stale ready flag alone is never a
            # recovery, so this attempt is the verification.
            watchdog.attempt("auto", bridge_healthy=self._worker_watchdog_bridge_healthy)
            return
        if watchdog.attempt_due():
            watchdog.attempt("auto", bridge_healthy=self._worker_watchdog_bridge_healthy)

    def _worker_watchdog_read(self):
        """(ready, cause, grant_on) for the Muse worker transport.

        ready is MuseConnection.status()['ready'] via MuseService.
        Any read failure is not-ready (the 3-miss rule absorbs blips).
        A worker object without a status() signal reads as missing
        (None, None, None) — never as a disconnect; that is the
        fixture-muse case in VM tests, not a live signal.
        """
        muse = getattr(self, "muse", None)
        if muse is None:
            return None, None, None
        status_fn = getattr(muse, "status", None)
        if not callable(status_fn):
            return None, None, None
        cause, grant_on = self._worker_watchdog_cause()
        try:
            status = status_fn()
        except Exception:
            return False, cause, grant_on
        if not isinstance(status, dict):
            return False, cause, grant_on
        https = status.get("https_connection")
        if not isinstance(https, dict):
            return False, cause, grant_on
        return (https.get("ready") is True), cause, grant_on

    def _worker_watchdog_cause(self):
        """(cause, grant_on) — the last known cause, never invented (§5.3).

        cause is grant_off / grant_paused / tunnel_down / None
        (unknown); grant_on is True / False / None (positive evidence
        about the grant only — the watchdog re-arms a grant stop-early
        on grant_on True, never on an unknown cause). A fresh memoised
        health snapshot is authoritative for both: its C#-owned
        grant/tunnel rows are real when host_rpc is live. Only when
        no fresh snapshot exists is the persisted grant re-read (fails
        closed: a missing or unreadable config reads as grant off,
        like _task_agent_grant_on).
        """
        try:
            cache = getattr(self, "_health_cache", None)
            if isinstance(cache, dict):
                snapshot = cache.get("snapshot")
                as_of = cache.get("as_of")
                if (isinstance(snapshot, dict)
                        and isinstance(as_of, (int, float))
                        and time.time() - as_of <= 6):
                    comps = snapshot.get("components") or {}
                    grant_status = (comps.get("grant") or {}).get("status")
                    tunnel_down = ((comps.get("tunnel") or {}).get("status")
                                   == "DOWN")
                    if grant_status == "DOWN":
                        return "grant_off", False
                    if grant_status == "IDLE":
                        return "grant_paused", False
                    if grant_status == "OK":
                        return ("tunnel_down", True) if tunnel_down else (None, True)
                    if tunnel_down:
                        return "tunnel_down", None
                    return None, None
        except Exception:
            pass
        try:
            if _task_agent_grant_on(self.data):
                return None, True
            return "grant_off", False
        except Exception:
            return None, None

    def _worker_watchdog_bridge_healthy(self):
        """True only when the laptop-agent bridge answers /health.

        The reconcile-failing stop-early (§6.3) consults this: a
        persistently failing reconcile with a healthy bridge means the
        failure is in the worker path. Bounded probe (0.5 s); a probe
        failure is False, never an exception. Called lazily, only when
        an attempt has already failed, so the 1 s tick never pays it.
        """
        try:
            return self._bridge_health(timeout=0.5)
        except Exception:
            return False

    def _worker_watchdog_reconnect(self, kind):
        """One reconnect attempt. Returns (ok, error).

        Drives reconcile() via MuseService.ensure_chat_ready() — the
        live reconnect entry point. ok is True ONLY after a clean
        reconcile AND status()['ready'] True afterwards (§5.4, H4):
        ensure_chat_ready raises on a failed reconcile, and the ready
        check after it is read fresh, never from a stale flag.
        """
        muse = getattr(self, "muse", None)
        if muse is None:
            return False, "Muse integration is not enabled"
        ready_fn = getattr(muse, "ensure_chat_ready", None)
        if not callable(ready_fn):
            return False, "Muse worker has no reconnect path"
        try:
            ready_fn()
        except Exception as exc:
            return False, str(exc) or "ensure_chat_ready failed"
        try:
            status = muse.status()
        except Exception as exc:
            return False, str(exc) or "muse_status failed after reconcile"
        https = status.get("https_connection") if isinstance(status, dict) else None
        if not isinstance(https, dict) or https.get("ready") is not True:
            return False, str((https or {}).get("error")
                              or "Muse worker not ready after reconcile")
        return True, None

    def _worker_watchdog_event(self, event):
        """Sink one watchdog event (§9): runtime feed + audit journal.

        Stream events (the four §10.4 types) go to vm_events.jsonl
        via _append_vm_event so runtime_stream() serves them to the
        phone; every event — including the journal-only re-arm note —
        goes to the audit journal via deskhand_task_agent_op._journal
        with component "watchdog". Never raises.
        """
        if not isinstance(event, dict) or not event.get("type"):
            return
        try:
            if event["type"] in WORKER_STREAM_EVENT_TYPES:
                self._append_vm_event({
                    "ts": event.get("ts", time.time()),
                    "source": "worker",
                    "type": event["type"],
                    "level": event.get("level", "INFO"),
                    "code": event.get("code", "worker.event"),
                    "actor": "watchdog",
                    "summary": event.get("summary", ""),
                    "detail": event.get("detail") or {}})
        except Exception:
            pass
        try:
            from deskhand_task_agent_op import _journal as _task_agent_journal
            _task_agent_journal(self.data, {
                "level": event.get("level", "INFO"),
                "component": "watchdog",
                "actor": "watchdog",
                "outcome": event.get("outcome", "ok"),
                "code": event.get("code", "worker.event"),
                "msg": event.get("summary", "") or event["type"],
                "detail": event.get("detail") or {}})
        except Exception:
            pass

    def _muse_worker_gate_signal(self):
        """Fired watchdog state for the fail-fast gate/trip, or None.

        The relay-based snapshot path is untouched — both signals
        refuse, for different failures (spec §16).
        """
        watchdog = getattr(self, "worker_watchdog", None)
        if watchdog is None:
            return None
        try:
            return watchdog.muse_signal()
        except Exception:
            return None

    def _worker_reconnect_manual(self):
        """Manual Reconnect op (100.037 §10.5; D1 fix: flat return).

        One reconcile() immediately, outside the backoff schedule;
        single-flight with any in-flight attempt (a press while an
        attempt is in flight returns the in-flight outcome). Returns
        the attempt outcome + the current watchdog state as FLAT
        top-level keys — the shape MainForm.ApplyWorkerReconnectResult
        parses (attempts, disconnected_since, next_delay_s, cause,
        reconnected, ready + reconcile_ok, status,
        auto_reconnect_stopped). No nested ``attempt`` / ``watchdog``
        grouping: MainForm reads only top-level keys, and the nested
        shape left its failure-path render stale until the next poll.
        Never touches a grant, an approval, a tunnel, a process, or
        STOP.
        """
        if getattr(self, "muse", None) is None:
            raise RuntimeError("Muse integration is not enabled")
        watchdog = getattr(self, "worker_watchdog", None)
        if watchdog is None:
            ok, error = self._worker_watchdog_reconnect("manual")
            ok = bool(ok)
            return {"reconnected": ok,
                    "ran": True, "kind": "manual", "ok": ok,
                    "error": error, "attempt": None, "recovered": ok,
                    "attempts": 0, "disconnected_since": None,
                    "next_delay_s": None, "cause": None,
                    "ready": ok, "reconcile_ok": ok,
                    "status": "connected" if ok else "disconnected",
                    "auto_reconnect_stopped": False,
                    "stopped_reason": None}
        was_down = bool(watchdog.state()["fired"])
        result = watchdog.manual_attempt(
            bridge_healthy=self._worker_watchdog_bridge_healthy)
        # Read the state AFTER the attempt: on verified recovery the
        # watchdog has reset (fired False, attempts 0, since None) and
        # the flat keys below carry the attempt outcome instead; on
        # failure the state is the live episode (attempts, since, next
        # delay, cause, stopped reason). ``ok`` is True only after a
        # clean reconcile AND ready afterwards (§5.4), so ready /
        # reconcile_ok / status all derive from it.
        ok = bool(result.get("ok"))
        state = watchdog.state()
        stopped = state.get("stopped_reason")
        return {"reconnected": ok and was_down,
                "ran": bool(result.get("ran")),
                "kind": result.get("kind"),
                "ok": ok, "error": result.get("error"),
                "attempt": result.get("attempt"),
                "recovered": bool(result.get("recovered")),
                "attempts": state.get("attempts"),
                "disconnected_since": state.get("disconnected_since"),
                "next_delay_s": state.get("next_delay_s"),
                "cause": state.get("cause"),
                "ready": ok, "reconcile_ok": ok,
                "status": "connected" if ok else "disconnected",
                "auto_reconnect_stopped": stopped is not None,
                "stopped_reason": stopped}


    def diagnostics_export(self, command=None):
        """Scrubbed diagnostics export bundle (100.016, spec 7-8).

        DECISION (Worker A): the Android share sheet is served by THIS
        separate `diagnostics_export` op, not by carrying the scrubbed
        tail inside the `health` op response. The health snapshot stays
        a small fast poll; the export (log tails + scrub) is built on
        demand only. The scrub pass runs here, on the PC, in the
        service, before anything crosses the session (spec 7.3). The
        Windows Copy button uses this same op, so both platforms share
        the one scrubber in the service -- no second scrubber anywhere.
        Returns {"ok", "filename", "text", "as_of"}.
        """
        if deskhand_diagnostics is None:
            return {"ok": False, "error": "diagnostics module not loaded"}
        snapshot = self.health()
        if not snapshot.get("ok"):
            return {"ok": False, "error": "health snapshot unavailable"}
        windows_version = (snapshot.get("components", {})
                           .get("windows_app", {}).get("version", ""))
        supplied = command if isinstance(command, dict) else {}
        if not windows_version:
            windows_version = supplied.get("windows_version", "") or ""
        # 100.029 (E41 §6.1): level-aware tail. level_filter "all"
        # (default) is the last 50 lines at INFO and above, DEBUG
        # excluded; "warn_error" is the last 50 WARN/ERROR lines only.
        # include_debug is the Windows-only toggle: the Android share
        # never passes it, and an explicit android platform is
        # ignored here even if it does.
        level_filter = supplied.get("level_filter", "all")
        if level_filter not in ("all", "warn_error"):
            raise ValueError("unknown level_filter %r" % (level_filter,))
        include_debug = supplied.get("include_debug", False)
        if not isinstance(include_debug, bool):
            raise ValueError("include_debug must be a boolean")
        platform = supplied.get("platform") or supplied.get("source")
        if isinstance(platform, str) and platform.lower() in ("android", "phone"):
            include_debug = False
        filename, text = deskhand_diagnostics.build_export(
            snapshot,
            data_dir=getattr(self, "data", None),
            config=supplied.get("config")
            if isinstance(supplied.get("config"), dict) else None,
            windows_version=windows_version,
            android_version=supplied.get("android_version", "") or "",
            service_dir=Path(__file__).resolve().parent,
            lifecycle_module_path=Path(__file__).with_name(
                "deskhand_task_agent_lifecycle.py"),
            level_filter=level_filter, include_debug=include_debug)
        return {"ok": True, "filename": filename, "text": text,
                "as_of": snapshot.get("as_of")}

    def activity(self):
        """Live runtime activity for the main-page panel.

        Returns {"status", "task", "files"}:
        - status: "busy" while a turn is queued/dispatching, else "idle".
        - task: the current turn (the message being worked on, truncated
          to 120 chars, with its status and assistant) — the most recent
          queued/dispatching turn, or the most recent turn when idle so
          the panel shows what was last done instead of going blank.
        - files: the most recent file/command operations from the vendor
          bridge audit (fs.read/write/list, exec.run with working
          directory), most-recent-first, up to 8. Empty when Muse is
          not enabled or the bridge has no such operations.
        Never raises: a failed read returns an empty/idle activity
        rather than breaking the panel's poll.
        """
        task = None
        busy = False
        try:
            turns = self.box.recent(limit=5)
        except Exception:
            turns = []
        for turn in reversed(turns):
            if turn.get('status') in ('queued', 'dispatching'):
                task = {'text': (turn.get('text') or '')[:120],
                        'status': turn.get('status'),
                        'assistant': turn.get('assistant'),
                        'updated': turn.get('updated')}
                busy = True
                break
        if task is None and turns:
            last = turns[-1]
            task = {'text': (last.get('text') or '')[:120],
                    'status': last.get('status'),
                    'assistant': last.get('assistant'),
                    'updated': last.get('updated')}
        files = []
        if self.muse is not None:
            try:
                files = self.muse.recent_file_activity(limit=8)
            except Exception:
                files = []
        return {'status': 'busy' if busy else 'idle', 'task': task,
                'files': files,
                'as_of': time.time()  # v100.002: poll timestamp (Unix epoch).
                # The panel keeps the last good text when a poll fails,
                # so without this the UI cannot tell fresh data from a
                # stale/cached read. MainForm can now show "updated Xs
                # ago" or flag data older than its 2s poll interval.
                }

    def close(self):
        # B2-D B-05: close every owned child in a guarded sequence.
        # The old code called self.muse.close() bare: a raise there
        # (e.g. MuseConnection.close() on a funnel-ownership hiccup)
        # aborted close() before the bridge child and the task-agent
        # lifecycle were stopped, orphaning them after the service
        # reported itself closed. Each step below is attempted even
        # when an earlier one fails; close() never raises.
        def _try_close(name, fn):
            # fn is a zero-arg lambda: the attribute lookup itself
            # happens inside the try (a duck-typed/fake child may lack
            # the method entirely -- that must not abort shutdown).
            try:
                fn()
            except Exception as error:
                logger.warning("ChatService.close: %s failed: %s", name, error)
        self.stopping.set()
        _try_close("dispatcher stop", lambda: self.dispatcher.shutdown())
        if self.host_rpc is not None:
            _try_close("host_rpc.close", lambda: self.host_rpc.close())
        self.wake.set()
        _try_close("local_runtime.close", lambda: self.local_runtime.close())
        if self.muse is not None:
            _try_close("muse.prepare_shutdown",
                       lambda: self.muse.prepare_shutdown())
        self.worker.join(5)
        self.watchdog.join(5)
        worker_watchdog = getattr(self, "_worker_watchdog_thread", None)
        if worker_watchdog is not None:
            worker_watchdog.join(5)
        # A request timeout does not guarantee a bounded DNS or OS call.
        # Bound shutdown, but never hand queue ownership to another
        # dispatcher while either worker could still touch the mailbox.
        self._tunnel_receipts_thread.join(5)
        if self._tunnel_receipts_thread.is_alive():
            logger.warning("Tunnel receipt worker did not stop; retaining queue ownership until process exit")
        if not self.worker.is_alive() and not self._tunnel_receipts_thread.is_alive():
            _try_close("dispatcher exit", lambda: self.dispatcher.__exit__())
        if self.muse is not None:
            _try_close("muse.close", lambda: self.muse.close())
        # Warm Codex processes (Batch 1, v100.003): adapters are released
        # at shutdown, after the dispatcher has exited so no turn is
        # dispatching. Codex adapters sit behind HostToolChat, which
        # forwards close() to its provider; other adapters are closed
        # directly when they offer close(). Never raises.
        for adapter in list(self.dispatcher.adapters.values()):
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        _try_close("bridge stop", lambda: self._stop_bridge())
        if self.task_agent_lifecycle is not None:
            _try_close("task_agent_lifecycle.close",
                       lambda: self.task_agent_lifecycle.close())
        # 100.029: detach the service-stderr sink bound to this
        # service's data dir, so a later service in the same process
        # (tests) does not keep writing to a deleted directory.
        _try_close("service log handler",
                   lambda: _release_service_log_handler(self.data))


def _auto_release_muse_delivery(staged, interval=5.0, stop=None):
    """Auto-release the Muse delivery hold for the life of the service (100.013, Item 5).

    Loops forever (until stop() returns True, test-only): every ~5 s,
    when the bridge is connected and the browser is signed in
    (browser.cloud_ready -- the same check prepare_host_tools() uses),
    calls ensure_chat_ready() on the staged service. On success it keeps looping, so
    the hold is re-released if it ever re-asserts; on failure it logs
    at debug level and keeps looping, never raising. The 2 s reconcile
    throttle inside ensure_chat_ready() (ENSURE_RECONCILE_THROTTLE_SECS)
    bounds the reconcile rate. Never holds dispatcher or Muse locks
    while sleeping: the sleep happens outside any lock, and
    ensure_chat_ready() holds the Muse lock only for its own call.
    """
    while stop is None or not stop():
        try:
            browser = getattr(staged, "browser", None)
            if browser is not None and getattr(browser, "cloud_ready", False):
                try:
                    state = staged.bridge.state()
                except Exception:
                    state = {}
                if isinstance(state, dict) and state.get("connected"):
                    staged.ensure_chat_ready()
        except Exception as error:
            logger.debug("Muse auto-release retrying: %s", error)
        time.sleep(interval)

def muse_reconnect_wiring(service):
    """Build the (in_flight, on_interrupted) pair for a MuseService.

    v2 (Fix 2 - in-flight guard wiring): the guard is the REAL
    dispatcher idle event, read through DispatcherIdleGuard, so a
    force-restart is refused while a turn is dispatching and is
    re-checked immediately before the kill. The guard is proof-gated
    on live dispatcher state, never guessed: if the dispatcher had
    no idle event the guard would fail closed (report in flight).
    The interrupted handler marks dispatching turns uncertain in the
    chat mailbox after a restart, never replaying them. Both are
    passed to MuseService at construction; MuseService forwards them
    to MuseConnection, which refuses force-restarts when no guard is
    wired. Until this wiring existed, production force-restarts were
    always refused (safe but inert).
    """
    from deskhand_reconnect_v2 import DispatcherIdleGuard, mark_interrupted_uncertain
    guard = DispatcherIdleGuard(service.dispatcher)
    return (guard.is_in_flight,
            lambda reason: mark_interrupted_uncertain(service.box, reason))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    output_lock = threading.Lock()
    def emit(value):
        with output_lock:
            print(json.dumps(value, ensure_ascii=True), flush=True)
    host = HostRpc(emit, action_timeout=150)
    service = ChatService(args.data, args.fixture, host_rpc=host)
    if args.fixture and os.environ.get('DESKHAND_MUSE_HOST_FIXTURE') == '1' and os.environ.get('DESKHAND_CONFIG_DIR'):
        from test_muse_host_runtime import create_fixture
        service.attach_muse(create_fixture(args.data))
    # Start the DeskHand-owned backend by default when the host authority is available.
    if not args.fixture and os.environ.get('DESKHAND_HOST_STOP_EVENT') and os.environ.get('DESKHAND_STAGED_MUSE') != '0':
        from deskhand_muse_service import MuseService
        base = Path(__file__).resolve().parent.parent
        source = base / 'jettpad'
        if not source.is_dir():
            source = base / 'vendor' / 'jettpad'
        # v2 (Fix 2): wire the real dispatcher in-flight guard into
        # the Muse connection (see muse_reconnect_wiring). Without
        # it, force-restarts are refused; with it, a restart is
        # deferred while a turn dispatches and interrupted turns are
        # marked uncertain in the chat mailbox, never replayed.
        in_flight, on_interrupted = muse_reconnect_wiring(service)
        staged = MuseService(args.data / 'muse-runtime', source, manage_connection=True,
                             in_flight=in_flight, on_interrupted=on_interrupted)
        try:
            staged.start()
            service.attach_muse(staged)
            try:
                staged.bridge.local_action('start')
            except Exception as error:
                # Keep setup available when a listener cannot start (for
                # example, no configured Tailscale address or a busy port).
                service.muse_error = 'Muse connection could not start: ' + str(error)[:300]
            # 100.013 Item 5: daemon auto-release loop for the life of
            # the service -- releases (and re-releases) the delivery
            # hold once the bridge is connected and the browser is
            # signed in, with no manual tap. Best-effort by design:
            # the loop never raises and never takes chat down.
            threading.Thread(target=_auto_release_muse_delivery, args=(staged,),
                           daemon=True, name="muse-auto-release").start()
        except Exception as error:
            staged.close()
            # Muse startup must not take the existing Codex/local chat down.
            service.muse_error = 'Muse could not start: ' + str(error)[:300]
    # Laptop-agent lifecycle: Deskhand owns the agent. Start (or
    # adopt) it and begin monitoring only when the Full PC access
    # grant is on; the lifecycle re-checks the grant on every poll
    # and stops the owned child if the grant turns off. Failures
    # here are recorded in task_agent_lifecycle.log; the fallback
    # hook still works through an agent started elsewhere.
    if service.task_agent_lifecycle is not None and _task_agent_grant_on(args.data):
        try:
            service.task_agent_lifecycle.ensure_started()
            service.task_agent_lifecycle.start_monitoring()
        except Exception as error:
            # B2-D M3: the old bare `except: pass` silently disabled
            # monitoring forever when the boot start failed (grant off
            # at boot, agent missing, 30 s health timeout) -- the
            # monitor that would retry/recover never existed until the
            # next service restart, and nothing surfaced it. Record
            # the failure on the service and log it; the fallback hook
            # still works through an agent started elsewhere.
            service.task_agent_lifecycle_error = str(error)[:300]
            logger.warning("Task-agent lifecycle failed to start: %s; "
                           "monitoring not started; fallback still works "
                           "through an agent started elsewhere", error)
    submit_threads = []
    def respond(command, ticket=None):
        try:
            emit({"rpc": command["rpc"], "ok": True, "result": service.handle(command, submission_ticket=ticket)})
        except Exception as error:
            emit({"rpc": command.get("rpc"), "ok": False, "error": str(error)})
    try:
        for line in sys.stdin:
            command = {}
            try:
                command = json.loads(line)
                if command.get("op") == "host_result":
                    host.receive(command.get("host_call"), command.get("result"))
                    continue
                if command.get("op") in {"submit", "catalog", "local_runtime_start", "task_agent_action", "task_agent_start", "access_validate", "health", "search_text", "search_files", "search_symbol", "transfer_put", "transfer_init", "transfer_chunk", "transfer_finalize", "transfer_status", "transfer_cancel", "transfer_verify", "transfer_read", "verify_build"}:
                    # 100.046 round 1 (N1): the search ops run threaded too.
                    # Search is synchronous and bounded by timeout_s
                    # (default 30 s, max 120 s) -- inline, a slow search
                    # stalls the stdin loop (chat) for up to 120 s, the
                    # same stall shape that got task_agent_action (30 s)
                    # and access_validate (25 s) threaded. No new
                    # machinery: respond() already emits the result on
                    # completion, and search is read-only with no
                    # per-call state on the cached Searcher, so
                    # overlapping searches are safe (bounded by the
                    # 16-thread cap below). Unlike health() there is
                    # no self-deadlock risk -- search needs no host
                    # reply via the loop -- this is responsiveness,
                    # not a correctness fix.
                    # 100.047 (B35): the transfer ops run threaded too.
                    # put/chunk/init are single small writes, but
                    # finalize reassembles up to 256 MiB from part
                    # files and hashes the result twice, and
                    # verify/read hash the full file at the target
                    # -- on a slow disk that is seconds of
                    # synchronous I/O, the same stall shape that
                    # got task_agent_action (30 s) and the search
                    # ops (up to 120 s) threaded. No new
                    # machinery: respond() already emits the result
                    # on completion, and the Transfer guards its
                    # registry with a lock while part/temp files
                    # are per-session, so overlapping transfers
                    # are safe (bounded by the 16-thread cap
                    # below; the module's own MAX_SESSIONS cap of
                    # 8 bounds chunked sessions). Unlike health()
                    # there is no self-deadlock risk -- transfer
                    # needs no host reply via the loop -- this is
                    # responsiveness, not a correctness fix.
                    # 100.048 (B36): verify_build runs threaded too.
                    # The op runs long commands -- up to the overall
                    # timeout (default 3600 s) -- inline it would
                    # stall the stdin loop for up to 3600 s, the same
                    # stall shape that got run_* / search_* /
                    # transfer_* threaded. No new machinery:
                    # respond() already emits the result on
                    # completion.
                    # task_agent_action runs threaded: a fallback call may
                    # take up to the 30s agent timeout, and must not block
                    # the stdin loop (chat) while it runs. task_agent_start
                    # is threaded for the same reason: ensure_started()
                    # waits up to the 30s start timeout for /health.
                    # B2-D B-06: access_validate is threaded too -- for
                    # Muse it runs prepare_host_tools(), a 25 s enable
                    # loop that used to run inline on the stdin loop and
                    # block every other host op (including a stop) for
                    # up to 25 s while wedged.
                    # 100.041: health is threaded too -- it
                    # calls build_health_snapshot, whose first step is a
                    # synchronous host_rpc.call("health.snapshot") that
                    # blocks up to the 30 s HostRpc timeout, and the
                    # reply can only arrive via the stdin loop
                    # (host.receive is called only there). Inline,
                    # health() self-deadlocks for exactly 30 s, and a
                    # concurrent submit thread's health.snapshot call
                    # stalls with it -- that is the 30.0 s
                    # received_at -> created gap. On a worker thread
                    # the stdin loop stays free to deliver the reply,
                    # so health() completes in milliseconds and no
                    # longer blocks submits. Callers already guard
                    # against overlap (phone 2.5 s poll guard,
                    # MainForm _diagnosticsPolling), and health()
                    # only reads + memoises, so overlap is benign.
                    submit_threads = [thread for thread in submit_threads if thread.is_alive()]
                    if len(submit_threads) >= 16:
                        raise RuntimeError("Too many pending assistant requests")
                    ticket = service.dispatcher.admission_ticket() if command.get("op") == "submit" else None
                    thread = threading.Thread(target=respond, args=(command, ticket), daemon=True)
                    submit_threads.append(thread)
                    thread.start()
                    continue
                value = {"rpc": command["rpc"], "ok": True, "result": service.handle(command)}
            except Exception as error:
                value = {"rpc": command.get("rpc"), "ok": False, "error": str(error)}
            emit(value)
    finally:
        service.close()


if __name__ == "__main__":
    main()
