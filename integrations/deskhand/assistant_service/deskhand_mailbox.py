"""Durable Deskhand-owned assistant queue. No model or action executor runs here."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


class Mailbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, text TEXT NOT NULL,
                assistant TEXT NOT NULL, model TEXT NOT NULL, account TEXT NOT NULL,
                effort TEXT NOT NULL, status TEXT NOT NULL,
                reply TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS chat_admission (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                stopped INTEGER NOT NULL CHECK (stopped IN (0,1)), epoch INTEGER NOT NULL
            )""")
            db.execute("INSERT OR IGNORE INTO chat_admission VALUES (1,0,0)")
            db.execute("CREATE TABLE IF NOT EXISTS tunnel_receipts (turn_id TEXT PRIMARY KEY)")
            if "agent_task_id" not in {row[1] for row in db.execute("PRAGMA table_info(turns)")}:
                db.execute("ALTER TABLE turns ADD COLUMN agent_task_id TEXT")
            # Batch 2, v100.004: streamed partial text (display-only, never
            # a reply) and per-hop timing JSON. Added as columns so old
            # databases migrate in place; finish() clears the partial.
            columns = {row[1] for row in db.execute("PRAGMA table_info(turns)")}
            if "partial" not in columns:
                db.execute("ALTER TABLE turns ADD COLUMN partial TEXT")
            if "partial_seq" not in columns:
                db.execute("ALTER TABLE turns ADD COLUMN partial_seq INTEGER")
            if "timing_json" not in columns:
                db.execute("ALTER TABLE turns ADD COLUMN timing_json TEXT")
            # 100.039: phone send time + PC receipt time (epoch millis,
            # NULL when absent). Same in-place migration pattern as
            # above; old databases gain the columns on first open and
            # old rows read NULL. Not part of the fingerprint.
            if "sent_at" not in columns:
                db.execute("ALTER TABLE turns ADD COLUMN sent_at INTEGER")
            if "received_at" not in columns:
                db.execute("ALTER TABLE turns ADD COLUMN received_at INTEGER")

    def _open(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def submit(self, turn_id: str, text: str, assistant: str, model: str, account: str = "", effort: str = "", agent_task_id: str | None = None, sent_at: int | None = None, received_at: int | None = None):
        # 100.039: timestamps are stored, never fingerprinted — a
        # re-submit of the same turn id carries a different sent_at
        # and must still match (first write wins). Non-int values
        # store as NULL (the service validates; this is the backstop).
        if isinstance(sent_at, bool) or not isinstance(sent_at, int):
            sent_at = None
        if isinstance(received_at, bool) or not isinstance(received_at, int):
            received_at = None
        if str(uuid.UUID(turn_id)) != turn_id:
            raise ValueError("turn ID must be a canonical UUID")
        if not isinstance(text, str) or not text.strip() or len(text) > 24000:
            raise ValueError("invalid message")
        if assistant not in {"codex", "local", "muse"} or not isinstance(model, str) or not model or len(model) > 256:
            raise ValueError("invalid assistant or model")
        if not isinstance(account, str) or len(account) > 256 or not isinstance(effort, str) or len(effort) > 64:
            raise ValueError("invalid account or effort")
        if agent_task_id is not None:
            try:
                task_uuid = uuid.UUID(agent_task_id)
                if str(task_uuid) != agent_task_id or task_uuid.version != 4: raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError("invalid agent task ID") from None
        identity = [text, assistant, model, account, effort]
        if agent_task_id is not None: identity.append(agent_task_id)
        fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        now = time.time()
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    db.rollback()
                    raise ValueError("turn ID already belongs to a different request")
                db.commit()
                return dict(old)
            db.execute("INSERT INTO turns(id,fingerprint,text,assistant,model,account,effort,status,reply,error,created,updated,agent_task_id,sent_at,received_at) VALUES (?,?,?,?,?,?,?,'queued',NULL,NULL,?,?,?, ?,?)", (turn_id, fingerprint, text, assistant, model, account, effort, now, now, agent_task_id, sent_at, received_at))
            db.commit()
        return self.get(turn_id)

    def admission_state(self):
        with closing(self._open()) as db:
            row = db.execute("SELECT stopped,epoch FROM chat_admission WHERE singleton=1").fetchone()
            if row is None:
                raise RuntimeError("Chat admission state is missing")
            return bool(row["stopped"]), row["epoch"]

    def set_admission_state(self, stopped: bool, epoch: int):
        with closing(self._open()) as db:
            result = db.execute("UPDATE chat_admission SET stopped=?,epoch=? WHERE singleton=1", (int(stopped), epoch))
            if result.rowcount != 1:
                raise RuntimeError("Chat admission state is missing")

    def persist_stop_and_cancel(self, epoch: int):
        """Commit the stop and queued-turn cancellation together."""
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("UPDATE chat_admission SET stopped=1,epoch=? WHERE singleton=1", (epoch,))
            if result.rowcount != 1:
                db.rollback()
                raise RuntimeError("Chat admission state is missing")
            rows = db.execute("SELECT id FROM turns WHERE status='queued' ORDER BY created,id").fetchall()
            db.execute("UPDATE turns SET status='cancelled',error=?,updated=? WHERE status='queued'",
                       ("Cancelled before dispatch", time.time()))
            db.commit()
            return [row["id"] for row in rows]

    def delete_queued_and_cancelled(self, just_cancelled=None):
        """Delete queued turns and turns cancelled by this same restart (100.013, Item 1b, O4).

        Deletes every 'queued' row always, plus 'cancelled' rows only when
        their id is in just_cancelled (the id list persist_stop_and_cancel
        returned for this restart). Older cancelled history is preserved.
        Never touches 'dispatching' or 'unknown' rows: a turn whose outcome
        is unknown may already have been delivered, so it is never deleted.
        Runs in one transaction and returns the deleted ids in order.
        """
        ids = [turn_id for turn_id in (just_cancelled or []) if isinstance(turn_id, str)]
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id FROM turns WHERE status='queued' ORDER BY created,id").fetchall()
            deleted = [row["id"] for row in rows]
            db.execute("DELETE FROM turns WHERE status='queued'")
            if ids:
                placeholders = ",".join("?" for _ in ids)
                rows = db.execute(
                    "SELECT id FROM turns WHERE status='cancelled' AND id IN (" + placeholders + ")",
                    ids).fetchall()
                deleted += [row["id"] for row in rows]
                db.execute(
                    "DELETE FROM turns WHERE status='cancelled' AND id IN (" + placeholders + ")",
                    ids)
            db.commit()
            return deleted

    def get(self, turn_id: str):
        with closing(self._open()) as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            return dict(row) if row else None


    def mark_tunnel_dispatch(self, turn_id):
        """Record plain-chat provenance before attempting external delivery."""
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,assistant,agent_task_id FROM turns WHERE id=?", (turn_id,)).fetchone()
            if not row or row['status'] != 'dispatching' or row['assistant'] != 'muse' or row['agent_task_id'] is not None:
                db.rollback()
                raise ValueError('not a dispatching plain Muse turn')
            db.execute("INSERT OR IGNORE INTO tunnel_receipts(turn_id) VALUES (?)", (turn_id,))
            db.commit()

    def pending_tunnel_receipts(self):
        with closing(self._open()) as db:
            return [dict(row) for row in db.execute(
                "SELECT turns.* FROM turns JOIN tunnel_receipts ON turns.id=tunnel_receipts.turn_id "
                "WHERE turns.status IN ('unknown','completed','failed') ORDER BY turns.updated")]

    def unmarked_unknown_muse_turns(self, limit=100):
        with closing(self._open()) as db:
            return [dict(row) for row in db.execute(
                "SELECT turns.* FROM turns LEFT JOIN tunnel_receipts ON turns.id=tunnel_receipts.turn_id "
                "WHERE turns.status='unknown' AND turns.assistant='muse' "
                "AND turns.agent_task_id IS NULL AND tunnel_receipts.turn_id IS NULL "
                "ORDER BY turns.created,turns.id LIMIT ?", (max(1, min(int(limit), 500)),))]

    def mark_unknown_muse_for_recovery(self, turn_id):
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT 1 FROM turns WHERE id=? AND status='unknown' "
                "AND assistant='muse' AND agent_task_id IS NULL", (turn_id,)).fetchone()
            if row is None:
                db.rollback()
                return False
            db.execute("INSERT OR IGNORE INTO tunnel_receipts(turn_id) VALUES (?)", (turn_id,))
            db.commit()
            return True

    def unmarked_terminal_muse_turns(self, after=None, limit=50):
        cursor_created, cursor_id = after if after is not None else (None, "")
        with closing(self._open()) as db:
            return [dict(row) for row in db.execute(
                "SELECT turns.* FROM turns LEFT JOIN tunnel_receipts ON turns.id=tunnel_receipts.turn_id "
                "WHERE turns.status IN ('completed','failed') AND turns.assistant='muse' "
                "AND turns.agent_task_id IS NULL AND tunnel_receipts.turn_id IS NULL "
                "AND (? IS NULL OR turns.created>? OR (turns.created=? AND turns.id>?)) "
                "ORDER BY turns.created,turns.id LIMIT ?",
                (cursor_created, cursor_created, cursor_created, cursor_id,
                 max(1, min(int(limit), 500))))]

    def resolve_tunnel_receipt(self, turn_id, reply=None, error=None):
        """Resolve only proven tunnel unknowns, or match an already durable result."""
        if (reply is None) == (error is None):
            return False
        if not isinstance(reply if reply is not None else error, str) or not (reply if reply is not None else error).strip():
            return False
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT turns.* FROM turns JOIN tunnel_receipts ON turns.id=tunnel_receipts.turn_id WHERE turns.id=?", (turn_id,)).fetchone()
            if not row or row['agent_task_id'] is not None or row['assistant'] != 'muse':
                db.rollback()
                return False
            status = 'completed' if reply is not None else 'failed'
            if row['status'] == 'unknown':
                db.execute("UPDATE turns SET status=?,reply=?,error=?,partial=NULL,partial_seq=NULL,updated=? WHERE id=? AND status='unknown'", (status, reply, error, time.time(), turn_id))
            elif row['status'] != status or row['reply'] != reply or row['error'] != error:
                db.rollback()
                return False
            db.commit()
            return True

    def clear_tunnel_receipt(self, turn_id):
        with closing(self._open()) as db:
            db.execute("DELETE FROM tunnel_receipts WHERE turn_id=?", (turn_id,))

    def recent(self, limit=100):
        with closing(self._open()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM (SELECT * FROM turns ORDER BY created DESC,id DESC LIMIT ?) ORDER BY created,id", (min(max(int(limit), 1), 200),))]

    def claim(self):
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM turns WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                db.commit()
                return None
            db.execute("UPDATE turns SET status='dispatching',updated=? WHERE id=?", (time.time(), row["id"]))
            result = db.execute("SELECT * FROM turns WHERE id=?", (row["id"],)).fetchone()
            db.commit()
            return dict(result)

    def claim_for_route(self, route):
        """Claim the oldest queued turn for one route (Batch 2).

        Same atomicity as claim(): BEGIN IMMEDIATE, oldest queued turn
        whose route columns match, flipped to dispatching in the same
        transaction. Used by the per-route parallel path so two routes
        never claim each other's turns.
        """
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM turns WHERE status='queued' AND assistant=? AND model=? "
                "AND (account IS ? OR account=?) AND (effort IS ? OR effort=?) "
                "ORDER BY created,id LIMIT 1",
                (route.assistant, route.model, route.account, route.account,
                 route.effort, route.effort)).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute("UPDATE turns SET status='dispatching',updated=? WHERE id=?", (time.time(), row["id"]))
            result = db.execute("SELECT * FROM turns WHERE id=?", (row["id"],)).fetchone()
            db.commit()
            return dict(result)

    def requeue(self, turn_id: str):
        """Return a dispatching turn to queued (Batch 2).

        Only used when the per-route path claims a turn it must not
        run (an agent-task turn): the serial path picks it up in
        order. Never touches a turn that already finished.
        """
        with closing(self._open()) as db:
            result = db.execute("UPDATE turns SET status='queued',updated=? WHERE id=? AND status='dispatching'",
                                (time.time(), turn_id))
            if result.rowcount != 1:
                raise ValueError("turn is not dispatching")

    def cancel_queued(self):
        """Cancel unstarted chat turns atomically; active work is not stopped.

        This is a queue primitive, not the desktop STOP authority. Callers must
        never represent its result as confirmation that running work stopped.
        """
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id FROM turns WHERE status='queued' ORDER BY created,id").fetchall()
            db.execute("UPDATE turns SET status='cancelled',error=?,updated=? WHERE status='queued'",
                       ("Cancelled before dispatch", time.time()))
            db.commit()
            return [row["id"] for row in rows]

    def cancel_queued_agent_turns(self):
        """Cancel queued agent turns only; chat admissions untouched.

        Same atomicity and error as cancel_queued() (BEGIN IMMEDIATE,
        "Cancelled before dispatch"), but only rows with a non-null
        agent_task_id are cancelled: queued chat turns survive, and
        stopped/epoch are never touched on this path, so it cannot
        reintroduce the S13 complaint (an agent-only stop stopping
        chat admissions). This is a queue primitive, not the desktop
        STOP authority. Callers must never represent its result as
        confirmation that running work stopped.
        """
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id FROM turns WHERE status='queued' AND agent_task_id IS NOT NULL ORDER BY created,id").fetchall()
            db.execute("UPDATE turns SET status='cancelled',error=?,updated=? WHERE status='queued' AND agent_task_id IS NOT NULL",
                       ("Cancelled before dispatch", time.time()))
            db.commit()
            return [row["id"] for row in rows]

    def publish_partial(self, turn_id: str, text: str, seq: int):
        """Store streamed chat text for a dispatching turn (Batch 2).

        Display-only: a partial is never a reply, never promotes the
        turn, and is cleared by finish(). Only newer sequences are
        stored, so a stale re-delivery cannot rewind the text.
        """
        if not isinstance(text, str) or len(text) > 100_000 or not isinstance(seq, int) or seq < 0:
            raise ValueError("invalid partial")
        with closing(self._open()) as db:
            result = db.execute(
                "UPDATE turns SET partial=?,partial_seq=?,updated=? WHERE id=? AND status='dispatching' "
                "AND (partial_seq IS NULL OR partial_seq<=?)",
                (text, seq, time.time(), turn_id, seq))
            if result.rowcount != 1:
                raise ValueError("turn is not dispatching or partial is stale")

    def record_timing(self, turn_id: str, timing: dict):
        """Store per-hop timing JSON beside a turn (Batch 2, measurement).

        Merges into any timing already stored. Never raises for a
        missing turn in the caller's path — the caller decides.
        """
        if not isinstance(timing, dict):
            raise ValueError("invalid timing")
        with closing(self._open()) as db:
            row = db.execute("SELECT timing_json FROM turns WHERE id=?", (turn_id,)).fetchone()
            if row is None:
                raise ValueError("turn not found")
            merged = {}
            if row["timing_json"]:
                try:
                    merged = json.loads(row["timing_json"])
                except ValueError:
                    merged = {}
            if isinstance(merged, dict):
                merged.update(timing)
            else:
                merged = dict(timing)
            db.execute("UPDATE turns SET timing_json=?,updated=? WHERE id=?",
                       (json.dumps(merged, ensure_ascii=True), time.time(), turn_id))

    def finish(self, turn_id: str, *, reply: str | None = None, error: str | None = None):
        if (reply is None) == (error is None) or (reply is not None and not reply.strip()) or (error is not None and not error.strip()):
            raise ValueError("provide exactly one nonempty reply or error")
        with closing(self._open()) as db:
            result = db.execute("UPDATE turns SET status=?,reply=?,error=?,partial=NULL,partial_seq=NULL,updated=? WHERE id=? AND status='dispatching'", ("completed" if reply is not None else "failed", reply, error, time.time(), turn_id))
            if result.rowcount != 1:
                raise ValueError("turn is not dispatching")

    def recover_interrupted(self):
        """Call only after acquiring exclusive ownership of the queue process."""
        with closing(self._open()) as db:
            return db.execute("UPDATE turns SET status='unknown',updated=? WHERE status='dispatching'", (time.time(),)).rowcount

    def cancel_active(self, turn_id: str):
        """Record a confirmed pre-start cancellation or backend interruption."""
        with closing(self._open()) as db:
            result = db.execute("UPDATE turns SET status='cancelled',error=?,updated=? WHERE id=? AND status='dispatching'",
                                ("Cancelled before start or confirmed interrupted", time.time(), turn_id))
            if result.rowcount != 1:
                raise ValueError("turn is not dispatching")

    def uncertain(self, turn_id: str, error: str | None = None):
        """Dispatch may have reached its destination; never automatically retry it."""
        with closing(self._open()) as db:
            result = db.execute("UPDATE turns SET status='unknown',error=?,updated=? WHERE id=? AND status='dispatching'", (error, time.time(), turn_id))
            if result.rowcount != 1:
                raise ValueError("turn is not dispatching")
