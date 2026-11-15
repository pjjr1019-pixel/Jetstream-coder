"""Long-running command runner for Deskhand (100.045, vault B33).

Runs a command as long as it needs, streams each output line back as a
vm-event (source ``"runner"``) via the emit callable handed in at
construction, keeps a per-run log file, and reports a structured result
(exit code, stdout/stderr tails, duration).

Ownership: the Runner never blocks the calling thread except for spawning
the child. Reader threads and a reaper thread own the child; the service
stdin loop is never waited on (the 100.041 health() self-deadlock lesson).

Safety (BUILD-SPEC-100045 SS1.5):
  * argv must be a list of non-empty strings; no shell anywhere — this
    file never enables a shell in Popen (grep-verifiable) and passes argv
    straight to ``subprocess.Popen``.
  * cwd must be an absolute path that exists.
  * env may only ADD variables (merged over the inherited environment);
    keys must match ``[A-Za-z_][A-Za-z0-9_]*``; values must be strings.
  * timeout_s is an int in 1..86400.
  * At most 4 runs concurrently; a 5th start raises ValueError.
  * Per-line event text is truncated to 2000 chars; the per-run log is
    capped at 5 MB (rewritten keeping the last 2 MB, flagged
    ``log_truncated``); status tails are the last 50 lines per stream.
  * Cancel / timeout kill the process TREE, not just the parent.
  * Output text goes in ``detail.text`` only — never in ``summary`` — so
    no token-shaped value can reach a vm-event summary (E41 scrub rule).

Stdlib only: subprocess, threading, uuid, pathlib, time, logging, re, os,
signal, sys, datetime, collections.
"""

