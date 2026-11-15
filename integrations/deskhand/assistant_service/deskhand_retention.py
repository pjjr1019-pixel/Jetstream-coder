"""Shared log-retention + structured-logging helpers (Deskhand 100.029).

E17 (log retention, Option A) + E41 (structured, levelled logs). One
small module so every writer agrees on the same rules:

* Retention (E17): rename-based rotation (never truncate in place),
  protected state/config files that rotation must never touch, and
  the SQLite prune for the Jett journal (rows only, then VACUUM --
  never delete the .db or WAL files by hand).
* Structure (E41): the four levels, the fixed actor / component /
  outcome vocabularies, the canonical JSONL record shape (keys in
  the spec section 2.4 order) and the plain-text shape
  ``[ts] [LEVEL] [component/actor] code -- msg``, 200-char truncation
  and token scrubbing at write time, and in-memory debug sessions
  (1 hour or until restart, whichever comes first).

This module is the cross-worker contract for 100.029: Workers B and
E depend on the names below. Do not rename them.

Levels / vocabularies are enforced, not suggested: the formatters
raise ValueError on a level, actor, component or outcome outside the
fixed lists, so a writer inventing a new component at a call site
fails loudly instead of shipping a line no reader can filter on.

NOTE on prune_jett_journal vs the live schema: the E17 spec writes
the prune as ``DELETE FROM events WHERE ts < <cutoff>``, but the live
``JettJournal.cs`` events table has no ``ts`` column -- its timestamp
column is ``happened_utc TEXT`` (ISO-8601) -- and it carries an
append-only trigger (``events_no_delete``) that ABORTs any DELETE on
events. This function therefore picks the timestamp column that
actually exists (``ts`` if present, else ``happened_utc``) and, when
the append-only trigger refuses the DELETE, raises RuntimeError
instead of silently returning 0 or dropping the trigger itself.
Dropping that trigger is a C#-side decision for the coordinator /
Worker D, not something a Python prune may do on its own. (Verified
against ``100029-build/work/windows/JettJournal.cs`` while writing
this module; see the worker report.)
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
ACTORS = ("phil", "system", "restore", "service", "agent", "watchdog",
          "janitor", "bridge", "host", "updater")
COMPONENTS = ("chat_service", "dispatcher", "watchdog", "lifecycle",
              "bridge", "tunnel_watchdog", "agent_watchdog", "agent",
              "launcher", "grant", "gateway", "relay", "updater",
              "diagnostics", "router", "muse", "codex", "journal")
OUTCOMES = ("ok", "failed", "refused", "started", "stopped", "skipped",
            "stale", "recovered", "interrupted", "unchanged")

# Files rotation must never touch (E17 section 2: "Not logs, never
# touched by rotation"). Compared case-insensitively.
_PROTECTED_NAMES = frozenset({
    "lifecycle.json", "agent_config.json", "config.json",
    "musebridge.config.json", "version.properties", "changelog.kt",
})

_DEBUG_SESSION_TTL_SECONDS = 3600  # E41 D3: 1 hour or until restart.
_DEBUG_SESSIONS = {}  # sink -> {"sink", "reason", "actor", "expires_at"}

# A token-shaped run: 32+ chars of hex / base64 / base64url. The
# lookarounds are deliberately "=" free: a run may start right after
# a "=" (``token=<64 hex>``) and may end in base64 padding, and it
# must not be part of a still-longer run.
_TOKEN_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/_-])")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_URL_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>?#]+)\?([^\s\"'<>#]*)")
_TOKEN_PARAM_RE = re.compile(
    r"(?:^|[?&])(token|secret|api[-_]?key|apikey|password|auth"
    r"|access[-_]?token|session|bearer)\b", re.IGNORECASE)


def truncate_text(s, limit=200):
    """Truncate to at most ``limit`` chars (plain slice, no ellipsis).

    The result is always ``<= limit`` chars, so a formatted ``msg`` /
    ``detail`` satisfies the E41 200-char bound. ``None`` becomes "".
    """
    if s is None:
        return ""
    text = s if isinstance(s, str) else str(s)
    return text if len(text) <= limit else text[:limit]


def scrub_text(s):
    """Redact token-shaped values from one string (E17 section 8).

    * A 32+ char hex / base64 / base64url run becomes
      ``[REDACTED-TOKEN]``. A UUID (36 chars with hyphens) is NOT a
      token and is left alone -- turn/task ids must stay readable.
    * A URL whose query carries a token parameter (``token=``,
      ``secret=``, ``api_key=``, ``password=``, ``auth=``,
      ``access_token=``, ``session=``, ``bearer=``) keeps its base
      URL and has the whole query replaced with ``[REDACTED]``.
      A URL with no token parameter is left alone.
    """
    if s is None:
        return ""
    text = s if isinstance(s, str) else str(s)

    def _scrub_url(match):
        if _TOKEN_PARAM_RE.search(match.group(2)):
            return match.group(1) + "?[REDACTED]"
        return match.group(0)

    text = _URL_RE.sub(_scrub_url, text)

    def _scrub_run(match):
        if _UUID_RE.match(match.group(0)):
            return match.group(0)
        return "[REDACTED-TOKEN]"

    return _TOKEN_RUN_RE.sub(_scrub_run, text)


def _scrub_value(value, limit=200):
    """Scrub + truncate a ``detail`` value (str, dict, list, or other).

    Dicts/lists keep their shape -- string leaves are scrubbed and
    truncated individually, non-string leaves pass through unchanged.
    """
    if isinstance(value, dict):
        return {key: _scrub_value(item, limit)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item, limit) for item in value]
    if value is None:
        return None
    return truncate_text(scrub_text(str(value)), limit)


def rotate_if_needed(path, max_bytes, backup_count):
    """Rotate ``path`` when it is over ``max_bytes``.

    Rename, never truncate in place: generations shift down
    (``log.1`` -> ``log.2``, ...), the oldest past ``backup_count``
    is deleted, then ``log`` -> ``log.1``. Returns True when a
    rotation happened. Returns False when no rotation was needed
    (file missing or under the cap), when the path is a protected
    state/config file, or when any step failed (locked, disk full,
    permission, a directory sitting in a generation slot) -- in the
    failure case the current file is left untouched and the caller
    logs the failure once and keeps appending.
    """
    try:
        target = Path(path)
        if target.name.lower() in _PROTECTED_NAMES:
            return False
        if max_bytes <= 0 or backup_count < 1:
            return False
        if not target.is_file():
            return False
        if target.stat().st_size <= max_bytes:
            return False
        for generation in range(backup_count, 0, -1):
            slot = Path("%s.%d" % (target, generation))
            if generation == backup_count:
                if slot.is_file() or slot.is_symlink():
                    slot.unlink()
                elif slot.exists():
                    # A directory (or something else) in the oldest
                    # slot: refuse rather than delete what is not a
                    # log generation.
                    return False
            elif slot.exists():
                os.replace(slot, Path("%s.%d" % (target, generation + 1)))
        os.replace(target, Path("%s.1" % target))
        return True
    except OSError:
        return False


def prune_jett_journal(db_path, max_age_days=30):
    """Prune Jett-journal rows older than ``max_age_days``, then VACUUM.

    ``DELETE FROM events WHERE <ts> < (now - max_age_days)``; returns
    the rows deleted. The timestamp column is ``ts`` when the table
    has one, else the live schema's ``happened_utc`` (ISO-8601 text);
    an INTEGER-typed timestamp is compared against an epoch cutoff
    instead, so a type mismatch can never delete the whole table.
    Never deletes the .db file or the WAL files by hand -- rows only,
    through SQL. A missing file (or a file with no events table)
    prunes nothing and returns 0. If the live append-only trigger
    (``events_no_delete``) refuses the DELETE, raises RuntimeError:
    that trigger is C#-owned and the prune cannot proceed until the
    coordinator decides how the C# side exempts it -- this function
    will not drop it, and will not pretend the prune happened.
    """
    path = Path(db_path)
    if not path.is_file():
        return 0
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None  # autocommit: VACUUM needs no txn.
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'events'").fetchall()
        if not rows:
            return 0
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(events)").fetchall()]
        if "ts" in columns:
            column = "ts"
        elif "happened_utc" in columns:
            column = "happened_utc"
        else:
            raise ValueError(
                "events table has no ts / happened_utc column; "
                "cannot prune %s" % path)
        sample = conn.execute(
            "SELECT %s FROM events WHERE %s IS NOT NULL LIMIT 1"
            % (column, column)).fetchone()
        if sample is None:
            return 0
        if isinstance(sample[0], (int, float)) and not isinstance(
                sample[0], bool):
            cutoff = int(time.time()) - int(max_age_days) * 86400
        else:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=max_age_days)).isoformat(
                          timespec="seconds")
        try:
            cursor = conn.execute(
                "DELETE FROM events WHERE %s < ?" % column, (cutoff,))
            deleted = cursor.rowcount
        except sqlite3.Error as error:
            if "append-only" in str(error):
                raise RuntimeError(
                    "Jett journal prune refused by the live append-only "
                    "trigger (events_no_delete): pruning needs a C#-side "
                    "decision first; no rows were deleted and the "
                    "trigger was left in place (%s)" % error) from error
            raise
        conn.execute("VACUUM")
        return deleted if deleted is not None and deleted >= 0 else 0
    finally:
        conn.close()


def enable_debug_session(sink, reason, actor="phil"):
    """Start a debug session for one sink (E41 section 4).

    In-memory ONLY: the record lives in this process and a restart
    resets it (there is no persisted DEBUG-on anywhere). The session
    expires 1 hour after it is enabled. Returns the record dict
    (``sink``, ``reason``, ``actor``, ``expires_at``) -- the same
    record a caller writes as its ``debug.enabled`` INFO line.
    Raises ValueError on an empty sink or an actor outside ACTORS.
    """
    if not isinstance(sink, str) or not sink.strip():
        raise ValueError("debug session needs a non-empty sink name")
    if actor not in ACTORS:
        raise ValueError("unknown debug-session actor %r" % (actor,))
    expires = datetime.now(timezone.utc) + timedelta(
        seconds=_DEBUG_SESSION_TTL_SECONDS)
    record = {
        "sink": sink,
        "reason": truncate_text(scrub_text(str(reason))),
        "actor": actor,
        "expires_at": expires.isoformat(timespec="seconds").replace(
            "+00:00", "Z"),
    }
    _DEBUG_SESSIONS[sink] = record
    return dict(record)


def _session_expires_at(record):
    try:
        return datetime.fromisoformat(
            record["expires_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None


def is_debug_enabled(sink):
    """True only while a debug session for ``sink`` is set and live."""
    record = _DEBUG_SESSIONS.get(sink)
    if not isinstance(record, dict):
        return False
    expires = _session_expires_at(record)
    if expires is None or datetime.now(timezone.utc) >= expires:
        _DEBUG_SESSIONS.pop(sink, None)
        return False
    return True


def debug_session_record(sink):
    """The record for the ``debug.enabled`` INFO line, or {}.

    A copy of {``sink``, ``reason``, ``actor``, ``expires_at``} when
    a live session exists for ``sink``; {} when there is none (or it
    expired) -- callers that log the session use this, they do not
    invent a record for a session that was never enabled.
    """
    if not is_debug_enabled(sink):
        return {}
    return dict(_DEBUG_SESSIONS[sink])


def format_jsonl_line(ts, level, component, actor, outcome, msg,
                      code=None, turn_id=None, detail=None,
                      duration_ms=None, generation=None):
    """One canonical JSONL record (E41 section 2.4), keys in order.

    ``ts, level, component, actor, outcome, code, msg, detail,
    turn_id, duration_ms, generation`` -- optional fields are omitted
    when None (``turn_id`` is omitted, never invented). ``msg`` and
    ``detail`` are scrubbed and truncated to 200 chars at write time.
    ``code`` is required on WARN/ERROR: when the caller passes none,
    ``<component>.unspecified`` is used rather than emitting a
    codeless WARN/ERROR line. Raises ValueError on a level, actor,
    component or outcome outside the fixed lists. No trailing
    newline: the writer appends it.
    """
    if level not in LEVELS:
        raise ValueError("unknown log level %r" % (level,))
    if component not in COMPONENTS:
        raise ValueError("unknown log component %r" % (component,))
    if actor not in ACTORS:
        raise ValueError("unknown log actor %r" % (actor,))
    if outcome not in OUTCOMES:
        raise ValueError("unknown log outcome %r" % (outcome,))
    if code is None and level in ("WARN", "ERROR"):
        code = "%s.unspecified" % component
    record = {"ts": str(ts), "level": level, "component": component,
              "actor": actor, "outcome": outcome}
    if code is not None:
        record["code"] = str(code)
    record["msg"] = truncate_text(scrub_text(msg))
    if detail is not None:
        record["detail"] = _scrub_value(detail)
    if turn_id:
        record["turn_id"] = str(turn_id)
    if duration_ms is not None:
        record["duration_ms"] = int(duration_ms)
    if generation is not None:
        record["generation"] = int(generation)
    return json.dumps(record, ensure_ascii=True)


def format_plain_line(ts, level, component, actor, code, msg):
    """One plain-text record (E41 section 2.4).

    ``[ts] [LEVEL] [component/actor] code -- msg``. ``msg`` is
    scrubbed and truncated to 200 chars at write time. Raises
    ValueError on a level, actor or component outside the fixed
    lists. A missing code renders as ``unspecified`` so the line
    always stays greppable.
    """
    if level not in LEVELS:
        raise ValueError("unknown log level %r" % (level,))
    if component not in COMPONENTS:
        raise ValueError("unknown log component %r" % (component,))
    if actor not in ACTORS:
        raise ValueError("unknown log actor %r" % (actor,))
    text = truncate_text(scrub_text(msg))
    return "[%s] [%s] [%s/%s] %s \u2014 %s" % (
        ts, level, component, actor,
        str(code) if code else "unspecified", text)
