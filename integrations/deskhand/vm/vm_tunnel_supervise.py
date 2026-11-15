#!/usr/bin/env python3
"""vm_tunnel_supervise.py -- supervisor for the B42 VM poll loop.

Run from cron every 2 minutes (see vm_tunnel_supervision.json).
Reads the loop's heartbeat.json + pid file and restores the loop
when it is missing or wedged:

  * No pid file, or pid dead, and heartbeat stale (>180 s):
    start the loop (detached, its own session) and log it.
  * Pid alive and heartbeat fresh: nothing to do (exit 0, quiet).
  * Pid file exists but unreadable: treat as missing (start a loop;
    the loop's own pid claim refuses if a live loop is actually there).
  * Pid alive but heartbeat stale (>300 s): fail closed and require
    operator review; the PID is not sufficient proof of process identity.

The supervisor never touches the agent, the mailbox, or any key:
it only manages the poll-loop process. It never deletes turn or
reply files. Its metadata-only tunnel-supervise.log rotates at 1 MiB,
keeping two older generations.

The cron job itself is created at deploy time (see
DEPLOY-HANDOFF.md) -- this script is the supervised check, not the
schedule.

Run:  python3 vm_tunnel_supervise.py [--supervision vm_tunnel_supervision.json]
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUPERVISOR_LOG_MAX_BYTES = 1024 * 1024
SUPERVISOR_LOG_GENERATIONS = 2


def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _rotate_log(path):
    path = Path(path)
    try:
        if not path.exists() or path.stat().st_size <= SUPERVISOR_LOG_MAX_BYTES:
            return
        for generation in range(SUPERVISOR_LOG_GENERATIONS, 0, -1):
            source = path if generation == 1 else path.with_name(
                path.name + "." + str(generation - 1))
            destination = path.with_name(path.name + "." + str(generation))
            if source.exists():
                os.replace(source, destination)
    except OSError as error:
        print(f"supervisor log rotation failed: {error}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="B42 tunnel poll-loop supervisor")
    ap.add_argument("--supervision", default=str(HERE / "vm_tunnel_supervision.json"))
    args = ap.parse_args()
    try:
        cfg = json.loads(Path(args.supervision).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"cannot read supervision definition: {error}", file=sys.stderr)
        return 2
    work_dir = Path(cfg["work_dir"]).expanduser()
    heartbeat_path = Path(cfg["heartbeat_path"]).expanduser()
    pid_path = Path(cfg["pid_path"]).expanduser()
    log_path = Path(cfg["supervisor_log"]).expanduser()
    heartbeat_max_age = int(cfg.get("heartbeat_max_age_secs", 180))
    wedge_max_age = int(cfg.get("wedge_max_age_secs", 300))
    loop_cmd = list(cfg["loop_command"])
    now = datetime.now(timezone.utc)

    def log(level, code, message):
        line = (f"[{now.isoformat(timespec='seconds')}] [{level}] "
                f"[tunnel_supervise] {code} -- {message}\n")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_log(log_path)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as error:
            print(f"supervisor log write failed: {error}", file=sys.stderr)
        print(line, end="")

    last_poll = None
    try:
        hb = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        last_poll = _parse_iso(hb.get("last_poll_at"))
    except (OSError, ValueError):
        last_poll = None
    heartbeat_age = (now - last_poll).total_seconds() if last_poll else None

    pid = None
    pid_alive = False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        pid_alive = True
    except (ValueError, OSError):
        pid_alive = False

    def start_loop():
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            stderr_path = work_dir / 'tunnel-poll.stderr.log'
            if stderr_path.exists() and stderr_path.stat().st_size > 1024 * 1024:
                os.replace(stderr_path, stderr_path.with_name(stderr_path.name + '.1'))
            with stderr_path.open('ab') as stderr_file:
                proc = subprocess.Popen(loop_cmd, start_new_session=True,
                                        stdout=subprocess.DEVNULL,
                                        stderr=stderr_file)
        except OSError as error:
            log("ERROR", "tunnel_supervise.start_failed", f"could not start poll loop: {error}")
            return 1
        log("INFO", "tunnel_supervise.started", f"poll loop (re)started, supervisor pid={proc.pid}")
        return 0

    if pid_alive and heartbeat_age is not None and heartbeat_age <= heartbeat_max_age:
        return 0  # healthy: loop alive, heartbeat fresh
    if pid_alive:
        # Alive but wedged: heartbeat stale past the wedge bound.
        if heartbeat_age is None or heartbeat_age > wedge_max_age:
            # The PID file alone does not prove identity: a terminated loop's
            # PID can have been reused by an unrelated process. Until a
            # race-free identity check is available, never signal that PID.
            log("ERROR", "tunnel_supervise.wedged_unverified",
                f"pid {pid} alive but heartbeat stale "
                f"({heartbeat_age if heartbeat_age is not None else 'missing'}s); "
                "identity unverified, operator review required")
            return 1
        # Stale but inside the wedge bound: give it one more cycle.
        log("INFO", "tunnel_supervise.heartbeat_stale",
            f"loop pid {pid} alive, heartbeat {heartbeat_age:.0f}s old (inside wedge bound); waiting")
        return 0
    log("WARN", "tunnel_supervise.loop_missing",
        f"no live poll loop (pid file: {'stale' if pid is not None else 'absent'}, "
        f"heartbeat age: {heartbeat_age if heartbeat_age is not None else 'missing'}s)")
    return start_loop()


if __name__ == "__main__":
    sys.exit(main())
