"""Durable replies for an already-sent isolated DeskHand browser job."""
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from .runtime_paths import pad_data, root


def reply_stored(reply_id):
    if root() is None:
        raise ValueError('DeskHand isolation required')
    identity = uuid.UUID(reply_id)
    if identity.version != 4 or str(identity) != reply_id:
        raise ValueError('Invalid reply identity')
    path = Path(pad_data()) / 'browser-relay.sqlite3'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Reply mailbox unavailable')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='https_replies'").fetchone():
            return False
        return db.execute('SELECT 1 FROM https_replies WHERE reply_id=?', (reply_id,)).fetchone() is not None


def store_reply(reply_id, text):
    if root() is None:
        raise ValueError('DeskHand isolation required')
    identity = uuid.UUID(reply_id)
    if identity.version != 4 or str(identity) != reply_id:
        raise ValueError('Invalid reply identity')
    if not isinstance(text, str) or not text.strip() or len(text) > 240000:
        raise ValueError('Invalid reply text')
    path = Path(pad_data()) / 'browser-relay.sqlite3'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Reply mailbox unavailable')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=3)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS https_replies (reply_id TEXT PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL)')
        existing = db.execute('SELECT text FROM https_replies WHERE reply_id=?', (reply_id,)).fetchone()
        if existing is not None:
            if existing[0] != text:
                raise ValueError('Conflicting reply')
            return False
        job = db.execute('SELECT state, held_epoch FROM jobs WHERE reply_id=?', (reply_id,)).fetchone()
        if job is None or job[0] not in ('sending', 'sent', 'uncertain') or job[1] is not None:
            raise ValueError('Reply is not pending')
        db.execute('INSERT INTO https_replies VALUES (?,?,?)', (reply_id, text, time.time()))
        return True
