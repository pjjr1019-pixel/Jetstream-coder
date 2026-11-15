"""Content-bounded phone chat deltas; no permission or execution authority."""
import hashlib
import json
import sqlite3
from contextlib import closing


def validate_known(value):
    if not isinstance(value,dict) or len(value)>200:
        raise ValueError('Invalid chat sync state')
    if any(not isinstance(k,str) or len(k)>160 or not isinstance(v,str) or len(v)!=64 for k,v in value.items()):
        raise ValueError('Invalid chat revision')
    return dict(value)


def delta(rows, known):
    changed=[];size=0;next_known={}
    for row in rows:
        revision=hashlib.sha256(json.dumps(row,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if known.get(row['id'])==revision:
            next_known[row['id']]=revision
            continue
        if changed and size+len(row['text'])>200000:
            # Retain only acknowledged revisions, not unsent newer ones.
            if row['id'] in known:next_known[row['id']]=known[row['id']]
            continue
        changed.append(dict(row,revision=revision));size+=len(row['text'])
        next_known[row['id']]=revision
    return changed,next_known


def revision(folder):
    path=folder/'pad-state.sqlite3'
    if not path.exists():return None
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
            row=db.execute('SELECT revision FROM chat_revision WHERE singleton=1').fetchone()
            return row[0] if row else None
    except sqlite3.OperationalError:
        return None  # compatibility with a Pad that has not migrated yet
