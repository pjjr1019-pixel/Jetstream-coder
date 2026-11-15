"""Receipt-based assistant action status; never submits or retries work."""
from contextlib import closing
from pathlib import Path
import sqlite3,time,threading

_STATES={'queued':'submitted','running':'running','done':'completed','failed':'failed','cancelled':'cancelled','uncertain':'unknown'}

def normalize(receipt,job_id):
    if not isinstance(receipt,dict) or receipt.get('id')!=job_id:
        return {'id':job_id,'state':'unknown','reason':'The existing job could not be confirmed.'}
    state=_STATES.get(receipt.get('status'),'unknown')
    reason=receipt.get('reason')
    steps=receipt.get('steps')
    completed=sum(1 for s in steps if isinstance(s,dict) and isinstance(s.get('result'),dict) and s['result'].get('ok') is True) if isinstance(steps,list) else None
    return {'id':job_id,'state':state,'reason':str(reason)[:300] if reason else None,'completed_steps':completed}

def format_status(rows):
    if not rows:return ''
    lines=['PC action status (executor job receipts; completion applies to the submitted job):']
    for row in rows:
        reason=' — '+row['reason'] if row.get('reason') else ''
        count='; '+str(row['completed_steps'])+' verified steps' if row.get('completed_steps') is not None else ''
        lines.append('Job '+row['id']+': '+row['state']+count+reason)
    if any(row['state']=='unknown' for row in rows):lines.append('Check the existing job before retrying; its outcome is unknown.')
    return '\n'.join(lines)

def query_turn(data,turn_id,broker_factory=None):
    ledger=Path(data)/'assistant-executor.sqlite3'
    if not ledger.is_file():return []
    from assistant_executor import AssistantExecutor
    from musebridge.cli import default_home
    with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        calls=list(db.execute('SELECT call_id,job_id FROM calls WHERE turn_id=? ORDER BY rowid',(turn_id,)))
    if not calls:return []
    broker=(broker_factory or AssistantExecutor)(default_home(),ledger)
    rows=[]
    for call_id,job_id in calls:
        try:rows.append(normalize(broker.status(turn_id,call_id),job_id))
        except Exception:rows.append(normalize(None,job_id))
    return rows

def publish(store,turn_id,rows):
    text=format_status(rows)
    if not text:return
    identity='action-status:'+turn_id
    with store.lock,store.db:
        exists=store.db.execute('SELECT text FROM messages WHERE id=?',(identity,)).fetchone()
        if exists and exists[0]!=text:store.db.execute('UPDATE messages SET text=? WHERE id=?',(text,identity))
        elif not exists:store.db.execute("INSERT INTO messages(id,ts,sender,text,status,logged) VALUES (?,?,'system',?,'complete',0)",(identity,time.time(),text))
    store.queue_changed.set()

def reporter(store,turn_id):
    rows={};lock=threading.Lock()
    def update(job_id,receipt):
        with lock:
            rows[job_id]=normalize(receipt,job_id)
            publish(store,turn_id,list(rows.values()))
    return update
