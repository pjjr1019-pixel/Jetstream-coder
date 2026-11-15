"""Deskhand diagnostics (100.017, E14) — health snapshot, scrubber, export.

Service-side half of the unified Health / Diagnostics view (spec
diagnostics-view-build-spec.md §2, §4, §7, §8). This module is
intentionally importable without the full service: the VM tests import
it standalone, and the service's health() / diagnostics_export() ops
are thin wrappers over build_health_snapshot() / build_export().

Rules that must not drift (spec §2.2 normative):
- Exactly the 13 component keys, each exactly
  {status, last_ok, version, error, level}; status one of
  OK / DEGRADED / DOWN / UNKNOWN / IDLE; last_ok a Unix epoch or 0;
  level one of DEBUG / INFO / WARN / ERROR (100.029, E41 §3.2/§6.3:
  the source's *configured* level, default INFO — what the source
  would log right now, not a per-line level).
- The op never fails the whole snapshot for one bad row: a throwing
  collector yields that row DOWN + exception text, the rest intact.
- No secrets, tokens, pairing material, file paths, per-turn text,
  chat content, file contents, or a `thought` field anywhere in the op.

Scrub (§8) applies to LOG-TAIL lines only. Structured sections
([health], [config], [versions]) are built from known-safe values and
are never passed through the token regex, so the export's own
[versions] hashes are never redacted. Within a log line the rules are
applied in an order that preserves each rule's specified output:
rule 4 (token query params) before rule 1 (token-shaped strings), so
`wss://host/path?token=<64-hex>` collapses to
`wss://host/path?[REDACTED]` instead of leaking the parameter name
beside a token marker; rule 2 (field names) before rule 1 for the same
reason. The spec lists the rules 1-6; the reordering is only between
rules whose outputs would otherwise collide, and every rule's
specified output is preserved (see test_diagnostics.py).
"""

import hashlib
import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract constants (spec §2.1, §5.2)
# ---------------------------------------------------------------------------

COMPONENT_KEYS = [
    "windows_app", "service", "gateway", "worker", "relay", "tunnel",
    "node", "agent", "bridge", "grant", "muse", "lifecycle", "phone",
]

# Rows owned by C# (via the `health.snapshot` HostRpc call, STEP0 §0.2).
CSHARP_KEYS = [
    "gateway", "worker", "relay", "tunnel", "node", "grant", "muse",
    "phone", "windows_app",
]

# Rows owned by this service.
SERVICE_KEYS = ["service", "bridge", "agent", "lifecycle"]

VALID_STATUSES = {"OK", "DEGRADED", "DOWN", "UNKNOWN", "IDLE"}

# Configured log levels (E41 §2.1, 100.029). The fixed vocabulary lives
# in deskhand_retention (Worker A); fall back to the same literal set
# when that module is not beside this one yet (VM runs before Worker
# A lands it). Never a fifth level: CRITICAL/VERBOSE/TRACE/FATAL are
# aliased or unused, never emitted.
try:
    from deskhand_retention import LEVELS as _RETENTION_LEVELS
    VALID_LEVELS = frozenset(_RETENTION_LEVELS)
except Exception:  # pragma: no cover - retention module not present yet
    VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARN", "ERROR"})


def _valid_level(value):
    """A configured level, or the INFO default for anything else."""
    if isinstance(value, str) and value in VALID_LEVELS:
        return value
    return "INFO"

NOT_CHECKED_YET = "not checked yet"
LIFECYCLE_LOG_MISSING = "lifecycle log not found"

# Laptop-agent probe (spec §4.3). Loopback is the path the service
# itself uses; Tailscale is the path the phone actually uses. The host
# below is the spec's example host, used in fixtures only.
AGENT_LOOPBACK_URL = "http://127.0.0.1:8899/health"
AGENT_TAILSCALE_HOST = "phillyg.tail559de7.ts.net"
AGENT_TAILSCALE_URL = "http://%s:8899/health" % AGENT_TAILSCALE_HOST
AGENT_TIMEOUT_SECS = 2

# Log files the export tail reads — per source, NOT one base dir
# (100.017 log-paths fix, STEP0 §0.5). The four sources live in three
# different directories on the live PC, so no single base can find
# them; the old single-dir mapping silently rendered every real log
# "[X log not found]". A missing file still renders a single
# "[X log not found]" line, never an error. Do not invent contents.
#   service   -> deskhand_bridge.log, beside THIS module (the service
#                runs from publish assistant_service on the PC).
#   tunnel    -> tunnel-watchdog.log + tunnel-remint.log in the PARENT
#                of the service data dir (data_dir is
#                %LOCALAPPDATA%\Deskhand\assistant, so the parent is
#                %LOCALAPPDATA%\Deskhand).
#   agent     -> agent.log in the Task-agent folder on the Desktop.
#   lifecycle -> NO log file exists; the tail is always the honest
#                "[lifecycle log not found]" line.
SERVICE_LOG_NAME = "deskhand_bridge.log"
TUNNEL_LOG_NAMES = ("tunnel-watchdog.log", "tunnel-remint.log")
AGENT_LOG_PARTS = ("Desktop", "Deskhand", "Deskhand-windows",
                   "Task-agent", "agent.log")


