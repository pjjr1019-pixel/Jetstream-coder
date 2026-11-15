"""Built-in verify-this-build step for Deskhand (100.048, vault B36).

One op, ``verify_build``, runs the Gate 4 checklist inside the service
(on the PC, where the files, the builds, and the gateway live) and
returns one structured pass/fail report. It is a verification-only op:
it never publishes and never relaunches -- the ``publish`` and
``relaunch`` checks are dry-run reports describing what would run.

The module orchestrates the three pieces already in place and
re-implements none of them: 100.045's Runner runs the build/test
commands, 100.046's Searcher finds the projects to check, and
100.047's Transfer carries the byte-exactness probe. Hashing is
read-only; no check detail ever carries file content or full command
output (tails are bounded to MAX_TAIL_LINES lines); events carry
metadata only.

Safety (BUILD-SPEC-100048 SS1.6):
  * STOP gate: ``verify()`` refuses while chat/agent work is stopped,
    checked once at op entry after arg validation, before any check.
  * This module starts no processes of its own and enables no
    command shell anywhere: commands run only through the injected
    Runner, as argv lists.
  * The overall timeout is enforced by polling ``runner.status``;
    on expiry the running command is cancelled, the current check
    fails, the rest skip, and the op still returns a report.
  * The transfer probe is random bytes per call, written through
    the Transfer, re-hashed, and deleted in a ``finally`` -- it
    never survives the check.

Stdlib only: base64, datetime, hashlib, logging, os, pathlib, re,
sys, time, urllib.error, urllib.request, uuid. (``base64`` is needed
for the probe payload the Transfer takes; it is stdlib, like the
rest. The pytest command runs under ``sys.executable`` -- the
interpreter attribute, named so by the spec; this module never
invokes that builtin.)
"""

