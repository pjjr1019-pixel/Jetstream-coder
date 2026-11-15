"""Append-only local operation audit records."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class AuditLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._writes = 0
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit ("
                "id TEXT PRIMARY KEY, ts REAL NOT NULL, operation TEXT NOT NULL, "
                "target TEXT NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts DESC)")
            db.execute("DELETE FROM audit WHERE ts < ?", (time.time()-30*86400,))
            db.execute("DELETE FROM audit WHERE id IN (SELECT id FROM audit ORDER BY ts DESC LIMIT -1 OFFSET 100000)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, operation: str, target: str = "", ok: bool = False, detail: str = "") -> None:
        if operation == "exec.run":
            audited_target, audited_detail = target, detail
        else:
            audited_target, audited_detail = target[:500], detail[:500]
        row = (
            str(uuid.uuid4()), time.time(), operation[:80],
            audited_target, int(ok), audited_detail,
        )
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?)", row)
            self._writes += 1
            if self._writes >= 256:
                db.execute("DELETE FROM audit WHERE ts < ?", (time.time()-30*86400,))
                db.execute("DELETE FROM audit WHERE id IN (SELECT id FROM audit ORDER BY ts DESC LIMIT -1 OFFSET 100000)")
                self._writes = 0

    def recent(self, limit: int = 200, query: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as db:
            if query:
                rows = db.execute(
                    "SELECT ts,operation,target,ok,detail FROM audit "
                    "WHERE operation LIKE ? OR target LIKE ? OR detail LIKE ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (f"%{query[:100]}%",) * 3 + (limit,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT ts,operation,target,ok,detail FROM audit ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {"ts": ts, "operation": op, "target": target, "ok": bool(ok), "detail": detail}
            for ts, op, target, ok, detail in rows
        ]

    def export(self) -> str:
        return json.dumps(self.recent(1000), indent=2)