def default_log_paths(data_dir):
    """Per-source log paths for collect_log_tail().

    Returns {source: Path | list[Path] | None}:
      service   — one Path beside this module;
      tunnel    — a list of the two tunnel logs under the parent of
                  data_dir (both files, in order);
      agent     — one Path under the user's home directory;
      lifecycle — None (no log file exists).
    data_dir None falls back to the current directory (its parent is
    itself, so the tunnel logs resolve under ".").
    """
    base = Path(data_dir) if data_dir else Path(".")
    return {
        "service": Path(__file__).resolve().parent / SERVICE_LOG_NAME,
        "lifecycle": None,
        "agent": Path.home().joinpath(*AGENT_LOG_PARTS),
        "tunnel": [base.parent / name for name in TUNNEL_LOG_NAMES],
    }
LOG_TAIL_LINES = 50
LOG_LINE_MAX_CHARS = 200
# Per-file candidate window for level filtering (100.029, E41 §6.1):
# the tail is the last 50 lines AFTER filtering, so each file
# contributes its last 200 lines as candidates — a WARN/ERROR-only
# tail must be able to reach past a run of recent INFO lines.
LOG_TAIL_CANDIDATE_LINES = 200

# A level token anywhere in a tail line: the plain-text sinks render
# "[LEVEL] [component/actor] code — msg" (E41 §2.4), so a grep for
# "[WARN]" selects WARN lines. Lines with no such token (pre-level
# lines, unlevelled sources) count as unlevelled, never as INFO.
_LEVEL_RE = re.compile(r"\[(DEBUG|INFO|WARN|ERROR)\]")
# An ISO timestamp anywhere in a tail line (the "[source]" prefix
# precedes it, so the anchored _ISO_PREFIX_RE cannot see it). Used
# only to order the merged tail newest-last across generations.
_LINE_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def parse_line_level(line):
    """The level token of one tail line, or None when unlevelled."""
    match = _LEVEL_RE.search(line or "")
    return match.group(1) if match else None

TOKEN_FIELD_NAMES = (
    "token", "secret", "password", "api_key", "apikey", "credential",
    "private_key", "bearer",
)
CONFIG_CONTENT_FILES = ("agent_config.json", "musebridge.config.json")

_HEX_TOKEN_RE = re.compile(r"[0-9a-fA-F]{32,}")
_B64_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-+/]{32,}")
_FIELD_RE = re.compile(
    r"(?i)\b(" + "|".join(TOKEN_FIELD_NAMES) + r")\s*([=:])\s*.+")
_URL_QUERY_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>?]+)\?([^\s\"'<>]*)")
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)(token|secret|password|api_key|apikey|credential|private_key|bearer|auth|key)=")
_PATH_TOKEN_RE = re.compile(r"[^\s\"']+[/\\][^\s\"']*")
_ISO_PREFIX_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

DROPPED_MARKER = "[1 line dropped — could not scrub]"


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def make_row(status, last_ok=0, version="", error=""):
    """One §2.1 row. Invalid status degrades to UNKNOWN, never raises."""
    if status not in VALID_STATUSES:
        status = "UNKNOWN"
    try:
        last_ok = int(last_ok)
    except (TypeError, ValueError):
        last_ok = 0
    if last_ok < 0:
        last_ok = 0
    # error is one line, verbatim from the source (spec §2.2).
    error = "" if error is None else str(error).replace("\n", " ").replace("\r", " ")[:500]
    version = "" if version is None else str(version)[:120]
    return {"status": status, "last_ok": last_ok, "version": version, "error": error}


def unknown_row():
    """A row for a component that has not been checked yet (spec §2.2)."""
    return make_row("UNKNOWN", 0, "", NOT_CHECKED_YET)


def _coerce_csharp_row(raw):
    """Validate one C#-supplied row; garbage degrades to UNKNOWN."""
    if not isinstance(raw, dict):
        return unknown_row()
    status = raw.get("status")
    if status not in VALID_STATUSES:
        return unknown_row()
    return make_row(status, raw.get("last_ok", 0),
                    raw.get("version", ""), raw.get("error", ""))


def collect_csharp_rows(host_rpc):
    """Read the 9 C#-owned rows via `health.snapshot` (STEP0 §0.2).

    host_rpc None, or a call that throws / times out / returns garbage,
    yields all 9 rows UNKNOWN "not checked yet" — never an exception.
    """
    rows = {key: unknown_row() for key in CSHARP_KEYS}
    if host_rpc is None:
        return rows
    started = time.monotonic()
    try:
        result = host_rpc.call("health.snapshot", {})
    except TimeoutError as exc:
        # 100.041: a health.snapshot timeout used to be
        # swallowed silently and return UNKNOWN rows, which is why
        # the 30 s submit stall took a live timestamp test to notice.
        # Log it (one line, with the elapsed time) so the next
        # investigation can correlate directly. Other call failures
        # stay silent: they are already UNKNOWN -> allow, and logging
        # them would spam the journal on every transient disconnect.
        logger.warning("health.snapshot timed out after %.1fs (%s); "
                       "C# rows UNKNOWN",
                       time.monotonic() - started, exc)
        return rows
    except Exception:
        return rows
    if isinstance(result, dict) and isinstance(result.get("components"), dict):
        result = result["components"]
    if not isinstance(result, dict):
        return rows
    for key in CSHARP_KEYS:
        if key in result:
            rows[key] = _coerce_csharp_row(result[key])
    return rows


