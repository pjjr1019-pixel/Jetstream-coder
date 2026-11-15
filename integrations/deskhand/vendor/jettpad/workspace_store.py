"""Local organization with version checks; never edits message bodies or grants."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS message_meta (
            id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '[]', version INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS metadata_operations (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS pinned_messages ON message_meta(pinned,id);
        CREATE TRIGGER IF NOT EXISTS metadata_insert AFTER INSERT ON message_meta BEGIN
            UPDATE chat_revision SET revision=revision+1 WHERE singleton=1;
            INSERT OR REPLACE INTO chat_changes SELECT NEW.id,revision FROM chat_revision WHERE singleton=1;
        END;
        CREATE TRIGGER IF NOT EXISTS metadata_update AFTER UPDATE ON message_meta
        WHEN OLD.version IS NOT NEW.version BEGIN
            UPDATE chat_revision SET revision=revision+1 WHERE singleton=1;
            INSERT OR REPLACE INTO chat_changes SELECT NEW.id,revision FROM chat_revision WHERE singleton=1;
        END;
        CREATE INDEX IF NOT EXISTS active_outbox ON outbox(state,route);
    ''')


def normalize_tags(tags):
    if not isinstance(tags,list) or len(tags)>8:raise ValueError('Use up to eight tags')
    result=[]
    for tag in tags:
        if not isinstance(tag,str) or not 1<=len(tag.strip())<=32 or any(ord(c)<32 for c in tag):
            raise ValueError('Tags must contain 1–32 printable characters')
        tag=tag.strip()
        if tag.casefold() not in {t.casefold() for t in result}:result.append(tag)
    return sorted(result,key=str.casefold)


def organize(folder, identifier, pinned, tags, expected_version, operation_id=None, authorize=None):
    if not isinstance(identifier,str) or not 1<=len(identifier)<=160:raise ValueError('Invalid message identity')
    if type(pinned) is not bool or type(expected_version) is not int or expected_version<0:raise ValueError('Invalid organization version')
    tags=normalize_tags(tags);operation_id=operation_id or str(uuid.uuid4())
    if str(uuid.UUID(operation_id))!=operation_id:raise ValueError('Invalid organization request ID')
    body={'id':identifier,'pinned':pinned,'tags':tags,'version':expected_version}
    digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    path=(Path(folder)/'pad-state.sqlite3').resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=1)) as db, db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
        if authorize is not None and not authorize():raise ValueError('Phone chat permission or identity changed')
        prior=db.execute('SELECT digest,receipt FROM metadata_operations WHERE id=?',(operation_id,)).fetchone()
        if prior:
            if prior['digest']!=digest:raise ValueError('Organization request ID reused with different content')
            return json.loads(prior['receipt'])
        if not db.execute("SELECT 1 FROM messages WHERE id=? AND status!='deferred_cloud'",(identifier,)).fetchone():
            raise ValueError('Message is unavailable')
        row=db.execute('SELECT pinned,tags,version FROM message_meta WHERE id=?',(identifier,)).fetchone()
        before={'pinned':bool(row['pinned']),'tags':json.loads(row['tags']),'version':row['version']} if row else {'pinned':False,'tags':[],'version':0}
        if before['version']!=expected_version:raise ValueError('Organization changed on another device; refresh and review')
        changed=before['pinned']!=pinned or before['tags']!=tags
        version=before['version']+int(changed)
        if changed:
            if row:
                db.execute('UPDATE message_meta SET pinned=?,tags=?,version=? WHERE id=?',
                           (int(pinned),json.dumps(tags),version,identifier))
            else:
                db.execute('INSERT INTO message_meta VALUES (?,?,?,?)',(identifier,int(pinned),json.dumps(tags),version))
        receipt={'id':identifier,'before':before,'after':{'pinned':pinned,'tags':tags,'version':version},'operation_id':operation_id}
        if authorize is not None and not authorize():raise ValueError('Phone chat permission or identity changed')
        db.execute('INSERT INTO metadata_operations VALUES (?,?,?)',(operation_id,digest,json.dumps(receipt)))
        return receipt


PROJECTION="SELECT m.*,m.rowid AS position,o.state AS delivery,ro.route AS reply_route,ro.requested_model AS reply_model,ro.codex_model AS reply_codex_model,ro.codex_effort AS reply_effort,COALESCE(x.pinned,0) AS pinned,COALESCE(x.tags,'[]') AS tags,COALESCE(x.version,0) AS meta_version FROM messages m LEFT JOIN outbox o ON o.id=m.id LEFT JOIN message_meta x ON x.id=m.id LEFT JOIN outbox ro ON ro.id=CASE WHEN m.id LIKE 'offline:%' THEN substr(m.id,9) ELSE NULL END "
