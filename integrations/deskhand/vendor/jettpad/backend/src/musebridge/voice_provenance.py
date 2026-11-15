"""Register only authenticated upload commits; keep immutable verified audio bytes.

Called by the authenticated bridge dispatcher, never by fs.write or the crew.
The registry and content store live outside the remotely writable Documents scope.
"""
import hashlib
from pathlib import Path
import sqlite3
import time
from contextlib import closing
from .runtime_paths import documents


def register(home, result, request_id, inbox=None):
    inbox = Path(inbox or documents() / 'jett-voice-inbox').resolve()
    path = Path(result['committed_path']).resolve()
    if path.parent != inbox or path.suffix.lower() not in {'.mp3', '.json'}:
        return None
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if len(content) != result['size'] or digest != result['sha256']:
        raise ValueError('Committed voice file changed before provenance registration')
    root = Path(home) / 'voice-provenance'
    root.mkdir(parents=True, exist_ok=True)
    target = root / (digest + path.suffix.lower())
    if not target.exists():
        with target.open('xb') as f:
            f.write(content)
            f.flush()
            import os
            os.fsync(f.fileno())
    # Identical content is safe to reuse only after verifying the stored bytes too.
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise ValueError('Voice provenance content store checksum mismatch')
    with closing(sqlite3.connect(root / 'commits.sqlite3', timeout=5)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS commits(path TEXT PRIMARY KEY, sha256 TEXT, '
                   'size INTEGER, request_id TEXT, committed_ts REAL, snapshot TEXT)')
        with db:
            db.execute('INSERT OR REPLACE INTO commits VALUES (?,?,?,?,?,?)',
                       (str(path), digest, len(content), request_id, time.time(), str(target)))
    return {'sha256': digest, 'upload_request_id': request_id, 'registered': True}


def resolve(home, path):
    root = Path(home) / 'voice-provenance'
    if not (root / 'commits.sqlite3').exists():
        raise ValueError('No authenticated voice upload commit')
    with closing(sqlite3.connect('file:' + str(root / 'commits.sqlite3') + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT sha256,size,snapshot FROM commits WHERE path=?',
                         (str(Path(path).resolve()),)).fetchone()
    if not row:
        raise ValueError('No authenticated voice upload commit')
    snapshot = Path(row[2])
    if snapshot.parent.resolve() != root.resolve():
        raise ValueError('Invalid provenance snapshot')
    content = snapshot.read_bytes()
    if len(content) != row[1] or hashlib.sha256(content).hexdigest() != row[0]:
        raise ValueError('Voice snapshot checksum mismatch')
    return content, row[0]