def file_hash8(path):
    """First 8 chars of the SHA-256 of a file; "" when unreadable."""
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, ValueError):
        return ""
    return digest[:8]


def collect_service_row(service_file_path, now=None):
    """The `service` row: alive is proven by the op itself running.

    version is the first 8 chars of the SHA-256 of the loaded
    deskhand_service.py file, hashed AT OP TIME (spec §4.1 option
    chosen here: op time, not cached at service start).
    """
    now = int(now if now is not None else time.time())
    return make_row("OK", now, file_hash8(service_file_path), "")


def collect_bridge_row(bridge_status):
    """The `bridge` row from an existing _bridge_status() result dict.

    bridge_status is {"running": bool, "pid": int|None, "health": bool}
    (deskhand_service.py _bridge_status :241, reused exactly — the
    probe itself is untouched). OK only when the loopback /health
    answers; a running-but-unhealthy bridge is DOWN, a bridge that is
    not running is DOWN, never OK by default.
    """
    status = bridge_status or {}
    if status.get("health") is True:
        return make_row("OK", int(time.time()), "", "")
    if status.get("running"):
        return make_row("DOWN", 0, "",
                        "bridge not answering /health on 127.0.0.1:8899")
    return make_row("DOWN", 0, "", "bridge not running")


def _fetch_agent_health(url, timeout, opener=None):
    """One GET of an agent /health URL. Returns (ok, data, error_text).

    ok is True only for HTTP 200 with JSON {"ok": true}. opener, when
    given, is a fake fetch used by the VM tests: opener(url, timeout)
    returns a parsed JSON dict, or raises.
    """
    try:
        if opener is not None:
            data = opener(url, timeout)
        else:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if response.status != 200:
                    return False, {}, "HTTP %s" % response.status
                data = json.loads(response.read().decode())
    except Exception as exc:  # TimeoutError, URLError, OSError, ValueError
        return False, {}, str(exc) or exc.__class__.__name__
    if not isinstance(data, dict) or data.get("ok") is not True:
        return False, data if isinstance(data, dict) else {}, "ok=false"
    return True, data, ""


def agent_probe(loopback_url=AGENT_LOOPBACK_URL,
                tailscale_url=AGENT_TAILSCALE_URL,
                timeout=AGENT_TIMEOUT_SECS, opener=None):
    """Probe the laptop agent on loopback AND Tailscale (spec §4.3).

    Returns {"running", "server", "requests_total", "error"}.
    status is the worst of the two paths and the error names which
    path failed — a loopback-only OK is never reported as OK when the
    Tailscale path (the path the phone actually uses) is dead.
    """
    loop_ok, loop_data, loop_err = _fetch_agent_health(
        loopback_url, timeout, opener)
    tail_ok, _tail_data, tail_err = _fetch_agent_health(
        tailscale_url, timeout, opener)
    server = str(loop_data.get("server", "") or "")[:120]
    try:
        requests_total = int(loop_data.get("requests_total", 0))
    except (TypeError, ValueError):
        requests_total = 0
    if loop_ok and tail_ok:
        return {"running": True, "server": server,
                "requests_total": requests_total, "error": ""}
    parts = []
    if loop_ok:
        parts.append("loopback OK")
    else:
        parts.append("loopback %s" % (loop_err or "failed"))
    if tail_ok:
        parts.append("Tailscale OK")
    else:
        parts.append("Tailscale %s" % (tail_err or "failed"))
    return {"running": False, "server": server,
            "requests_total": requests_total,
            "error": "; ".join(parts)}


def collect_agent_row(probe=None):
    """The `agent` row from an agent_probe() result (or a fresh probe)."""
    result = probe if probe is not None else agent_probe()
    if result.get("running"):
        version = result.get("server", "")
        total = result.get("requests_total", 0)
        if version:
            version = "%s, requests_total %s" % (version, total)
        return make_row("OK", int(time.time()), version, "")
    return make_row("DOWN", 0, "", result.get("error", "agent not answering"))