import base64
import hashlib
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["Verifier", "get_verifier"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Caps (BUILD-SPEC-100048 SS1.3, pinned).
# ---------------------------------------------------------------------------

VERSION = "100.048"
DEFAULT_TIMEOUT_S = 3600
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 86400
MAX_FILES = 64
MAX_TAIL_LINES = 20
MAX_GATEWAY_TIMEOUT_S = 10
PROBE_BYTES = 4096
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8899/status"
POLL_INTERVAL_S = 0.5

CHECK_ORDER = ["hash", "backup", "transfer", "rehash", "build", "specs",
               "pytest", "gradle", "publish", "relaunch", "gateway"]
_COMMAND_CHECKS = ("build", "specs", "pytest", "gradle")

_READ_BLOCK = 64 * 1024
_ERROR_LIMIT = 300

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SPECS_COUNTS_RE = re.compile(r"Passed:\s*(\d+),\s*Failed:\s*(\d+)")
_PASSED_RE = re.compile(r"(\d+) passed")
_FAILED_RE = re.compile(r"(\d+) failed")
_COMPLETED_RE = re.compile(r"(\d+) tests? completed")


def _utc_now_iso():
    """UTC timestamp in the service's shape (isoformat, seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path):
    """sha256 hex of the exact bytes of the file at ``path`` (64 KiB reads)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(_READ_BLOCK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _one_line(text, limit=_ERROR_LIMIT):
    """Collapse an error to one short line (no tracebacks in replies)."""
    if not isinstance(text, str):
        text = str(text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return text


def _default_http_get(url, timeout_s):
    """GET ``url`` with ``timeout_s``; return ``(status_code, body)``.

    A non-200 HTTP response is returned as its status code (the
    gateway check then fails with that code); transport failures
    raise, and the gateway check turns them into a fail detail.
    """
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""


# ---------------------------------------------------------------------------
# Validation (SS1.1: arg errors raise ValueError at op entry, before the
# STOP gate, so the caller learns its call was malformed).
# ---------------------------------------------------------------------------

def _validate_files(files):
    if not isinstance(files, list) or not files:
        raise ValueError(
            "files must be a non-empty list of {path, sha256} (max %d)"
            % MAX_FILES)
    if len(files) > MAX_FILES:
        raise ValueError("files must contain at most %d entries" % MAX_FILES)
    validated = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError(
                "each files entry must be a dict with path and sha256")
        path = entry.get("path")
        sha256 = entry.get("sha256")
        if not isinstance(path, str) or not path or not os.path.isabs(path):
            raise ValueError(
                "file path must be an absolute path string: %r" % (path,))
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            raise ValueError("sha256 must be 64 lowercase hex chars")
        validated.append({"path": path, "sha256": sha256})
    return validated


def _validate_checks(checks):
    if checks is None:
        return list(CHECK_ORDER)
    if not isinstance(checks, list) or not checks:
        raise ValueError("checks must be a non-empty list of check names")
    for name in checks:
        if name not in CHECK_ORDER:
            raise ValueError("unknown verify check: %r" % (name,))
    # Pinned order regardless of the order given; duplicates collapse.
    return [name for name in CHECK_ORDER if name in checks]


def _validate_abs_path(value, label):
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise ValueError(
            "%s must be an absolute path string: %r" % (label, value))
    return Path(value)


def _validate_timeout(timeout_s):
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int):
        raise ValueError("timeout_s must be an int in %d..%d"
                         % (MIN_TIMEOUT_S, MAX_TIMEOUT_S))
    if not MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S:
        raise ValueError("timeout_s must be in %d..%d"
                         % (MIN_TIMEOUT_S, MAX_TIMEOUT_S))
    return timeout_s


def _validate_backup_files(backup_files):
    if backup_files is None:
        return []
    if not isinstance(backup_files, list):
        raise ValueError("backup_files must be a list of relative path strings")
    for rel in backup_files:
        if not isinstance(rel, str) or not rel or os.path.isabs(rel):
            raise ValueError(
                "backup_files entries must be relative path strings: %r"
                % (rel,))
    return list(backup_files)


def _validate_argv(argv):
    if not isinstance(argv, list):
        raise ValueError("argv must be a list of non-empty strings")
    if not argv:
        raise ValueError("argv must not be empty")
    for item in argv:
        if not isinstance(item, str) or not item:
            raise ValueError("argv must be a list of non-empty strings")
    return list(argv)


def _validate_commands(commands):
    if commands is None:
        return {}
    if not isinstance(commands, dict):
        raise ValueError("commands must be a dict of check name -> argv list")
    validated = {}
    for name, argv in commands.items():
        if name not in _COMMAND_CHECKS:
            raise ValueError("unknown command check: %r" % (name,))
        validated[name] = _validate_argv(argv)
    return validated


def _validate_non_empty_string(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty string" % label)
    return value


# ---------------------------------------------------------------------------
# Check-detail shapes (SS1.1/SS1.2).
# ---------------------------------------------------------------------------

def _command_detail(exit_code, duration_ms, stdout_tail, stderr_tail,
                    counts, reason=None, notes=None):
    """The pinned command-check detail (SS1.1)."""
    detail = {
        "exit_code": exit_code,
        "duration_ms": int(duration_ms or 0),
        "stdout_tail": list(stdout_tail)[-MAX_TAIL_LINES:],
        "stderr_tail": list(stderr_tail)[-MAX_TAIL_LINES:],
        "counts": counts,
    }
    if reason is not None:
        detail["reason"] = reason
    if notes is not None:
        detail["notes"] = notes
    return detail


def _parse_counts(name, stdout_tail, stderr_tail):
    """Parse pass/fail counts from the command tail, per SS1.2.

    Returns ``{"passed": n, "failed": m}`` or None when the tail
    carries no parseable counts. ``build`` has no count format, so
    it always returns None.
    """
    text = "\n".join(list(stdout_tail) + list(stderr_tail))
    if name == "specs":
        match = _SPECS_COUNTS_RE.search(text)
        if match is None:
            return None
        return {"passed": int(match.group(1)), "failed": int(match.group(2))}
    if name == "pytest":
        passed = _PASSED_RE.search(text)
        failed = _FAILED_RE.search(text)
        if passed is None and failed is None:
            return None
        return {"passed": int(passed.group(1)) if passed else 0,
                "failed": int(failed.group(1)) if failed else 0}
    if name == "gradle":
        failed = _FAILED_RE.search(text)
        total = _COMPLETED_RE.search(text)
        if failed is None and total is None:
            return None
        failed_n = int(failed.group(1)) if failed else 0
        total_n = int(total.group(1)) if total else None
        passed_n = (total_n - failed_n) if total_n is not None else 0
        return {"passed": passed_n, "failed": failed_n}
    return None


def _snapshot_duration(snapshot):
    if not isinstance(snapshot, dict):
        return 0
    duration = snapshot.get("duration_ms")
    return duration if isinstance(duration, int) else 0


def _snapshot_tail(snapshot, key):
    if not isinstance(snapshot, dict):
        return []
    tail = snapshot.get(key)
    if not isinstance(tail, list):
        return []
    lines = [line if isinstance(line, str) else str(line) for line in tail]
    return lines[-MAX_TAIL_LINES:]


# ---------------------------------------------------------------------------
# One run's validated args + deadline.
# ---------------------------------------------------------------------------

class _Run:
    def __init__(self, files, checks, verify_root, backup_dir, backup_files,
                 timeout_s, publish_dir, version, gateway_url, commands,
                 deadline):
        self.files = files
        self.checks = checks
        self.verify_root = verify_root
        self.backup_dir = backup_dir
        self.backup_files = backup_files
        self.timeout_s = timeout_s
        self.publish_dir = publish_dir
        self.version = version
        self.gateway_url = gateway_url
        self.commands = commands
        self.deadline = deadline
        self.timed_out = False


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

class Verifier:
    """Runs the Gate 4 checklist and returns one structured report.

    Constructed as ``Verifier(emit_event=None, data_dir=None,
    runner=None, transfer=None, searcher=None, is_stopped=None,
    http_get=None)``; the service wires the real collaborators via
    ``get_verifier`` (below). Direct use (tests, drivers) passes the
    same pieces explicitly. ``is_stopped=None`` means the gate is
    open. ``http_get`` defaults to a loopback urllib GET.
    """

    def __init__(self, emit_event=None, data_dir=None, runner=None,
                 transfer=None, searcher=None, is_stopped=None,
                 http_get=None):
        self.emit_event = emit_event
        if data_dir is None:
            data_dir = Path(__file__).resolve().parent
        self.data_dir = data_dir
        self.runner = runner
        self.transfer = transfer
        self.searcher = searcher
        self.is_stopped = is_stopped
        self.http_get = http_get if http_get is not None else _default_http_get

    # ------------------------------------------------------------------
    # Public API (SS1.3)
    # ------------------------------------------------------------------

    def verify(self, files, checks=None, verify_root=None, backup_dir=None,
               backup_files=None, timeout_s=DEFAULT_TIMEOUT_S,
               publish_dir=None, version=VERSION,
               gateway_url=DEFAULT_GATEWAY_URL, commands=None):
        """Run the Gate 4 checklist; return the verify_build report (SS1.1)."""
        started_at = _utc_now_iso()
        started_mono = time.monotonic()

        # Arg validation happens at op entry, before the STOP gate.
        files_v = _validate_files(files)
        selected = _validate_checks(checks)
        verify_root_p = (_validate_abs_path(verify_root, "verify_root")
                         if verify_root is not None else None)
        backup_dir_p = (_validate_abs_path(backup_dir, "backup_dir")
                        if backup_dir is not None else None)
        backup_files_v = _validate_backup_files(backup_files)
        timeout_v = _validate_timeout(timeout_s)
        publish_dir_p = (_validate_abs_path(publish_dir, "publish_dir")
                         if publish_dir is not None else None)
        version_v = _validate_non_empty_string(version, "version")
        gateway_v = _validate_non_empty_string(gateway_url, "gateway_url")
        commands_v = _validate_commands(commands)

        # STOP gate (SS1.6): checked once at entry, after validation.
        if self.is_stopped is not None and self.is_stopped():
            raise RuntimeError("Verifier stopped: chat/agent work is stopped")

        run = _Run(files=files_v, checks=selected, verify_root=verify_root_p,
                   backup_dir=backup_dir_p, backup_files=backup_files_v,
                   timeout_s=timeout_v, publish_dir=publish_dir_p,
                   version=version_v, gateway_url=gateway_v,
                   commands=commands_v,
                   deadline=started_mono + timeout_v)

        results = []
        for name in selected:
            check_started = time.monotonic()
            if run.timed_out:
                status, detail = "skip", {"reason": "verify timeout"}
            elif time.monotonic() >= run.deadline:
                run.timed_out = True
                status, detail = "skip", {"reason": "verify timeout"}
            else:
                try:
                    status, detail = self._run_check(run, name)
                except Exception as exc:
                    # A check that fails is a fail result, never a crash.
                    status = "fail"
                    detail = {"error": _one_line(str(exc)
                                                 or exc.__class__.__name__)}
            duration_ms = int((time.monotonic() - check_started) * 1000)
            result = {"name": name, "status": status,
                      "duration_ms": duration_ms, "detail": detail}
            results.append(result)
            self._emit({"type": "verify_progress", "check": name,
                        "status": status, "duration_ms": duration_ms})

        finished_at = _utc_now_iso()
        total_ms = int((time.monotonic() - started_mono) * 1000)
        passed = sum(1 for r in results if r["status"] == "pass")
        failed = sum(1 for r in results if r["status"] == "fail")
        skipped = sum(1 for r in results if r["status"] == "skip")
        report = {
            "ok": failed == 0,
            "verdict": "pass" if failed == 0 else "fail",
            "version": version_v,
            "op": "verify_build",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": total_ms,
            "checks_run": [r["name"] for r in results],
            "checks": results,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        }
        self._emit({"type": "verify_done", "verdict": report["verdict"],
                    "passed": passed, "failed": failed, "skipped": skipped,
                    "duration_ms": total_ms})
        return report

    def dispatch(self, op, command):
        """One dispatch entry point for the service handle() branch (SS1.3).

        ``command`` is the op's args dict. Routes ``verify_build`` to
        ``verify()`` and returns the report dict. Unknown op raises
        ValueError; missing args raise ValueError, never a bare
        KeyError (the search-module precedent).
        """
        if not isinstance(command, dict):
            raise ValueError("command must be a dict of args")
        if op == "verify_build":
            timeout_s = command.get("timeout_s", DEFAULT_TIMEOUT_S)
            if timeout_s is None:
                timeout_s = DEFAULT_TIMEOUT_S
            version = command.get("version", VERSION)
            if version is None:
                version = VERSION
            gateway_url = command.get("gateway_url", DEFAULT_GATEWAY_URL)
            if gateway_url is None:
                gateway_url = DEFAULT_GATEWAY_URL
            return self.verify(
                files=command.get("files"),
                checks=command.get("checks"),
                verify_root=command.get("verify_root"),
                backup_dir=command.get("backup_dir"),
                backup_files=command.get("backup_files"),
                timeout_s=timeout_s,
                publish_dir=command.get("publish_dir"),
                version=version,
                gateway_url=gateway_url,
                commands=command.get("commands"),
            )
        raise ValueError("unknown verify op: %r" % (op,))

    # ------------------------------------------------------------------
    # Check fan-out
    # ------------------------------------------------------------------

    def _run_check(self, run, name):
        if name in ("hash", "rehash"):
            return self._check_hash(run.files)
        if name == "backup":
            return self._check_backup(run)
        if name == "transfer":
            return self._check_transfer(run)
        if name in _COMMAND_CHECKS:
            return self._check_command(run, name)
        if name == "publish":
            return self._check_publish(run)
        if name == "relaunch":
            return self._check_relaunch(run)
        if name == "gateway":
            return self._check_gateway(run)
        raise ValueError("unknown verify check: %r" % (name,))

    def _emit(self, entry):
        """Emit one vm-event; a failing sink never propagates (SS1.7).

        Events carry metadata only -- no file bytes, no command
        output, no hashes (E41 scrub rule).
        """
        if self.emit_event is None:
            return
        try:
            self.emit_event(entry)
        except Exception:
            logger.warning("verify event sink failed; continuing",
                           exc_info=True)

    # ------------------------------------------------------------------
    # Checks 1 + 4: hash / rehash (read-only, same helper).
    # ------------------------------------------------------------------

    def _check_hash(self, files):
        mismatched = []
        missing = []
        for entry in files:
            path = Path(entry["path"])
            if not path.is_file():
                missing.append(entry["path"])
                continue
            try:
                actual = _sha256_file(path)
            except OSError:
                missing.append(entry["path"])
                continue
            if actual != entry["sha256"]:
                mismatched.append({"path": entry["path"],
                                   "expected": entry["sha256"],
                                   "actual": actual})
        detail = {"checked": len(files),
                  "mismatched": mismatched, "missing": missing}
        status = "pass" if not mismatched and not missing else "fail"
        return status, detail

    # ------------------------------------------------------------------
    # Check 2: backup.
    # ------------------------------------------------------------------

    def _check_backup(self, run):
        if run.backup_dir is None:
            return "skip", {"reason": "no backup_dir"}
        root = run.backup_dir
        exists = root.is_dir()
        file_count = 0
        if exists:
            try:
                file_count = sum(1 for _ in root.iterdir())
            except OSError:
                file_count = 0
        missing = [rel for rel in run.backup_files
                   if not (root / rel).exists()]
        detail = {"exists": exists, "file_count": file_count,
                  "missing": missing}
        status = ("pass" if exists and file_count > 0 and not missing
                  else "fail")
        return status, detail

    # ------------------------------------------------------------------
    # Check 3: transfer probe (SS1.5).
    # ------------------------------------------------------------------

    def _check_transfer(self, run):
        if self.transfer is None:
            return "fail", {"error": "transfer unavailable"}
        raw = os.urandom(PROBE_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        name = "verify-probe-" + uuid.uuid4().hex[:12] + ".bin"
        probe_path = Path(self.data_dir) / name
        data_b64 = base64.b64encode(raw).decode("ascii")
        deleted = False
        outcome = None
        try:
            result = self.transfer.put(str(probe_path), data_b64, digest,
                                       True)
            if not isinstance(result, dict) or not result.get("ok"):
                error = (result.get("error") if isinstance(result, dict)
                         else None) or "transfer put failed"
                outcome = ("fail", {"error": _one_line(error)})
            else:
                verified = self.transfer.verify(str(probe_path), digest)
                match = (verified.get("match") if isinstance(verified, dict)
                         else False)
                if not match:
                    outcome = ("fail", {"error": "probe re-hash mismatch"})
                else:
                    outcome = ("pass", {"probe": name,
                                        "bytes": PROBE_BYTES})
        except Exception as exc:
            outcome = ("fail", {"error": _one_line(str(exc))})
        finally:
            # Best-effort delete: the probe must not survive the check,
            # even on failure.
            try:
                os.remove(probe_path)
                deleted = True
            except OSError:
                deleted = False
        status, detail = outcome
        if status == "pass":
            detail = dict(detail)
            detail["deleted"] = deleted
        return status, detail

    # ------------------------------------------------------------------
    # Checks 5-8: build / specs / pytest / gradle, via the Runner (SS1.4).
    # ------------------------------------------------------------------

    def _first_search_match(self, run, pattern):
        """First sorted search_files match as an absolute Path.

        Returns ``(path_or_None, error_or_None)``. A relative result
        path is resolved against ``verify_root`` (SS1.2).
        """
        if self.searcher is None:
            return None, "searcher unavailable"
        try:
            result = self.searcher.search_files(pattern,
                                                root=str(run.verify_root))
        except Exception as exc:
            return None, _one_line(str(exc))
        files = result.get("files") if isinstance(result, dict) else None
        if not files:
            return None, None
        entry = sorted(str(item) for item in files)[0]
        path = Path(entry)
        if not path.is_absolute():
            path = Path(run.verify_root) / path
        return path, None

    def _check_command(self, run, name):
        """Resolve the command for ``name`` and run it (SS1.2/SS1.4)."""
        if run.verify_root is None:
            return "skip", {"reason": "no verify_root"}
        override = run.commands.get(name)
        if override is not None:
            return self._run_command(run, name, override, run.verify_root)
        if name == "build":
            target, error = self._first_search_match(
                run, "Deskhand.Windows.App.csproj")
            if error is not None:
                return "fail", _command_detail(None, 0, [], [], None,
                                               reason=error)
            if target is None:
                return "fail", _command_detail(
                    None, 0, [], [], None,
                    reason="csproj not found under verify_root")
            return self._run_command(
                run, name,
                ["dotnet", "build", str(target), "-c", "Release"],
                run.verify_root)
        if name == "specs":
            target, error = self._first_search_match(run, "*Specs*.csproj")
            if error is not None:
                return "fail", _command_detail(None, 0, [], [], None,
                                               reason=error)
            if target is None:
                return "fail", _command_detail(
                    None, 0, [], [], None,
                    reason="no Specs project found under verify_root")
            return self._run_command(
                run, name,
                ["dotnet", "test", str(target), "-c", "Release"],
                run.verify_root)
        if name == "pytest":
            tests_dir = Path(run.verify_root) / "tests"
            if not tests_dir.is_dir():
                target, error = self._first_search_match(run, "test_*.py")
                if error is not None:
                    return "fail", _command_detail(None, 0, [], [], None,
                                                   reason=error)
                if target is None:
                    return "fail", _command_detail(
                        None, 0, [], [], None,
                        reason="no tests dir found under verify_root")
                tests_dir = target.parent
            return self._run_command(
                run, name,
                [sys.executable, "-m", "pytest", str(tests_dir), "-q"],
                run.verify_root)
        if name == "gradle":
            target, error = self._first_search_match(run, "gradlew.bat")
            if error is not None:
                return "fail", _command_detail(None, 0, [], [], None,
                                               reason=error)
            if target is None:
                return "fail", _command_detail(
                    None, 0, [], [], None,
                    reason="gradlew.bat not found under verify_root")
            return self._run_command(
                run, name, ["gradlew.bat", "testDebugUnitTest"],
                target.parent)
        raise ValueError("unknown verify check: %r" % (name,))

    def _run_command(self, run, name, argv, cwd):
        """Run one command through the Runner to completion (SS1.4).

        Polls ``runner.status`` every POLL_INTERVAL_S. If the overall
        deadline passes while the command is still running, cancels
        it, fails this check with reason ``"verify timeout"``, and
        marks the run timed out so the remaining checks skip.
        """
        if self.runner is None:
            return "fail", _command_detail(None, 0, [], [], None,
                                           reason="runner unavailable")
        remaining = run.deadline - time.monotonic()
        if remaining <= 0:
            run.timed_out = True
            return "fail", _command_detail(None, 0, [], [], None,
                                           reason="verify timeout")
        per_command = int(min(remaining, 3600))
        per_command = max(MIN_TIMEOUT_S, min(per_command, MAX_TIMEOUT_S))
        try:
            started = self.runner.start(
                argv, str(cwd), timeout_s=per_command,
                label="verify:" + name)
        except Exception as exc:
            return "fail", _command_detail(None, 0, [], [], None,
                                           reason=_one_line(str(exc)))
        run_id = started.get("run_id") if isinstance(started, dict) else None
        if not run_id:
            return "fail", _command_detail(
                None, 0, [], [], None,
                reason="runner start returned no run_id")
        snapshot = None
        while True:
            try:
                snapshot = self.runner.status(run_id)
            except Exception as exc:
                return "fail", _command_detail(None, 0, [], [], None,
                                               reason=_one_line(str(exc)))
            if not isinstance(snapshot, dict) \
                    or snapshot.get("state") != "running":
                break
            if time.monotonic() >= run.deadline:
                try:
                    final = self.runner.cancel(run_id)
                    if isinstance(final, dict):
                        snapshot = final
                except Exception:
                    pass
                run.timed_out = True
                return "fail", _command_detail(
                    None, _snapshot_duration(snapshot),
                    _snapshot_tail(snapshot, "stdout_tail"),
                    _snapshot_tail(snapshot, "stderr_tail"), None,
                    reason="verify timeout")
            time.sleep(POLL_INTERVAL_S)
        if not isinstance(snapshot, dict):
            return "fail", _command_detail(
                None, 0, [], [], None, reason="runner status malformed")
        exit_code = snapshot.get("exit_code")
        stdout_tail = _snapshot_tail(snapshot, "stdout_tail")
        stderr_tail = _snapshot_tail(snapshot, "stderr_tail")
        counts = _parse_counts(name, stdout_tail, stderr_tail)
        notes = None
        if counts is None and name in ("specs", "pytest", "gradle"):
            notes = "counts unparsed"
        status = "pass" if exit_code == 0 else "fail"
        return status, _command_detail(
            exit_code, _snapshot_duration(snapshot), stdout_tail,
            stderr_tail, counts, notes=notes)

    # ------------------------------------------------------------------
    # Check 9: publish -- DRY-RUN REPORT ONLY (SS1.6).
    #
    # This check never calls runner.start. There is no arg, flag, or
    # mode that makes this module publish. It only describes what
    # would run and checks the precondition (csproj found).
    # ------------------------------------------------------------------

    def _check_publish(self, run):
        if run.publish_dir is None:
            return "skip", {"reason": "no publish_dir"}
        would_template = ("dotnet publish %s -c Release -r win-x64 "
                          "--self-contained true -p:PublishSingleFile=true "
                          "-o %s")
        if run.verify_root is None:
            return "fail", {"dry_run": True,
                            "would": would_template % ("<csproj>",
                                                      str(run.publish_dir)),
                            "error": "csproj not found under verify_root"}
        target, error = self._first_search_match(
            run, "Deskhand.Windows.App.csproj")
        if error is not None:
            return "fail", {"dry_run": True,
                            "would": would_template % ("<csproj>",
                                                      str(run.publish_dir)),
                            "error": error}
        if target is None:
            return "fail", {"dry_run": True,
                            "would": would_template % ("<csproj>",
                                                      str(run.publish_dir)),
                            "error": "csproj not found under verify_root"}
        return "pass", {"dry_run": True,
                        "would": would_template % (str(target),
                                                   str(run.publish_dir))}

    # ------------------------------------------------------------------
    # Check 10: relaunch -- DRY-RUN REPORT ONLY (SS1.6).
    #
    # This check never calls runner.start. It only checks the
    # precondition (Deskhand.exe under publish_dir) and names the
    # steps that would run. It never stops or starts any process.
    # ------------------------------------------------------------------

    def _check_relaunch(self, run):
        if run.publish_dir is None:
            return "skip", {"reason": "no publish_dir"}
        exe = Path(run.publish_dir) / "Deskhand.exe"
        found = exe.is_file()
        detail = {"dry_run": True,
                  "would": ["stop all Deskhand procs",
                            "verify publish exe ProductVersion",
                            "relaunch plain",
                            "check process list"],
                  "exe_found": found}
        return ("pass" if found else "fail"), detail

    # ------------------------------------------------------------------
    # Check 11: gateway.
    # ------------------------------------------------------------------

    def _check_gateway(self, run):
        try:
            status_code, _body = self.http_get(
                run.gateway_url, MAX_GATEWAY_TIMEOUT_S)
        except Exception as exc:
            return "fail", {"status_code": None,
                            "error": _one_line(str(exc))}
        if status_code == 200:
            return "pass", {"status_code": 200}
        return "fail", {"status_code": status_code,
                        "error": "gateway returned HTTP %s" % (status_code,)}


# ---------------------------------------------------------------------------
# Service accessor (SS1.3): one lazily-created Verifier per service.
# ---------------------------------------------------------------------------

def get_verifier(service):
    """Return the service's single Verifier, creating it on first use.

    Wired to ``service._append_vm_event`` and ``service.data``, with
    the STOP gate wired to the dispatcher state (the gate lives in
    the module, SS1.6). The gate is fail-closed: if the dispatcher
    state cannot be read at all, verification is refused rather than
    allowed on an unknown state. The service branch does a lazy
    ``from deskhand_verify import get_verifier`` so a broken verify
    module cannot break service startup.
    """
    verifier = getattr(service, "_verifier", None)
    if verifier is None:
        from deskhand_runner import get_runner
        from deskhand_search import get_searcher
        from deskhand_transfer import get_transfer

        def _service_stopped():
            try:
                return bool(service.dispatcher.admission_state()["stopped"])
            except Exception:
                return True

        verifier = Verifier(
            emit_event=getattr(service, "_append_vm_event", None),
            data_dir=getattr(service, "data", None),
            runner=get_runner(service),
            transfer=get_transfer(service),
            searcher=get_searcher(service),
            is_stopped=_service_stopped,
        )
        service._verifier = verifier
    return verifier
