#!/usr/bin/env python3
"""vm_tunnel_poll.py -- persistent VM-side poll loop for the B42 chat tunnel.

The VM is client-only on the tailnet: it can reach out to the laptop
agent, nothing can reach in, and it hosts nothing. So the agent
cannot push chat turns to the VM -- this loop polls the agent's
/chat/* mailbox and carries turns both ways:

  agent --POST /chat/claim--> loop --turns/<id>.json--> Muse side
  Muse side --replies/<id>.json--> loop --POST /chat/reply--> agent

Handoff contract (the Muse side of the loop -- a cron / the main
agent runtime -- reads and writes these files):
  * turns/<turn_id>.json, written atomically (tmp file + os.replace):
    {"turn_id", "text", "assistant", "model", "account", "effort",
     "agent_task_id", "sent_at_ms", "received_at_ms", "claimed_at"}.
    The Muse side consumes a turn by DELETING the file after it has
    taken responsibility for the reply. A turn file older than 1 hour
    is a WARN (the Muse side may be down) -- never re-claimed and
    never deleted by this loop: the agent already marked it
    dispatched, and this loop owns it until it posts a reply.
  * replies/<turn_id>.json, written atomically by the Muse side:
    {"turn_id", "reply": "..."} or {"turn_id", "error": "..."}.
    The loop atomically claims each file into reply-claims/<unique>/,
    then deletes only the claimed file after the agent confirms its
    exact content digest and the receipt is durably recorded. A failed
    or conflicting response remains claimed for reconciliation. A bad
    file is retained in a unique directory under replies-dead/.
  * heartbeat.json, rewritten every iteration: {"last_poll_at",
    "agent_reachable", "turns_claimed_total", "replies_posted_total",
    "last_error"}. The supervisor (vm_tunnel_supervise.py) reads it.
  * tunnel-poll.pid: the loop's PID. A second loop refuses to start
    while a live PID owns it.

Credentials: the loop authenticates with the SCOPED CHAT KEY only
(never the full exec token -- with the chat key it cannot exec, read
files, screenshot, or drive input even if it tried). The key comes
from --chat-key-file (a 0600 file created at deploy, preferred) or
the DESKHAND_CHAT_KEY environment variable. This build ships no key
and no key file. A missing key is a loud startup failure (exit 2).

Resilience: one bad iteration never kills the loop (log + backoff);
a 401 (wrong key) is logged as an ERROR every iteration but the loop
keeps running -- a restart would not fix a bad key, and the
heartbeat's last_error is what an operator acts on. SIGTERM/SIGINT
exit cleanly (the PID file is removed).

Logging: turn_id + byte lengths + counters only. Turn text, reply
text, and the key are NEVER logged.

Run:  python3 vm_tunnel_poll.py --agent-url http://<tailscale-ip>:8899 \
        --work-dir ~/workspace/deskhand-coordinator/tunnel-state
Test:  python3 vm_tunnel_poll.py --once [same flags]
"""
import argparse
import json
import os
import signal
import stat
import sys
import time
import threading
import traceback
import hashlib
import uuid
try:
    import fcntl
except ImportError:
    fcntl = None  # local non-Linux tests only; run() fails closed
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_AGENT_URL = "http://phillyg.tail559de7.ts.net:8899"
DEFAULT_WORK_DIR = Path.home() / "workspace" / "deskhand-coordinator" / "tunnel-state"
TURN_STALE_WARN_SECS = 3600
POSTED_TURNS_MAX = 200
LOG_MAX_BYTES = 1024 * 1024
LOG_GENERATIONS = 2

STOP = False


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _handle_signal(_signum, _frame):
    global STOP
    STOP = True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused")


