"""Indexed active companion work; canonical JSON and terminal receipts survive.

Legacy directories are reconciled once per process, never on every poll.
The SQLite transaction serializes same-ID admissions across bridge processes.
"""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import threading
import time
import uuid

_ready = set()
_initializing = threading.RLock()


def connect(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder/'companion-queue.sqlite3', timeout=5)
    db.execute('PRAGMA busy_timeout=5000')
    return db


def initialize(folder):
    folder = Path(folder)
    key = str(folder.resolve())
    with _initializing:
        if key in _ready:
            return
        with closing(connect(folder)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL,
                    body TEXT NOT NULL, state TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS companion_pending
                    ON requests(kind,created,id) WHERE state='pending';
            ''')
            for path in (folder/'companion-requests').glob('*.json'):
                if (folder/'companion-results'/path.name).exists():
                    continue
                if db.execute('SELECT 1 FROM requests WHERE id=?',(path.stem,)).fetchone():
                    continue
                try:
                    value=json.loads(path.read_text(encoding='utf-8'))
                    if str(uuid.UUID(value['id'])) != value['id'] or path.stem != value['id']:
                        continue
                    if value['kind'] not in ('chat.send','stream.start','stream.stop','stream.pause'):
                        continue
                    if not isinstance(value['payload'],dict) or not isinstance(value['phone'],str):
                        continue
                    db.execute('INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?)',
                               (value['id'],value['kind'],value['created'],json.dumps(value),'pending'))
                except (OSError,ValueError,KeyError,TypeError):
                    continue
        _ready.add(key)


def _read_evidence(path, attempts=10):
    # A concurrent writer may be mid-write; retry briefly, then give up.
    for _ in range(attempts):
        try:return json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):time.sleep(.02)
    return None


def admit(folder, value):
    folder=Path(folder); initialize(folder)
    root=folder/'companion-requests';root.mkdir(exist_ok=True)
    path=root/(value['id']+'.json')
    with closing(connect(folder)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT body FROM requests WHERE id=?',(value['id'],)).fetchone()
        prior=json.loads(row[0]) if row else _read_evidence(path) if path.exists() else None
        if prior is not None:
            if any(prior.get(k)!=value[k] for k in ('id','kind','payload','phone')):
                raise ValueError('Request ID reused with different content')
            value=prior
        else:
            # Retain the original immutable duplicate-content evidence.
            try:
                with path.open('x',encoding='utf-8') as output:
                    json.dump(value,output);output.flush();os.fsync(output.fileno())
            except FileExistsError:
                # Another process admitted the same id between the check above
                # and the exclusive create; re-read its evidence instead of failing.
                prior=_read_evidence(path)
                if prior is None or any(prior.get(k)!=value[k] for k in ('id','kind','payload','phone')):
                    raise ValueError('Request ID reused with different content')
                value=prior
        state='done' if (folder/'companion-results'/path.name).exists() else 'pending'
        db.execute('INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?)',
                   (value['id'],value['kind'],value['created'],json.dumps(value),state))


def pending(folder, kinds, limit=64):
    folder=Path(folder); initialize(folder)
    kinds=tuple(kinds)
    if not kinds:return []
    with closing(connect(folder)) as db, db:
        rows=db.execute("SELECT id,body FROM requests WHERE state='pending' AND kind IN ("+
                        ','.join('?' for _ in kinds)+") ORDER BY created,id LIMIT ?",(*kinds,limit)).fetchall()
        active=[]
        for identifier,body in rows:
            # Recovery for a crash between durable result and ledger update.
            if (folder/'companion-results'/(identifier+'.json')).exists():
                db.execute("UPDATE requests SET state='done' WHERE id=?",(identifier,))
            else:
                active.append((folder/'companion-requests'/(identifier+'.json'),json.loads(body)))
        return active


def finish(folder, identifier):
    folder=Path(folder);initialize(folder)
    with closing(connect(folder)) as db, db:
        db.execute("UPDATE requests SET state='done' WHERE id=?",(identifier,))
