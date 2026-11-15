"""A voice producer for the existing Pad queue, never a queue consumer."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from contextlib import closing
from .common import atomic


def submit(text, request_id, documents, pad, emit):
    text = text.strip()
    if not text:
        raise ValueError('Empty post-wake transcript')
    heard = dict(contract='1.1', request_id=request_id, ts=time.time(), text=text, status='captured')
    target = Path(documents) / 'jett-last-heard.json'
    atomic(target, heard)
    sys.path.insert(0, str(pad))
    from jettcrew.mentor_channel import register_pad_outbox
    data = Path(os.environ['LOCALAPPDATA']) / 'JettPad'
    # Idempotent INSERT OR IGNORE with one request id. Pad alone delivers browser jobs.
    register_pad_outbox(data, request_id,
        'origin=voice; conversation=voice; request_id=' + request_id + '\n' + text, heard['ts'])
    heard.update(status='queued', ts=time.time())
    atomic(target, heard)
    emit('event', event='voice.transcript_queued', reason='Text only; private Muse browser; no bridge chat_request', ref=request_id)
    return request_id


def known_reason(side, documents, pad_data):
    """A request must exist; proactive refs must identify canonical evidence."""
    request = side.get('request_id')
    if request:
        try:
            with closing(sqlite3.connect('file:' + str(Path(pad_data) / 'pad-state.sqlite3') + '?mode=ro', uri=True)) as db:
                return db.execute("SELECT 1 FROM messages WHERE id=? AND sender='phil'", (request,)).fetchone() is not None
        except sqlite3.Error:
            return False
    ref = side.get('reason_ref')
    if not isinstance(ref, str) or not ref or '/' in ref or '\\' in ref or ref in {'.', '..'}:
        return False
    if ref.startswith('event:'):
        # Facts emitted by the actual PC collector, bounded tail read only.
        path=Path(documents)/'jett-desktop-events.jsonl'
        try:
            with path.open('rb') as stream:
                stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-4*1024*1024))
                if size>4*1024*1024:stream.readline()
                for line in stream:
                    row=json.loads(line)
                    if row.get('id')==ref[6:] and row.get('contract')=='1.1':return True
        except (OSError,ValueError):pass
        return False
    # A reminder source has not been built; do not invent a due reminder.
    target = Path(documents) / 'jett-crew/out' / (ref + '.json')
    try:
        row = json.loads(target.read_text())
        return row.get('id') == ref and row.get('status') == 'done'
    except (OSError, ValueError):
        return False


def refresh_heard(documents, pad_data):
    path = Path(documents) / 'jett-last-heard.json'
    try:
        heard = json.loads(path.read_text())
        if heard['status'] not in {'queued', 'sent'}:
            return
        with closing(sqlite3.connect('file:' + str(Path(pad_data) / 'browser-relay.sqlite3') + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT state FROM jobs WHERE id=?', (heard['request_id'],)).fetchone()
        if row:
            status = 'uncertain' if row[0] == 'uncertain' else 'sent' if row[0] in {'sent', 'complete', 'completed', 'awaiting_reply'} else heard['status']
            if status != heard['status']:
                atomic(path, {**heard, 'status': status, 'ts': time.time()})
    except (OSError, ValueError, sqlite3.Error, KeyError):
        return