def _agent_opener():
    """Opener for the Tailscale agent -- mirrors agent_transient.py.

    The VM's http_proxy/https_proxy point at the egress proxy (port
    3128), which cannot reach Tailscale hostnames. Derive the
    tailnet-reachable proxy from HTTPS_PROXY at runtime by swapping
    its port to 3130 (any credentials in the value are preserved by
    the rsplit; the value is never hardcoded, never logged, and never
    written anywhere). Loopback hosts stay direct: ProxyHandler
    honours no_proxy.
    """
    proxy = os.environ.get("HTTPS_PROXY", "")
    if proxy:
        proxy = proxy.rsplit(":", 1)[0] + ":3130"
        return urllib.request.build_opener(
            _NoRedirect(), urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(_NoRedirect())


class PollLoop:
    def __init__(self, agent_url, chat_key, work_dir, interval_secs=30,
                 claim_limit=10):
        self.agent_url = agent_url.rstrip("/")
        self.chat_key = chat_key
        self.work_dir = Path(work_dir)
        self.turns_dir = self.work_dir / "turns"
        self.replies_dir = self.work_dir / "replies"
        self.dead_dir = self.work_dir / "replies-dead"
        self.claims_dir = self.work_dir / "reply-claims"
        self.completed_dir = self.work_dir / "reply-completed"
        for d in (self.work_dir, self.turns_dir, self.replies_dir, self.dead_dir, self.claims_dir, self.completed_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._sync_dir(self.work_dir)
        self.log_path = self.work_dir / "tunnel-poll.log"
        self.heartbeat_path = self.work_dir / "heartbeat.json"
        self.pid_path = self.work_dir / "tunnel-poll.pid"
        self.posted_turns_path = self.work_dir / "posted-turns.json"
        self.posted_turn_ids = self._load_posted_turns()
        self.interval_secs = interval_secs
        self.claim_limit = claim_limit
        self.turns_claimed_total = 0
        self.replies_posted_total = 0
        self.last_error = None
        self.poster_stop = threading.Event()
        self.log_lock = threading.Lock()
        self.pid_lock_file = None
        self.retry_after = {}
        self.poster_error = None
        self.poster_cursor = ''
        self.started_seen = set()

    # -- posted-turn dedup: never post a second reply for a turn_id
    # already posted. The set is a JSON list on disk (oldest first),
    # bounded to the most recent POSTED_TURNS_MAX ids, loaded at
    # startup and appended on each successful post. A restart loses
    # nothing: the file is the memory, not the process.
    def _load_posted_turns(self):
        try:
            data = json.loads(self.posted_turns_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                ids = [t for t in data if isinstance(t, str)]
                return ids[-POSTED_TURNS_MAX:]
        except (OSError, ValueError):
            pass
        return []

    def _record_posted(self, turn_id):
        if turn_id not in self.posted_turn_ids:
            self.posted_turn_ids.append(turn_id)
        self.posted_turn_ids = self.posted_turn_ids[-POSTED_TURNS_MAX:]
        self._write_atomic(self.posted_turns_path, self.posted_turn_ids)

    # -- logging (metadata only; never turn/reply text, never the key)
    def _rotate_log(self):
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX_BYTES:
                for g in range(LOG_GENERATIONS, 0, -1):
                    src = self.log_path if g == 1 else self.log_path.with_name(
                        self.log_path.name + "." + str(g - 1))
                    dst = self.log_path.with_name(self.log_path.name + "." + str(g))
                    if src.exists():
                        os.replace(src, dst)
        except OSError:
            pass

    def log(self, level, code, message):
        with self.log_lock:
            self._rotate_log()
            line = f"[{_iso_now()}] [{level}] [tunnel_poll] {code} -- {message}\n"
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                pass

    # -- pid file: one loop only
    def claim_pid(self):
        if fcntl is None:
            raise RuntimeError('Linux flock is required for exclusive loop ownership')
        lock_file = (self.work_dir / 'tunnel-poll.lock').open('a+')
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            lock_file.close()
            return False
        self.pid_lock_file = lock_file
        if self.pid_path.exists():
            try:
                pid = int(self.pid_path.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)
                self.pid_lock_file.close()
                self.pid_lock_file = None
                return False
            except (ValueError, OSError):
                pass  # stale or unreadable pid file: take it over
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def release_pid(self):
        try:
            if self.pid_path.exists() and int(self.pid_path.read_text().strip()) == os.getpid():
                self.pid_path.unlink()
        except (ValueError, OSError):
            pass
        finally:
            if self.pid_lock_file is not None:
                self.pid_lock_file.close()
                self.pid_lock_file = None

    # -- agent HTTP (chat key only)
    def _agent(self, method, path, payload=None):
        body = None
        headers = {"Authorization": "Bearer " + self.chat_key}
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.agent_url + path, data=body,
                                     headers=headers, method=method)
        try:
            with _agent_opener().open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, {"error": error.read().decode("utf-8", "replace")[:200]}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise ConnectionError(f"agent {method} {path} unreachable: {error}")

    @staticmethod
    def _sync_dir(path):
        if os.name == 'nt':
            return  # local test host; Linux verification is required
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_atomic(self, path, payload):
        tmp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
        with tmp.open('w', encoding='utf-8') as file:
            file.write(json.dumps(payload))
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        self._sync_dir(path.parent)

    # -- one iteration: claim turns, then post replies
    def iterate(self):
        agent_reachable = True
        try:
            status, data = self._agent("POST", "/chat/claim", {"limit": self.claim_limit})
        except ConnectionError as error:
            agent_reachable = False
            self.last_error = str(error)
            self.log("WARN", "tunnel_poll.agent_down", str(error)[:200])
            self._write_heartbeat(agent_reachable)
            return
        if status == 401:
            agent_reachable = False
            self.last_error = "agent returned 401: the chat key is wrong or missing"
            self.log("ERROR", "tunnel_poll.auth_failed",
                     "agent returned 401 on /chat/claim: the chat key is wrong or missing "
                     "(fix the key; restarting the loop will not help)")
            self._write_heartbeat(agent_reachable)
            return
        if status != 200 or not isinstance(data, dict):
            agent_reachable = False
            self.last_error = f"agent /chat/claim -> HTTP {status}"
            self.log("WARN", "tunnel_poll.claim_failed", f"agent /chat/claim -> HTTP {status}")
            self._write_heartbeat(agent_reachable)
            return
        self.last_error = None
        for turn in data.get("turns", []) or []:
            turn_id = turn.get("turn_id", "")
            if not isinstance(turn_id, str) or not turn_id or "/" in turn_id or turn_id in (".", ".."):
                self.log("WARN", "tunnel_poll.bad_turn_id", f"skipping turn with bad id {turn_id!r}")
                continue
            dest = self.turns_dir / (turn_id + ".json")
            claimed = self.work_dir / 'turns-claimed' / (turn_id + '.json')
            completed = self.completed_dir / (turn_id + '.json')
            if claimed.exists() or completed.exists():
                self.log('WARN', 'tunnel_poll.reclaimed_turn_uncertain',
                         f'turn_id={turn_id} prior claim or completion exists; review required')
                continue
            if dest.exists():
                continue  # already handed off; the Muse side has not consumed it yet
            record = dict(turn)
            record["claimed_at"] = _iso_now()
            self._write_atomic(dest, record)
            self.turns_claimed_total += 1
            text = turn.get("text", "")
            self.log("INFO", "tunnel_poll.turn_claimed",
                     f"turn_id={turn_id} text_bytes={len(str(text).encode())}")
        # Unconsumed turns older than an hour: the Muse side may be down.
        now = time.time()
        for path in self.turns_dir.glob("*.json"):
            try:
                if now - path.stat().st_mtime > TURN_STALE_WARN_SECS:
                    self.log("WARN", "tunnel_poll.turn_unconsumed",
                             f"turn {path.stem} unconsumed for over 1 hour (Muse side may be down)")
            except OSError:
                pass
        self._write_heartbeat(agent_reachable)

    def _post_replies(self):
        # Atomically move each published file into a unique private directory.
        # A writer replacing the original path thereafter creates a separate
        # candidate and cannot replace the file being sent or cleaned up.
        for source in sorted(self.replies_dir.glob('*.json')):
            if source.name.startswith('.') or '.tmp-' in source.name:
                continue
            claim_dir = self.claims_dir / uuid.uuid4().hex
            try:
                claim_dir.mkdir()
                os.replace(source, claim_dir / source.name)
                self._sync_dir(claim_dir)
                self._sync_dir(self.claims_dir)
                self._sync_dir(self.replies_dir)
            except OSError:
                try:
                    claim_dir.rmdir()
                except OSError:
                    pass
        files = sorted(self.claims_dir.glob('*/*.json'))
        files = ([p for p in files if str(p) > self.poster_cursor] +
                 [p for p in files if str(p) <= self.poster_cursor])
        attempts = 0
        for path in files:
            if self.poster_stop.is_set():
                break
            if path.name.startswith('.') or '.tmp-' in path.name:
                continue
            turn_id = path.stem
            retry_key = str(path)
            if time.monotonic() < self.retry_after.get(retry_key, 0):
                continue
            self.poster_cursor = retry_key
            try:
                # A prior rename or directory fsync may have failed or been
                # interrupted. Never ask the agent to accept a reply until
                # both the owned file and its directory entries are durable.
                with path.open('rb') as file:
                    if os.name != 'nt':
                        os.fsync(file.fileno())
                self._sync_dir(path.parent)
                self._sync_dir(self.claims_dir)
                self._sync_dir(self.replies_dir)
                payload = json.loads(path.read_text(encoding="utf-8"))
            except OSError:
                self.retry_after[retry_key] = time.monotonic() + 5
                self.poster_error = 'reply claim unavailable or not durable'
                continue
            except ValueError as error:
                self._dead_letter(path, f"unreadable reply file: {error}")
                continue
            if not isinstance(payload, dict) or payload.get("turn_id") != turn_id:
                self._dead_letter(path, "turn_id in file does not match filename")
                continue
            # Recheck even previously recorded IDs: the ledger has no digest,
            # and a conflicting later reply must never be discarded.
            if isinstance(payload.get("reply"), str) and payload["reply"]:
                method, body = "POST", {"turn_id": turn_id, "reply": payload["reply"]}
                kind = "reply"
            elif isinstance(payload.get("error"), str) and payload["error"]:
                method, body = "POST", {"turn_id": turn_id, "error": payload["error"]}
                kind = "error"
            else:
                self._dead_letter(path, "reply file has neither a reply nor an error")
                continue
            digest = hashlib.sha256(body[kind].encode('utf-8')).hexdigest()
            marker = self.completed_dir / (turn_id + '.json')
            try:
                recorded = json.loads(marker.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                recorded = None
            if (isinstance(recorded, dict) and recorded.get('turn_id') == turn_id
                    and recorded.get('kind') in ('reply', 'error')
                    and isinstance(recorded.get('stored_sha256'), str)
                    and len(recorded['stored_sha256']) == 64
                    and all(c in '0123456789abcdef' for c in recorded['stored_sha256'])):
                if recorded['kind'] == kind and recorded['stored_sha256'] == digest:
                    try:
                        path.unlink()
                        self._sync_dir(path.parent)
                        path.parent.rmdir()
                        self._sync_dir(self.claims_dir)
                    except OSError:
                        pass
                    self.log('INFO', 'tunnel_poll.duplicate_reply_skipped',
                             f'turn_id={turn_id} matched durable receipt')
                else:
                    # Conflicts remain available for operator review and are
                    # never repeatedly sent to the agent.
                    conflicts = self.work_dir / 'replies-conflicts'
                    try:
                        conflicts.mkdir(exist_ok=True)
                        self._sync_dir(self.work_dir)
                        destination = conflicts / path.parent.name
                        destination.mkdir(exist_ok=True)
                        self._sync_dir(conflicts)
                        os.replace(path, destination / path.name)
                        self._sync_dir(destination)
                        self._sync_dir(path.parent)
                        path.parent.rmdir()
                        self._sync_dir(self.claims_dir)
                        self.log('WARN', 'tunnel_poll.duplicate_reply_skipped',
                                 f'turn_id={turn_id} conflicting reply retained for review')
                    except OSError:
                        self.poster_error = 'failed to retain conflicting reply'
                self.retry_after.pop(retry_key, None)
                continue
            try:
                attempts += 1
                status, _data = self._agent(method, "/chat/reply" if kind == "reply" else "/chat/error", body)
            except ConnectionError as error:
                self.poster_error = type(error).__name__
                self.retry_after[retry_key] = time.monotonic() + 5
                self.log("WARN", "tunnel_poll.agent_down", str(error)[:200])
                if attempts >= 4:
                    break
                continue  # preserve this file; try other ready replies
            expected_status = 'replied' if kind == 'reply' else 'failed'
            if (status == 200 and isinstance(_data, dict)
                    and _data.get('turn_id') == turn_id
                    and _data.get('status') == expected_status
                    and _data.get('stored_sha256') == digest):
                # Persist the dedup receipt before deleting the reply file.
                # A lost response or failed disk write safely retries against
                # the agent's idempotent first-reply-wins mailbox.
                try:
                    self._record_posted(turn_id)
                    self._write_atomic(self.completed_dir / (turn_id + '.json'),
                                       {'turn_id': turn_id, 'kind': kind, 'stored_sha256': digest})
                except OSError:
                    self.posted_turn_ids = self._load_posted_turns()
                    continue
                try:
                    path.unlink()
                    self._sync_dir(path.parent)
                    path.parent.rmdir()
                    self._sync_dir(self.claims_dir)
                except OSError:
                    # Retrying is safe: the remote agent preserves its first
                    # reply and returns its digest again.
                    pass
                self.replies_posted_total += 1
                self.retry_after.pop(retry_key, None)
                self.poster_error = None
                self.log("INFO", "tunnel_poll.reply_posted", f"turn_id={turn_id} kind={kind}")
            else:
                self.poster_error = f"agent reply post -> HTTP {status}"
                self.retry_after[retry_key] = time.monotonic() + 5
                self.log("WARN", "tunnel_poll.reply_failed",
                         f"turn_id={turn_id} post -> HTTP {status}; receipt unverified, retained")
            if attempts >= 4:
                break

    def _poster(self):
        while not self.poster_stop.is_set():
            try:
                self._observe_worker_starts()
                self._post_replies()
            except Exception as error:
                self.log("ERROR", "tunnel_poll.poster_failed", type(error).__name__)
            self.poster_stop.wait(2)

    def _observe_worker_starts(self):
        for path in self.replies_dir.glob('.*.started'):
            turn_id = path.name[1:-len('.started')]
            if not turn_id or turn_id in self.started_seen:
                continue
            try:
                stamp = path.read_text(encoding='utf-8').strip()
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if parsed.tzinfo is None or len(stamp) > 40:
                    continue
            except (OSError, UnicodeError, ValueError):
                continue
            self.started_seen.add(turn_id)
            self.log('INFO', 'tunnel_poll.worker_started',
                     f'turn_id={turn_id} started_at={parsed.isoformat()}')

    def _dead_letter(self, path, reason):
        try:
            dest = self.dead_dir / path.parent.name
            dest.mkdir(exist_ok=True)
            self._sync_dir(self.dead_dir)
            os.replace(path, dest / path.name)
            self._sync_dir(dest)
            self._sync_dir(path.parent)
            path.parent.rmdir()
            self._sync_dir(self.claims_dir)
        except OSError:
            pass
        self.log("WARN", "tunnel_poll.dead_letter", f"{path.name}: {reason}")

    def _write_heartbeat(self, agent_reachable):
        payload = {"last_poll_at": _iso_now(),
                   "agent_reachable": agent_reachable,
                   "turns_claimed_total": self.turns_claimed_total,
                   "replies_posted_total": self.replies_posted_total,
                   "poster_error": self.poster_error,
                   "last_error": self.last_error}
        try:
            import resource
            payload['max_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            pass
        try:
            self._write_atomic(self.heartbeat_path, payload)
        except OSError:
            pass

    def run(self):
        if not self.claim_pid():
            self.log("ERROR", "tunnel_poll.already_running",
                     "another poll loop owns the pid file; refusing to start a second loop")
            return 3
        self.log("INFO", "tunnel_poll.started",
                 f"poll loop started (pid {os.getpid()}, interval {self.interval_secs}s)")
        poster = threading.Thread(target=self._poster, name="reply-poster")
        poster.start()
        try:
            while not STOP:
                try:
                    self.iterate()
                except Exception as error:  # one bad iteration never kills the loop
                    self.last_error = str(error)
                    self.log("ERROR", "tunnel_poll.iteration_failed", str(error)[:200])
                    self._write_heartbeat(False)
                waited = 0.0
                while waited < self.interval_secs and not STOP:
                    time.sleep(0.5)
                    waited += 0.5
        except BaseException as error:
            self.log("ERROR", "tunnel_poll.fatal", f"pid={os.getpid()} error={type(error).__name__}")
            # Frames only: exception messages can contain request content or secrets.
            traceback.print_tb(error.__traceback__, file=sys.stderr)
            raise
        finally:
            self.poster_stop.set()
            # Keep exclusive process ownership until the poster has stopped.
            poster.join()
            self.release_pid()
            self.log("INFO", "tunnel_poll.stopped", f"poll loop stopped (pid {os.getpid()})")
        return 0


def resolve_chat_key(args):
    """The chat key, from --chat-key-file (preferred) or the env var.
    Returns None when neither yields a usable key (loud failure)."""
    if args.chat_key_file:
        path = Path(args.chat_key_file)
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError:
            print(f"chat key file unreadable: {path}", file=sys.stderr)
            return None
        if not key:
            print(f"chat key file is empty: {path}", file=sys.stderr)
            return None
        try:
            mode = path.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                print(f"WARNING: chat key file {path} is group/world-accessible; fix with chmod 600",
                      file=sys.stderr)
        except OSError:
            pass
        return key
    key = os.environ.get(args.chat_key_env, "").strip()
    if not key:
        print(f"no chat key: set {args.chat_key_env} or pass --chat-key-file", file=sys.stderr)
        return None
    return key


def main():
    ap = argparse.ArgumentParser(description="B42 chat-tunnel VM poll loop")
    ap.add_argument("--agent-url", default=DEFAULT_AGENT_URL)
    ap.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR))
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--claim-limit", type=int, default=10)
    ap.add_argument("--chat-key-env", default="DESKHAND_CHAT_KEY")
    ap.add_argument("--chat-key-file", default=None)
    ap.add_argument("--once", action="store_true",
                    help="run a single iteration, then exit (tests / cron probes)")
    args = ap.parse_args()
    if args.interval < 2:
        print("interval must be >= 2 seconds", file=sys.stderr)
        return 2
    key = resolve_chat_key(args)
    if key is None:
        return 2
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    loop = PollLoop(args.agent_url, key, args.work_dir,
                    interval_secs=args.interval, claim_limit=args.claim_limit)
    if args.once:
        if not loop.claim_pid():
            print("another poll loop owns the pid file", file=sys.stderr)
            return 3
        try:
            loop.iterate()
            loop._post_replies()
        finally:
            loop.release_pid()
        return 0
    return loop.run()


if __name__ == "__main__":
    sys.exit(main())