# --- lifecycle row (spec §4.4) -------------------------------------------
# Live module facts (repo-snapshot deskhand_task_agent_lifecycle.py,
# verified, not assumed):
# - log file: <data_dir>/task_agent_lifecycle.log (:200); state file:
#   <data_dir>/task_agent_lifecycle.json (:219-227, status() dict).
# - every state transition logs "lifecycle state -> <state> ..." (:232).
# - grant-off is STATE_REFUSED_GATE = "refused_gate" (:117, :414, :549);
#   the monitor sets it when the grant is off (:543-547, :586-589).
# - "stopped" is set only by ensure_stopped() (:485): a deliberate stop.
#
# Mapping (task §4.4 + spec §4.4):
#   running            -> OK
#   degraded_flag_off  -> DEGRADED (healthy, remote_control_enabled off)
#   refused_gate       -> IDLE, error "state -> stopped (grant off)"
#   refused_stopped    -> IDLE, error "state -> stopped (service stopping)"
#                          (fail-closed stop; spec §4.4: never DOWN for a
#                          fail-closed stop)
#   starting/restarting-> DEGRADED, error "state -> <state>"
#   stopped / refused_config / refused_flag / failed_start /
#   restart_exhausted  -> DOWN, error = last log line
#   missing/unreadable log -> UNKNOWN, error "lifecycle log not found"

_LIFECYCLE_IDLE = {"refused_gate": "state -> stopped (grant off)"}
_LIFECYCLE_FAIL_CLOSED_IDLE = {
    "refused_stopped": "state -> stopped (service stopping)",
}


def _parse_lifecycle_state_line(line):
    """Return the state word from a "lifecycle state -> X" line, else None."""
    marker = "lifecycle state -> "
    idx = line.find(marker)
    if idx < 0:
        return None
    rest = line[idx + len(marker):].strip()
    return rest.split()[0].strip(":,") if rest else None


def _lifecycle_last_ok(lines):
    """Epoch of the last log line showing a healthy state, else 0."""
    last_ok = 0
    for line in lines:
        state = _parse_lifecycle_state_line(line)
        if state in ("running", "degraded_flag_off"):
            match = _ISO_PREFIX_RE.match(line)
            if match:
                try:
                    # Lifecycle log timestamps are UTC ISO (module
                    # _now_iso uses datetime.now(timezone.utc)); a bare
                    # "YYYY-MM-DDTHH:MM:SS" prefix is UTC, not local.
                    last_ok = int(datetime.fromisoformat(
                        match.group(1)).replace(
                            tzinfo=timezone.utc).timestamp())
                except ValueError:
                    pass
    return last_ok


def collect_lifecycle_row(lifecycle=None, data_dir=None,
                          lifecycle_module_path=None):
    """The `lifecycle` row from the lifecycle log + state.

    lifecycle: the TaskAgentLifecycle instance (status() gives the
    live state) or None. data_dir: the service data dir holding
    task_agent_lifecycle.log. lifecycle_module_path: the module file
    (hashed at op time, first 8 chars, into `version`).
    """
    version = file_hash8(lifecycle_module_path) if lifecycle_module_path else ""
    log_path = Path(data_dir) / "task_agent_lifecycle.log" if data_dir else None
    if log_path is None or not log_path.exists():
        return make_row("UNKNOWN", 0, version, LIFECYCLE_LOG_MISSING)
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return make_row("UNKNOWN", 0, version, LIFECYCLE_LOG_MISSING)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return make_row("UNKNOWN", 0, version, LIFECYCLE_LOG_MISSING)
    last_line = lines[-1]
    state = None
    if lifecycle is not None:
        try:
            state = lifecycle.status().get("state")
        except Exception:
            state = None
    if not state:
        for line in reversed(lines):
            state = _parse_lifecycle_state_line(line)
            if state:
                break
    last_ok = _lifecycle_last_ok(lines)
    if state == "running":
        return make_row("OK", last_ok, version, "")
    if state == "degraded_flag_off":
        return make_row("DEGRADED", last_ok, version,
                        "remote_control_enabled=false")
    if state in _LIFECYCLE_IDLE:
        return make_row("IDLE", last_ok, version, _LIFECYCLE_IDLE[state])
    if state in _LIFECYCLE_FAIL_CLOSED_IDLE:
        return make_row("IDLE", last_ok, version,
                        _LIFECYCLE_FAIL_CLOSED_IDLE[state])
    if state in ("starting", "restarting"):
        return make_row("DEGRADED", last_ok, version,
                        "state -> %s" % state)
    # stopped / refused_config / refused_flag / failed_start /
    # restart_exhausted / unknown state word: DOWN with the last log
    # line as the error. Never IDLE for a crash; never DOWN for a
    # fail-closed stop (handled above).
    return make_row("DOWN", last_ok, version, last_line)


# ---------------------------------------------------------------------------
# Snapshot assembly (spec §4.5)
# ---------------------------------------------------------------------------

def _safe_collect(fn, key):
    """Run one row collector; a throw yields that row DOWN + text."""
    try:
        row = fn()
    except Exception as exc:  # noqa: BLE001 — one bad row must not kill the op
        return make_row("DOWN", 0, "",
                        "%s collector failed: %s" % (key, exc))
    if not isinstance(row, dict) or row.get("status") not in VALID_STATUSES:
        return make_row("DOWN", 0, "",
                        "%s collector returned an invalid row" % key)
    return make_row(row.get("status"), row.get("last_ok", 0),
                    row.get("version", ""), row.get("error", ""))