import logging
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["Runner", "get_runner"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Caps (BUILD-SPEC SS1.5 rule 5/6, SS1.2).
# ---------------------------------------------------------------------------

MAX_CONCURRENT_RUNS = 4
MAX_COMPLETED_RUNS = 20
COMPLETED_MAX_AGE_S = 24 * 3600

MAX_EVENT_TEXT_CHARS = 2000
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_TAIL_KEEP_BYTES = 2 * 1024 * 1024
TAIL_LINES = 50

MAX_TIMEOUT_S = 86400
MIN_TIMEOUT_S = 1
DEFAULT_TIMEOUT_S = 3600
MAX_LABEL_CHARS = 80

READER_DRAIN_TIMEOUT_S = 5.0
CANCEL_FINALIZE_TIMEOUT_S = 5.0
TASKKILL_TIMEOUT_S = 10

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _utc_now_iso():
    """UTC timestamp in the service's shape (isoformat, seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Validation (SS1.5 rules 1-4).
# ---------------------------------------------------------------------------

def _validate_argv(argv):
    if not isinstance(argv, list):
        raise ValueError("argv must be a list of non-empty strings")
    if not argv:
        raise ValueError("argv must not be empty")
    for item in argv:
        if not isinstance(item, str) or not item:
            raise ValueError("argv must be a list of non-empty strings")
    return list(argv)


def _validate_cwd(cwd):
    if cwd is None:
        raise ValueError("cwd is required and must be an absolute path that exists")
    path = Path(cwd)
    if not path.is_absolute():
        raise ValueError("cwd must be an absolute path: %r" % (cwd,))
    if not path.is_dir():
        raise ValueError("cwd does not exist or is not a directory: %r" % (cwd,))
    return path


def _validate_env(env):
    """Merge env additions over the inherited environment (SS1.5 rule 3).

    No full-env replacement: the child always inherits os.environ, with
    only the validated additions on top.
    """
    merged = dict(os.environ)
    if env is None:
        return merged
    if not isinstance(env, dict):
        raise ValueError("env must be a dict of str -> str")
    for key, value in env.items():
        if not isinstance(key, str) or not _ENV_KEY_RE.match(key):
            raise ValueError("invalid env key: %r" % (key,))
        if not isinstance(value, str):
            raise ValueError("env value for %r must be a string" % (key,))
        merged[key] = value
    return merged


def _validate_timeout(timeout_s):
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int):
        raise ValueError("timeout_s must be an int in %d..%d"
                         % (MIN_TIMEOUT_S, MAX_TIMEOUT_S))
    if not MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S:
        raise ValueError("timeout_s must be in %d..%d"
                         % (MIN_TIMEOUT_S, MAX_TIMEOUT_S))
    return timeout_s


def _validate_label(label, argv):
    if label is None:
        # Default: basename of argv[0] (SS1.1).
        return Path(argv[0]).name or argv[0]
    if not isinstance(label, str):
        raise ValueError("label must be a string")
    if len(label) > MAX_LABEL_CHARS:
        raise ValueError("label must be at most %d chars" % MAX_LABEL_CHARS)
    return label


# ---------------------------------------------------------------------------
# Process-tree kill (SS1.5 rule 8).
# ---------------------------------------------------------------------------

def _close_proc_handle(proc):
    """Close the Popen's Windows process handle, if any (no-op on POSIX).

    On Windows, Popen.wait()/poll() only set returncode (via
    WaitForSingleObject / GetExitCodeProcess) — they never close
    proc._handle, and the Handle is only closed when the Popen object
    is garbage-collected (Handle.__del__ = Close). The runner keeps
    record["proc"] in its registry, so without this the kernel process
    object survives and os.kill(pid, 0) keeps reporting the dead child
    as alive. Closing the handle is safe once returncode is set:
    poll()/wait() never touch _handle again, and Handle.Close() is
    idempotent via its closed flag — so the closed Handle is left in
    place and record["proc"] introspection still works. Never raises.
    """
    if os.name != "nt":
        return
    try:
        handle = getattr(proc, "_handle", None)
        if handle is None:
            return
        close = getattr(handle, "Close", None)
        if callable(close):
            close()
            return
        import _winapi  # lazy: Windows only
        _winapi.CloseHandle(handle)
    except Exception:
        logger.warning("failed to close process handle for pid %s",
                       getattr(proc, "pid", None), exc_info=True)


def _kill_tree(proc):
    """Kill the whole process tree rooted at ``proc``.

    Windows: taskkill /PID <pid> /T /F, falling back to proc.kill() if
    taskkill fails. POSIX: SIGKILL to the child's own process group —
    the child is spawned with start_new_session=True, so the kill reaches
    the tree — falling back to proc.kill().
    """
    if proc.poll() is not None:
        return
    pid = proc.pid
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=TASKKILL_TIMEOUT_S, check=False)
            if result.returncode == 0:
                return
            logger.warning("taskkill for pid %d returned %d; "
                           "falling back to proc.kill()",
                           pid, result.returncode)
        except Exception:
            logger.warning("taskkill for pid %d failed; "
                           "falling back to proc.kill()", pid, exc_info=True)
        try:
            proc.kill()
        except Exception:
            logger.warning("proc.kill() for pid %d failed", pid, exc_info=True)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        logger.warning("killpg for pid %d failed; falling back to proc.kill()",
                       pid, exc_info=True)
        try:
            proc.kill()
        except Exception:
            logger.warning("proc.kill() for pid %d failed", pid, exc_info=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class Runner:
    """Owns long-running child processes and streams their output.

    Constructed as ``Runner(emit_event, data_dir)``; the service wires
    ``emit_event=self._append_vm_event`` and ``data_dir=self.data``.
    """

    def __init__(self, emit_event, data_dir):
        self._emit_event = emit_event
        self._data_dir = Path(data_dir)
        self._registry = {}          # run_id -> record (dict)
        self._registry_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, argv, cwd, env=None, timeout_s=DEFAULT_TIMEOUT_S,
              label=None):
        """Spawn a run and return the run_start record immediately.

        Returns ``{run_id, state: "running", started_at, pid}``. Never
        blocks on the child beyond spawning it.
        """
        argv = _validate_argv(argv)
        cwd_path = _validate_cwd(cwd)
        merged_env = _validate_env(env)
        timeout_s = _validate_timeout(timeout_s)
        label = _validate_label(label, argv)

        run_id = "run-" + uuid.uuid4().hex[:12]
        started_mono = time.monotonic()
        started_at = _utc_now_iso()
        log_path = self._data_dir / "runner" / ("%s.log" % run_id)

        record = {
            "run_id": run_id,
            "argv": argv,
            "cwd": str(cwd_path),
            "label": label,
            "started_at": started_at,
            "started_mono": started_mono,
            "timeout_s": timeout_s,
            "pid": None,
            "proc": None,
            "state": "running",
            "exit_code": None,
            "duration_ms": None,
            "timed_out": False,
            "cancelled": False,
            "log_truncated": False,
            "log_path": log_path,
            "log_size": 0,
            "stdout_lines": 0,
            "stderr_lines": 0,
            "stdout_tail": deque(maxlen=TAIL_LINES),
            "stderr_tail": deque(maxlen=TAIL_LINES),
            "seq": 0,
            "lock": threading.Lock(),
            "done_event": threading.Event(),
            "finalized": False,
        }

        with self._registry_lock:
            running = sum(1 for r in self._registry.values()
                          if r["state"] == "running")
            if running >= MAX_CONCURRENT_RUNS:
                raise ValueError("too many concurrent runs")
            self._registry[run_id] = record
            self._prune_locked()

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd_path),
                env=merged_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                               if os.name == "nt" else 0),
                start_new_session=(os.name != "nt"),
            )
        except Exception:
            # Spawn failed: the record never became a run.
            with self._registry_lock:
                self._registry.pop(run_id, None)
            raise

        record["proc"] = proc
        record["pid"] = proc.pid

        stdout_thread = threading.Thread(
            target=self._reader, args=(record, proc.stdout, "stdout"),
            name="runner-%s-out" % run_id, daemon=True)
        stderr_thread = threading.Thread(
            target=self._reader, args=(record, proc.stderr, "stderr"),
            name="runner-%s-err" % run_id, daemon=True)
        record["reader_threads"] = (stdout_thread, stderr_thread)
        reaper_thread = threading.Thread(
            target=self._reaper, args=(record,),
            name="runner-%s-reaper" % run_id, daemon=True)
        record["reaper_thread"] = reaper_thread

        stdout_thread.start()
        stderr_thread.start()
        reaper_thread.start()

        return {
            "run_id": run_id,
            "state": "running",
            "started_at": started_at,
            "pid": proc.pid,
        }

    def status(self, run_id):
        """The run_status record for ``run_id`` (SS1.1 shape)."""
        with self._registry_lock:
            record = self._registry.get(run_id)
        if record is None:
            raise ValueError("unknown run_id: %r" % (run_id,))
        return self._snapshot(record)

    def cancel(self, run_id):
        """Kill the run's process tree; return its final run_status record.

        Cancelling a finished run is a no-op returning its final status.
        Unknown run_id raises ValueError.
        """
        with self._registry_lock:
            record = self._registry.get(run_id)
        if record is None:
            raise ValueError("unknown run_id: %r" % (run_id,))

        with record["lock"]:
            already_done = record["state"] != "running"
            if not already_done:
                record["cancelled"] = True
        if already_done:
            return self._snapshot(record)

        proc = record["proc"]
        if proc is not None:
            _kill_tree(proc)
        # The reaper notices the death, drains the readers, and finalizes.
        # Wait for it, bounded, so the returned record is the final state.
        record["done_event"].wait(timeout=CANCEL_FINALIZE_TIMEOUT_S)
        return self._snapshot(record)

    def dispatch(self, op, command):
        """One dispatch entry point for the service handle() branch (SS1.3).

        ``command`` is the op's args dict. Routes to start / status /
        cancel and returns the record dict.
        """
        if op == "run_start":
            timeout_s = command.get("timeout_s", DEFAULT_TIMEOUT_S)
            if timeout_s is None:
                timeout_s = DEFAULT_TIMEOUT_S
            return self.start(
                command.get("argv"),
                command.get("cwd"),
                env=command.get("env"),
                timeout_s=timeout_s,
                label=command.get("label"),
            )
        if op == "run_status":
            return self.status(command["run_id"])
        if op == "run_cancel":
            return self.cancel(command["run_id"])
        raise ValueError("unknown runner op: %r" % (op,))

    # ------------------------------------------------------------------
    # Snapshot / pruning
    # ------------------------------------------------------------------

    def _snapshot(self, record):
        """Public record shape (SS1.1) — a copy, no internal fields."""
        with record["lock"]:
            duration_ms = record["duration_ms"]
            if duration_ms is None:
                duration_ms = int((time.monotonic()
                                   - record["started_mono"]) * 1000)
            return {
                "run_id": record["run_id"],
                "state": record["state"],
                "argv": list(record["argv"]),
                "cwd": record["cwd"],
                "label": record["label"],
                "started_at": record["started_at"],
                "pid": record["pid"],
                "exit_code": record["exit_code"],
                "duration_ms": duration_ms,
                "stdout_tail": list(record["stdout_tail"]),
                "stderr_tail": list(record["stderr_tail"]),
                "stdout_lines": record["stdout_lines"],
                "stderr_lines": record["stderr_lines"],
                "timed_out": record["timed_out"],
                "cancelled": record["cancelled"],
                "log_truncated": record["log_truncated"],
            }

    def _prune_locked(self):
        """Drop completed runs: keep the newest 20, drop those older than
        24 h (SS1.2). A pruned run's status raises ValueError; its log
        file is the surviving record. Caller holds _registry_lock."""
        now_mono = time.monotonic()
        completed = [r for r in self._registry.values()
                     if r["state"] != "running"]
        if not completed:
            return
        completed.sort(key=lambda r: r["started_mono"])
        stale = [r for r in completed
                 if now_mono - r["started_mono"] > COMPLETED_MAX_AGE_S]
        excess = completed[:max(0, len(completed) - MAX_COMPLETED_RUNS)]
        for record in stale + excess:
            self._registry.pop(record["run_id"], None)

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def _emit(self, entry):
        """Emit one vm-event; a failing sink never propagates (SS1.2).

        The run still completes and run_status still works — the feed is
        the view, the run is the work.
        """
        try:
            self._emit_event(entry)
        except Exception:
            logger.warning("runner event sink failed; continuing",
                           exc_info=True)

    def _reader(self, record, stream, stream_name):
        """One reader thread per stream: each line becomes one event and
        one log-file line (SS1.2)."""
        prefix = "OUT " if stream_name == "stdout" else "ERR "
        try:
            for raw_line in stream:
                line = raw_line.rstrip("\r\n")
                with record["lock"]:
                    record["seq"] += 1
                    seq = record["seq"]
                    if stream_name == "stdout":
                        record["stdout_lines"] += 1
                        record["stdout_tail"].append(line)
                    else:
                        record["stderr_lines"] += 1
                        record["stderr_tail"].append(line)
                self._append_log(record, prefix + line + "\n")
                # Line text goes in detail.text ONLY, never in summary
                # (SS1.5 rule 9 / E41).
                self._emit({
                    "source": "runner",
                    "type": "runner_output",
                    "level": "INFO",
                    "summary": "run %s output" % record["run_id"],
                    "detail": {
                        "run_id": record["run_id"],
                        "seq": seq,
                        "stream": stream_name,
                        "text": line[:MAX_EVENT_TEXT_CHARS],
                    },
                })
        except Exception:
            logger.warning("runner reader failed for %s %s",
                           record["run_id"], stream_name, exc_info=True)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _reaper(self, record):
        """Wait for the child; enforce the timeout; finalize once."""
        proc = record["proc"]
        deadline = record["started_mono"] + record["timeout_s"]
        exit_code = None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with record["lock"]:
                        was_cancelled = record["cancelled"]
                    if not was_cancelled:
                        record["timed_out"] = True
                        _kill_tree(proc)
                    try:
                        exit_code = proc.wait(
                            timeout=READER_DRAIN_TIMEOUT_S)
                    except subprocess.TimeoutExpired:
                        exit_code = None
                    break
                try:
                    exit_code = proc.wait(timeout=remaining)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except Exception:
            logger.warning("runner reaper failed for %s",
                           record["run_id"], exc_info=True)
            try:
                _kill_tree(proc)
            except Exception:
                pass
            try:
                exit_code = proc.wait(timeout=READER_DRAIN_TIMEOUT_S)
            except Exception:
                exit_code = None

        # Close the Windows process handle now that returncode is set:
        # otherwise the kernel process object survives (the registry
        # keeps the Popen alive, so Handle.__del__ never runs) and
        # os.kill(pid, 0) keeps reporting the dead child as alive.
        # Deliberately NOT done in _kill_tree: the reaper may be blocked
        # in WaitForSingleObject on this handle, and closing it from
        # another thread is undefined behaviour on Windows.
        _close_proc_handle(proc)

        # Drain the readers so tails / line counts are final, bounded.
        for thread in record.get("reader_threads", ()):
            thread.join(timeout=READER_DRAIN_TIMEOUT_S / 2)

        with record["lock"]:
            if record["finalized"]:
                return
            record["finalized"] = True
            if record["cancelled"]:
                record["state"] = "cancelled"
            elif record["timed_out"]:
                record["state"] = "timed_out"
            else:
                record["state"] = "done"
            # Cancelled / timed-out runs report no exit code (SS1.1/SS1.2).
            record["exit_code"] = (None if record["state"] != "done"
                                   else exit_code)
            record["duration_ms"] = int(
                (time.monotonic() - record["started_mono"]) * 1000)

        # Prune now that this run is complete.
        with self._registry_lock:
            self._prune_locked()

        # ONE runner_done event, on process exit for any cause (SS1.2).
        if record["state"] == "done":
            summary = "run %s finished (exit %s)" % (
                record["run_id"], record["exit_code"])
        elif record["state"] == "cancelled":
            summary = "run %s finished (cancelled)" % record["run_id"]
        else:
            summary = "run %s finished (timed out)" % record["run_id"]
        self._emit({
            "source": "runner",
            "type": "runner_done",
            "level": "INFO",
            "summary": summary,
            "detail": {
                "run_id": record["run_id"],
                "exit_code": record["exit_code"],
                "duration_ms": record["duration_ms"],
                "stdout_lines": record["stdout_lines"],
                "stderr_lines": record["stderr_lines"],
                "timed_out": record["timed_out"],
                "cancelled": record["cancelled"],
            },
        })
        record["done_event"].set()

    # ------------------------------------------------------------------
    # Log file (SS1.2): data_dir / "runner" / "<run_id>.log", cap 5 MB.
    # ------------------------------------------------------------------

    def _append_log(self, record, text):
        """Append one prefixed line; on exceeding 5 MB rewrite keeping
        the last 2 MB and set log_truncated. Never raises into a reader."""
        try:
            path = record["log_path"]
            data = text.encode("utf-8", errors="replace")
            with record["lock"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "ab") as handle:
                    handle.write(data)
                record["log_size"] += len(data)
                if record["log_size"] <= MAX_LOG_BYTES:
                    return
                # Over the cap: keep the last 2 MB, flag it.
                try:
                    with open(path, "rb") as handle:
                        handle.seek(max(0, record["log_size"]
                                        - LOG_TAIL_KEEP_BYTES))
                        tail = handle.read()
                    with open(path, "wb") as handle:
                        handle.write(tail)
                    record["log_size"] = len(tail)
                except OSError:
                    logger.warning("runner log rewrite failed for %s",
                                   record["run_id"], exc_info=True)
                    return
                record["log_truncated"] = True
        except Exception:
            logger.warning("runner log append failed for %s",
                           record["run_id"], exc_info=True)


# ---------------------------------------------------------------------------
# Service accessor (SS1.3): one lazily-created Runner per service.
# ---------------------------------------------------------------------------

def get_runner(service):
    """Return the service's single Runner, creating it on first use.

    Wired to ``service._append_vm_event`` and ``service.data``. The
    service branch does a lazy ``from deskhand_runner import get_runner``
    so a broken runner module cannot break service startup.
    """
    runner = getattr(service, "_runner", None)
    if runner is None:
        runner = Runner(service._append_vm_event, service.data)
        service._runner = runner
    return runner
