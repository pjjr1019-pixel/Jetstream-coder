"""Durable HTTPS request identities; never automatically replay a side effect."""
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class HttpsJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, kind TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL, receipt TEXT)')
            db.execute("UPDATE requests SET state='uncertain', updated=? WHERE state IN ('accepted','running')", (time.time(),))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, identifier, kind, payload):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        with self.lock, self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT kind, fingerprint, state, receipt FROM requests WHERE id=?', (identifier,)).fetchone()
            if row:
                if row[0] != kind or row[1] != fingerprint:
                    raise ValueError('Request identity reused with different content')
                return {'new': False, 'state': row[2], 'receipt': json.loads(row[3]) if row[3] else None}
            if db.execute('SELECT count(*) FROM requests').fetchone()[0] >= 100000:
                raise ValueError('HTTPS request history full; local maintenance required')
            db.execute("INSERT INTO requests VALUES (?, ?, ?, 'accepted', ?, NULL)", (identifier, kind, fingerprint, time.time()))
            return {'new': True, 'state': 'accepted', 'receipt': None}

    def transition(self, identifier, expected, state, receipt=None):
        if expected not in {'accepted', 'running'} or state not in {'running', 'complete', 'failed', 'cancelled', 'uncertain'}:
            raise ValueError('Invalid request transition')
        # Persist metadata only; request text, screenshots and action output are
        # not retained in this journal.
        allowed = {'job_id', 'code', 'ok', 'response_type', 'cancelled'}
        if receipt is not None and (not isinstance(receipt, dict) or set(receipt) - allowed):
            raise ValueError('Unsupported receipt metadata')
        serialized = json.dumps(receipt, separators=(',', ':'), allow_nan=False) if receipt is not None else None
        if serialized is not None and len(serialized.encode()) > 16384:
            raise ValueError('Receipt too large')
        with self.lock, self._connect() as db:
            result = db.execute('UPDATE requests SET state=?, updated=?, receipt=? WHERE id=? AND state=?',
                                (state, time.time(), serialized, identifier, expected))
            return result.rowcount == 1

    def get(self, identifier):
        with self.lock, self._connect() as db:
            row = db.execute('SELECT kind, state, updated, receipt FROM requests WHERE id=?', (identifier,)).fetchone()
        if not row:
            return None
        return {'id': identifier, 'kind': row[0], 'state': row[1], 'updated': row[2],
                'receipt': json.loads(row[3]) if row[3] else None}