def build_health_snapshot(host_rpc=None, service_file_path=None,
                          bridge_status=None, agent_probe_result=None,
                          lifecycle=None, data_dir=None,
                          lifecycle_module_path=None,
                          bridge_status_fn=None, agent_probe_fn=None,
                          lifecycle_row_fn=None, csharp_rows_fn=None,
                          last_ok_store=None, now=None, levels=None):
    """Assemble the §2.1 snapshot. Never raises for a bad row.

    Callables (bridge_status_fn / agent_probe_fn / lifecycle_row_fn /
    csharp_rows_fn) override the default collectors; they exist so the
    op-never-fails test can force each collector to throw in turn.
    last_ok_store is a caller-owned dict {key: epoch} of the last time
    each service-owned row was observed OK; it survives across polls
    within one service run so a row that flips OK -> DOWN keeps its
    real last-good time instead of "never". C# rows carry their own
    last_ok from the snapshot and are never overwritten here.
    levels (100.029, E41 §6.3) is an optional {component: level} of
    each source's *configured* log level; a missing or invalid entry
    defaults to INFO, so a snapshot built without levels is all-INFO.
    Returns {"ok", "as_of", "as_of_human", "components"}.
    """
    now_epoch = int(now if now is not None else time.time())
    store = last_ok_store if isinstance(last_ok_store, dict) else {}
    components = {}

    try:
        fetcher = csharp_rows_fn if csharp_rows_fn is not None else (
            lambda: collect_csharp_rows(host_rpc))
        csharp = fetcher()
    except Exception:
        csharp = None
    if isinstance(csharp, dict) and set(csharp) == set(CSHARP_KEYS):
        components.update(csharp)
    else:
        # Fetcher broke, or returned something that is not the 9-row
        # dict: C# rows are UNKNOWN, service rows still real.
        components.update({key: unknown_row() for key in CSHARP_KEYS})

    def _service_row():
        row = collect_service_row(service_file_path, now=now_epoch)
        if row["status"] == "OK":
            store["service"] = now_epoch
        return row

    def _bridge_row():
        status = (bridge_status_fn() if bridge_status_fn is not None
                  else bridge_status)
        row = collect_bridge_row(status)
        if row["status"] == "OK":
            store["bridge"] = now_epoch
        elif "bridge" in store:
            row["last_ok"] = store["bridge"]
        return row

    def _agent_row():
        probe = (agent_probe_fn() if agent_probe_fn is not None
                 else agent_probe_result)
        row = collect_agent_row(probe)
        if row["status"] == "OK":
            store["agent"] = now_epoch
        elif "agent" in store:
            row["last_ok"] = store["agent"]
        return row

    def _lifecycle_row():
        if lifecycle_row_fn is not None:
            return lifecycle_row_fn()
        return collect_lifecycle_row(lifecycle, data_dir,
                                     lifecycle_module_path)

    components["service"] = _safe_collect(_service_row, "service")
    components["bridge"] = _safe_collect(_bridge_row, "bridge")
    components["agent"] = _safe_collect(_agent_row, "agent")
    components["lifecycle"] = _safe_collect(_lifecycle_row, "lifecycle")

    # Exactly the 13 keys, in §5.2 order. Nothing else is ever emitted.
    ordered = {key: components.get(key, unknown_row())
               for key in COMPONENT_KEYS}
    # 100.029 (E41 §6.3): every row carries its source's configured
    # level. A level is a configured-level claim, not a per-line one;
    # unknown sources default to INFO (§4: default level is INFO).
    level_map = levels if isinstance(levels, dict) else {}
    for key in COMPONENT_KEYS:
        ordered[key]["level"] = _valid_level(level_map.get(key))
    as_of_human = datetime.fromtimestamp(now_epoch).strftime(
        "%Y-%m-%d %H:%M:%S %Z")
    return {"ok": True, "as_of": now_epoch, "as_of_human": as_of_human,
            "components": ordered}


# ---------------------------------------------------------------------------
# Scrubber (spec §8)
# ---------------------------------------------------------------------------

