"""Convert a correlated Pad Notepad-open action into a durable chat receipt."""
from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import time
import uuid
from .runtime_paths import documents as runtime_documents, pad_data


def recover_written_pad_replies(permissions, *, documents=None, data=None):
    """Resume interrupted render-only delivery; never resend or execute anything."""
    docs=Path(documents or runtime_documents())
    mailbox=Path(data or pad_data())/'browser-relay.sqlite3'
    if not mailbox.is_file():return []
    with closing(sqlite3.connect(mailbox.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        rows=db.execute("SELECT reply_id FROM jobs WHERE state IN ('sent','uncertain') LIMIT 100").fetchall()
    recovered=[]
    for (identity,) in rows:
        try:
            if str(uuid.UUID(identity))!=identity:continue
            source=docs/'jett-pad-inbox'/(identity+'.txt')
            if source.with_suffix('.json').exists() or not source.is_file():continue
            if time.time()-source.stat().st_mtime<2:continue
            result=deliver_notepad_reply(f'notepad.exe "{source}"',permissions,documents=docs,data=data,recovered=True)
            if result:recovered.append(result['pad_reply'])
        except (ValueError,OSError,sqlite3.Error):continue
    return recovered


def deliver_written_pad_reply(path, permissions):
    """Publish a completed correlated text write without another cloud turn.

    This only renders an answer; it never executes a command. The later
    Notepad action uses the same exclusive receipt and remains idempotent.
    An ordinary/unmatched file write keeps its original filesystem result.
    """
    source = Path(path)
    inbox = runtime_documents() / 'jett-pad-inbox'
    if source.suffix != '.txt' or not source.is_absolute() or source.parent.resolve() != inbox.resolve():
        return None
    try:
        result = deliver_notepad_reply(f'notepad.exe "{source}"', permissions)
        return result['pad_reply'] if result else None
    except (ValueError, OSError, sqlite3.Error):
        # Read/write scope, UUID, sent-job correlation and file checks are
        # enforced by the common delivery path. Never weaken them on writes.
        return None


def deliver_notepad_reply(command, permissions, *, documents=None, data=None,recovered=False):
    match = re.fullmatch(r'notepad(?:\.exe)?\s+"([^"\r\n]+)"\s*', command, re.IGNORECASE)
    if not match:
        if 'notepad' in command.casefold() and 'jett-pad-inbox' in command.casefold():
            raise ValueError('Pad replies require the direct notepad.exe "absolute-file-path" command')
        return None
    source = Path(match[1])
    inbox = Path(documents or runtime_documents()) / 'jett-pad-inbox'
    # Only this reserved reply directory changes Notepad behavior.
    if not source.is_absolute() or source.parent.resolve() != inbox.resolve():
        return None
    try:
        identity = str(uuid.UUID(source.stem))
    except ValueError:
        raise ValueError('Invalid Pad reply filename') from None
    if source.stem != identity or source.suffix != '.txt':
        raise ValueError('Invalid Pad reply filename')
    target = inbox / (identity + '.json')
    if source.is_symlink() or inbox.is_symlink() or target.is_symlink():
        raise ValueError('Linked Pad reply files are not accepted')
    permissions.authorize(str(source), 'read')
    permissions.authorize(str(target), 'write')
    mailbox = Path(data or pad_data()) / 'browser-relay.sqlite3'
    if not mailbox.is_file():
        raise ValueError('No browser Pad reply is pending')
    with closing(sqlite3.connect(mailbox.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as connection:
        job = connection.execute('SELECT id,state FROM jobs WHERE reply_id=?', (identity,)).fetchone()
    if not job or job[1] not in ('sending', 'sent', 'uncertain', 'complete'):
        raise ValueError('This Pad reply is not associated with a sent browser prompt')
    with source.open('rb') as stream:
        stat = os.fstat(stream.fileno())
        if stat.st_nlink != 1 or stat.st_size > 1024 * 1024:
            raise ValueError('Invalid Pad reply file')
        raw = stream.read(1024 * 1024 + 1)
    text = raw.decode('utf-8-sig')
    if not text.strip() or len(text) > 240000:
        raise ValueError('Pad reply must contain 1 to 240000 characters')
    row = {'id': identity, 'ts': time.time(), 'from': 'jett', 'text': text}
    if recovered:
        row.update(ts=stat.st_mtime,recovered=True,recovered_at=time.time())
    permissions.authorize(str(target), 'write')
    # Retries cannot replace an answer or create another visible message.
    try:
        with target.open('x', encoding='utf-8') as stream:
            json.dump(row, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if target.stat().st_nlink != 1 or target.stat().st_size > 1024 * 1024:
            raise ValueError('Invalid existing Pad receipt')
        existing = json.loads(target.read_text(encoding='utf-8-sig'))
        if any(existing.get(key) != row[key] for key in ('id', 'from', 'text')):
            raise ValueError('A different Pad answer was already delivered') from None
    return {'ok': True, 'stdout': 'Reply delivered to Jett Pad.', 'stderr': '', 'exit_code': 0,
            'pad_reply': {'id': identity, 'request_id': job[0], 'notepad_opened': False}}
