"""Durable request identities, safe restart recovery and bounded retention.

Chat bodies are excluded by default. Completed actions are never automatically
replayed: a duplicate receives its durable outcome or an uncertainty refusal.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class StateStore:
    def __init__(self, path: Path, retain_chat_text: bool = False):
        self.path = path
        self.retain_chat_text = retain_chat_text
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, updated REAL, record TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, kind TEXT, fingerprint TEXT, state TEXT, updated REAL, outcome TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS operations_updated ON operations(updated)")
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, updated REAL, record TEXT)")
            db.execute("UPDATE operations SET state='uncertain' WHERE state='running'")

    @contextmanager
    def connection(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=5)
            try:
                with db:
                    yield db
            finally:
                db.close()

    def save_chat(self, identifier: str, record: dict) -> None:
        saved = {key: value for key, value in record.items()
                 if not key.startswith("_") and key != "fingerprint"}
        if not self.retain_chat_text:
            saved.pop("request_text", None)
            saved.pop("text", None)
            saved.pop("partial_text", None)
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO chats VALUES (?, ?, ?)",
                       (identifier, time.time(), json.dumps(saved, separators=(",", ":"))))
            if db.execute("SELECT count(*) FROM chats").fetchone()[0] > 10000:
                # Pending records are never silently evicted.
                rows = db.execute("SELECT id,record FROM chats ORDER BY updated LIMIT 1000").fetchall()
                for old_id, serialized in rows:
                    if json.loads(serialized).get("terminal"):
                        db.execute("DELETE FROM chats WHERE id=?", (old_id,))

    def load_chats(self, limit=1000) -> dict[str, dict]:
        with self.connection() as db:
            rows = db.execute("SELECT id,record FROM chats ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        restored = {}
        for identifier, serialized in reversed(rows):
            record = json.loads(serialized)
            record.setdefault("request_text", "[Message text was not retained]")
            record.setdefault("text", "[Reply text was not retained]" if record.get("terminal") else "")
            record["recovered"] = True
            if not record.get("terminal"):
                record.update(status="pending", delivery="unknown_after_restart")
            restored[identifier] = record
        return restored

    def claim(self, identifier: str, kind: str, payload: dict) -> dict:
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                                 allow_nan=False).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT kind,fingerprint,state,outcome FROM operations WHERE id=?", (identifier,)).fetchone()
            if row:
                if row[0] != kind or row[1] != fingerprint:
                    raise ValueError("Request identity reused with different content")
                return {"new": False, "state": row[2], "outcome": json.loads(row[3]) if row[3] else None}
            if db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 100000:
                raise ValueError("Durable operation history is full; local maintenance is required")
            db.execute("INSERT INTO operations VALUES (?, ?, ?, 'running', ?, NULL)",
                       (identifier, kind, fingerprint, time.time()))
        return {"new": True, "state": "running"}

    def finish(self, identifier: str, state: str, outcome: dict | None = None) -> None:
        # Store outcome metadata, not stdout, file contents, typed text or images.
        safe = {key: value for key, value in (outcome or {}).items()
                if key in {"ok", "exit_code", "code", "response_type", "cancelled"}}
        with self.connection() as db:
            db.execute("UPDATE operations SET state=?,updated=?,outcome=? WHERE id=?",
                       (state, time.time(), json.dumps(safe), identifier))

    def operation(self, identifier: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT kind,state,updated,outcome FROM operations WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        return {"id": identifier, "kind": row[0], "state": row[1], "updated": row[2],
                "outcome": json.loads(row[3]) if row[3] else None}

    def prune(self, chat_days=7, operation_days=30) -> None:
        now = time.time()
        with self.connection() as db:
            # Pending chats are preserved until explicitly cancelled or completed.
            rows = db.execute("SELECT id,record FROM chats WHERE updated < ?", (now-chat_days*86400,)).fetchall()
            for identifier, serialized in rows:
                if json.loads(serialized).get("terminal"):
                    db.execute("DELETE FROM chats WHERE id=?", (identifier,))
            db.execute("DELETE FROM operations WHERE state IN ('complete','failed') AND updated < ?",
                       (now-operation_days*86400,))
            db.execute("DELETE FROM tasks WHERE updated < ?", (now-operation_days*86400,))

    def save_task(self, record):
        safe = {key: record[key] for key in ("task_id", "request_id", "fingerprint", "state", "created")}
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?)",
                       (record["task_id"], time.time(), json.dumps(safe)))

    def load_tasks(self):
        with self.connection() as db:
            rows = db.execute("SELECT id,record FROM tasks ORDER BY updated DESC LIMIT 1000").fetchall()
        result = {}
        for identifier, serialized in rows:
            record = json.loads(serialized)
            prior = record["state"]
            record.update(state="failed", result={"task_id": identifier, "ok": False,
                          "result": f"Recovered task metadata after restart (prior state: {prior}); task was not replayed. Result text was not retained."})
            result[identifier] = record
        return result