def scrub_line(line):
    """Scrub one log-tail line. Returns (cleaned_or_None, redactions, dropped).

    - Returns (None, 0, 1) for a line that cannot be scrubbed
      confidently (control characters): the caller emits the dropped
      marker and counts it.
    - Otherwise returns the cleaned line and the number of individual
      replacements (rules 1-5; rule 6 is the drop).
    Structured sections are never passed through here.
    """
    if _CONTROL_RE.search(line):
        return None, 0, 1
    redactions = 0
    work = line
    # Rule 4 — token query params: keep the URL up to ?, query -> [REDACTED].
    # Applied before rule 1 so a token-bearing query collapses to one
    # marker instead of leaking the parameter name.
    def _query(match):
        nonlocal redactions
        query = match.group(2)
        if _SENSITIVE_QUERY_RE.search(query):
            redactions += 1
            return match.group(1) + "?[REDACTED]"
        return match.group(0)
    work = _URL_QUERY_RE.sub(_query, work)
    # Rule 3 — config-file contents are never included: cut at the path.
    for name in CONFIG_CONTENT_FILES:
        idx = work.find(name)
        if idx >= 0:
            work = work[:idx + len(name)]
            redactions += 1
            break
    # Rule 5 — file-content echoes (fs.read): cut at the path.
    if "fs.read" in work.lower():
        marker = work.lower().find("fs.read")
        tail = work[marker:]
        match = _PATH_TOKEN_RE.search(tail)
        if match:
            work = work[:marker + match.end()]
            redactions += 1
        else:
            return None, 0, 1
    # Rule 2 — credential field values -> [REDACTED] (value to end-of-line).
    def _field(match):
        nonlocal redactions
        redactions += 1
        return "%s%s[REDACTED]" % (match.group(1), match.group(2))
    work = _FIELD_RE.sub(_field, work)
    # Rule 1 — token-shaped values -> [REDACTED-TOKEN] (log lines only).
    def _token(match):
        nonlocal redactions
        redactions += 1
        return "[REDACTED-TOKEN]"
    work = _HEX_TOKEN_RE.sub(_token, work)
    work = _B64_TOKEN_RE.sub(_token, work)
    return work, redactions, 0


def scrub_log_lines(lines):
    """Scrub a list of log lines. Returns (cleaned_lines, redactions, dropped)."""
    cleaned, redactions, dropped = [], 0, 0
    for line in lines:
        out, count, was_dropped = scrub_line(line)
        if was_dropped:
            cleaned.append(DROPPED_MARKER)
            dropped += 1
        else:
            cleaned.append(out)
            redactions += count
    return cleaned, redactions, dropped


# ---------------------------------------------------------------------------
# Export builder (spec §7.1)
# ---------------------------------------------------------------------------

def export_filename(now=None):
    """deskhand-diagnostics-YYYYMMDD-HHMMSS.txt (local time)."""
    stamp = datetime.fromtimestamp(
        now if now is not None else time.time()).strftime("%Y%m%d-%H%M%S")
    return "deskhand-diagnostics-%s.txt" % stamp


def _format_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ss" % seconds
    if seconds < 3600:
        return "%sm" % (seconds // 60)
    return "%sh %sm" % (seconds // 3600, (seconds % 3600) // 60)


def _format_ts(epoch, full=False):
    moment = datetime.fromtimestamp(epoch)
    if full:
        return moment.strftime("%Y-%m-%d %H:%M:%S %Z")
    return moment.strftime("%H:%M:%S %Z")


def _read_log_tail(path, source, limit=LOG_TAIL_LINES):
    """Last lines of one log file, prefixed. Missing file -> not-found line."""
    if not path.exists():
        return ["[%s log not found]" % source]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["[%s log not found]" % source]
    lines = [line for line in text.splitlines() if line.strip()]
    return ["[%s] %s" % (source, line) for line in lines[-limit:]]


def _parse_line_ts(line):
    """Epoch from a leading [ISO] timestamp, else None (sort helper)."""
    match = _ISO_PREFIX_RE.search(line)
    if not match:
        return None
    try:
        return int(datetime.fromisoformat(match.group(1)).replace(
            tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _parse_line_ts_anywhere(line):
    """Epoch from an [ISO] timestamp anywhere in a tail line, else None.

    The tagged tail lines start with "[source] ", so the anchored
    _parse_line_ts cannot see their timestamps; this is the tail's
    sort helper, and only the tail's.
    """
    match = _LINE_TS_RE.search(line or "")
    if not match:
        return None
    try:
        return int(datetime.fromisoformat(match.group(1)).replace(
            tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _rotated_generation(path):
    """The previous generation of a log file (``<name>.1``), or None.

    Rotation renames, never truncates (E17 §3.1): after a rotation
    the newest history sits in ``<name>.1``. The tail reads across
    the current file + this one generation, so a crash investigation
    keeps its context across the rotation boundary.
    """
    candidate = Path(path).with_name(Path(path).name + ".1")
    return candidate if candidate.exists() else None


def collect_log_tail_with_counts(data_dir, log_files=None,
                                 level_filter="all", include_debug=False):
    """Combined last 50 lines, newest last, across the four Deskhand logs.

    log_files overrides the default per-source mapping (tests): each
    value is a Path, a list of Paths (tunnel has two files), or None
    (no log file exists for that source — an honest not-found line).
    Untimestamped lines keep their file order and sort oldest. A
    source with several files contributes the lines of every file
    that exists; only when NONE of its files exists does it render a
    single "[X log not found]" line. Each file also contributes its
    previous generation (``<name>.1``) when one exists, so the tail
    still spans the rotation boundary.

    level_filter (100.029, E41 §6.1):
      "all"       — last 50 lines at INFO and above, DEBUG excluded
                    (the default export); unlevelled lines are kept.
      "warn_error" — last 50 WARN and ERROR lines only, newest last;
                    INFO, DEBUG and unlevelled lines are dropped.
    include_debug — Windows-only toggle: when True, DEBUG lines are
    kept in the "all" tail (the Android share never passes it, and
    an explicit platform of "android" is ignored by the caller).
    Unknown level_filter raises ValueError.

    Returns (lines, counts): counts is {"ERROR", "WARN", "INFO",
    "DEBUG", "debug_excluded", "unlevelled"} over the returned
    lines, where "debug_excluded" is the number of DEBUG lines the
    filter dropped from the candidate window and "unlevelled" is
    the number of returned lines with no level token.
    """
    if level_filter not in ("all", "warn_error"):
        raise ValueError("unknown level_filter %r" % (level_filter,))
    paths = log_files if log_files is not None else default_log_paths(data_dir)
    tagged = []
    for source in ("service", "lifecycle", "agent", "tunnel"):
        value = paths.get(source)
        if value is None:
            tagged.append((None, "[%s log not found]" % source))
            continue
        files = value if isinstance(value, (list, tuple)) else [value]
        if not any(Path(path).exists() for path in files):
            tagged.append((None, "[%s log not found]" % source))
            continue
        for path in files:
            if not Path(path).exists():
                continue  # a missing sibling must not leak its own
            # "[X log not found]" line into the tail (the one line
            # above covers the source when NONE of its files exist)
            # Previous generation first: its lines are older, and
            # the stable sort below keeps them before the current
            # file's lines when neither carries a timestamp.
            generation = _rotated_generation(path)
            if generation is not None:
                for line in _read_log_tail(generation, source,
                                           limit=LOG_TAIL_CANDIDATE_LINES):
                    tagged.append((_parse_line_ts_anywhere(line), line))
            for line in _read_log_tail(Path(path), source,
                                       limit=LOG_TAIL_CANDIDATE_LINES):
                tagged.append((_parse_line_ts_anywhere(line), line))
    tagged.sort(key=lambda item: (item[0] is not None, item[0] or 0))
    lines = [line for _, line in tagged]
    counts = {"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0,
              "debug_excluded": 0, "unlevelled": 0}
    kept = []
    debug_dropped_at = []
    for pos, line in enumerate(lines):
        if line.startswith("[") and line.endswith("log not found]"):
            kept.append(line)
            continue
        level = parse_line_level(line)
        if level == "DEBUG" and not include_debug:
            debug_dropped_at.append(pos)
            continue
        if level_filter == "warn_error" and level not in ("WARN", "ERROR"):
            continue
        kept.append(line)
    kept = kept[-LOG_TAIL_LINES:]
    # "DEBUG excluded" counts only the DEBUG lines that competed for
    # this window — a DEBUG line older than the window's oldest shown
    # line was never in this export to begin with, so it is not
    # "excluded" from it.
    if kept:
        try:
            window_start = lines.index(kept[0])
        except ValueError:
            window_start = 0
        counts["debug_excluded"] = sum(
            1 for pos in debug_dropped_at if pos >= window_start)
    for line in kept:
        if line.startswith("[") and line.endswith("log not found]"):
            continue
        level = parse_line_level(line)
        if level is None:
            counts["unlevelled"] += 1
        else:
            counts[level] += 1
    return kept, counts


def collect_log_tail(data_dir, log_files=None,
                     level_filter="all", include_debug=False):
    """Combined last 50 lines, newest last, across the four Deskhand logs.

    See collect_log_tail_with_counts for the level filtering; this
    wrapper returns only the lines, as before.
    """
    lines, _counts = collect_log_tail_with_counts(
        data_dir, log_files=log_files, level_filter=level_filter,
        include_debug=include_debug)
    return lines


def _config_value(value):
    """One [config] value: secret-shaped -> <redacted>, else verbatim."""
    if value is None:
        return "<not reported>"
    text = str(value)
    if _HEX_TOKEN_RE.search(text) or _B64_TOKEN_RE.search(text):
        return "<redacted>"
    if _FIELD_RE.search(text):
        return "<redacted>"
    return text


def build_export(snapshot, data_dir=None, log_files=None,
                 config=None, windows_version="", android_version="",
                 service_dir=None, lifecycle_module_path=None, now=None,
                 level_filter="all", include_debug=False):
    """Build the §7.1 export text. Returns (filename, text).

    snapshot is a build_health_snapshot() result. Structured sections
    are built from known-safe values and are NOT passed through the
    token regex; only the log tail goes through scrub_line().
    level_filter / include_debug (100.029, E41 §6.1): "all" is the
    last 50 lines at INFO and above, DEBUG excluded; "warn_error"
    is the last 50 WARN and ERROR lines only, newest last;
    include_debug (Windows only) keeps DEBUG lines in the tail.
    The footer gains the level counts of the exported tail, plus
    the DEBUG-excluded count when debug lines were filtered out,
    so the exclusion is visible, not silent. The bridge log is NOT
    added to the bundle: it is unstructured and unscrubbed at the
    writer, so it is pulled deliberately, never swept in.
    """
    now_epoch = int(now if now is not None else time.time())
    components = snapshot.get("components", {})
    as_of = snapshot.get("as_of", now_epoch)
    stamp_full = _format_ts(now_epoch, full=True)

    service_version = components.get("service", {}).get("version", "")
    lifecycle_version = components.get("lifecycle", {}).get("version", "")

    header_parts = ["Deskhand diagnostics — %s" % stamp_full]
    second = []
    if windows_version:
        second.append("Windows %s" % windows_version)
    if android_version:
        second.append("Android %s" % android_version)
    if service_version:
        second.append("Service %s" % service_version)
    if lifecycle_version:
        second.append("Lifecycle %s" % lifecycle_version)
    if second:
        header_parts.append(" · ".join(second))
    age = now_epoch - int(as_of)
    if age <= 6:
        header_parts.append("Snapshot age: %s (fresh)" % _format_age(age))
    else:
        header_parts.append("STALE — no update for %s" % _format_age(age))

    out = list(header_parts)
    out.append("")
    out.append("[health]")
    first_full = True
    for key in COMPONENT_KEYS:
        row = components.get(key) or unknown_row()
        last_ok = row.get("last_ok", 0)
        if last_ok:
            ts = _format_ts(last_ok, full=first_full)
            first_full = False
            last_text = "last OK %s" % ts
        else:
            last_text = "last OK never"
        parts = ["%s: %s — %s" % (key, row.get("status", "UNKNOWN"), last_text)]
        if row.get("version"):
            parts.append(row["version"])
        if row.get("error"):
            parts.append(row["error"])
        out.append(" — ".join(parts))
    out.append("")
    out.append("[approvals]")
    out.append("no approvals recorded")
    out.append("")
    out.append("[config]")
    cfg = config or {}
    relay_url = cfg.get("relay_url")
    if relay_url is not None and isinstance(relay_url, str):
        relay_url = _URL_QUERY_RE.sub(
            lambda m: (m.group(1) + "?[REDACTED]"
                       if _SENSITIVE_QUERY_RE.search(m.group(2))
                       else m.group(0)), relay_url)
    out.append("paused: %s · start_with_windows: %s · embedded_network: %s" % (
        _config_value(cfg.get("paused")),
        _config_value(cfg.get("start_with_windows")),
        _config_value(cfg.get("embedded_network"))))
    out.append("relay_port: %s · relay_url: %s · run_built_in_relay: %s" % (
        _config_value(cfg.get("relay_port")),
        _config_value(relay_url),
        _config_value(cfg.get("run_built_in_relay"))))
    out.append("devices_paired: %s" % _config_value(cfg.get("devices_paired")))
    out.append("")
    if level_filter == "warn_error":
        out.append("[log tail — last 50 WARN/ERROR lines, newest last]")
    else:
        out.append("[log tail — last 50 lines, newest last]")
    raw_tail, level_counts = collect_log_tail_with_counts(
        data_dir, log_files=log_files, level_filter=level_filter,
        include_debug=include_debug)
    cleaned, redactions, dropped = scrub_log_lines(raw_tail)
    for line in cleaned:
        if len(line) > LOG_LINE_MAX_CHARS:
            line = line[:LOG_LINE_MAX_CHARS] + "… [truncated]"
        out.append(line)
    out.append("")
    out.append("[versions]")
    versions = []
    if windows_version:
        versions.append("windows: %s (MainForm.cs label)" % windows_version)
    if android_version:
        versions.append("android: %s" % android_version)
    service_hashes = []
    if service_dir:
        sdir = Path(service_dir)
        for name in ("deskhand_service.py", "deskhand_codex.py",
                     "deskhand_cloud_tools.py"):
            hashed = file_hash8(sdir / name)
            service_hashes.append("%s %s" % (
                name, hashed if hashed else "<not found>"))
    if service_hashes:
        versions.append("service: " + " · ".join(service_hashes))
    if lifecycle_module_path:
        hashed = file_hash8(lifecycle_module_path)
        versions.append("lifecycle: deskhand_task_agent_lifecycle.py %s" % (
            hashed if hashed else "<not found>"))
    out.extend(versions if versions else ["service: <not reported>"])
    out.append("")
    out.append("[scrub]")
    out.append("scrubbed: %d redactions, %d lines dropped"
               % (redactions, dropped))
    # 100.029 (E41 §6.1): the level shape of the exported tail, so
    # the reader sees the failure before reading a single line. The
    # DEBUG-excluded count is shown whenever DEBUG filtering was
    # active (even 0), so the exclusion is visible, not silent; when
    # DEBUG was included, its count is shown as a level, not as an
    # exclusion. Unlevelled lines are named when present, never
    # silently folded into INFO.
    levels_line = ("levels: %d ERROR, %d WARN, %d INFO"
                   % (level_counts["ERROR"], level_counts["WARN"],
                      level_counts["INFO"]))
    if include_debug:
        if level_counts["DEBUG"]:
            levels_line += ", %d DEBUG" % level_counts["DEBUG"]
    else:
        levels_line += ", %d DEBUG excluded" % level_counts["debug_excluded"]
    if level_counts["unlevelled"]:
        levels_line += ", %d unlevelled" % level_counts["unlevelled"]
    out.append(levels_line)
    out.append("")
    return export_filename(now_epoch), "\n".join(out)
