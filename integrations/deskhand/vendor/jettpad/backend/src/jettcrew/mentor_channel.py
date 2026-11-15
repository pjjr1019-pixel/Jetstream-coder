"""A deterministic notification on the existing browser queue, never a second consumer."""
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from contextlib import closing


def register_pad_outbox(data,identity,message,stamp):
    """Use Pad's existing completion observer, even if crew is later stopped."""
    with closing(sqlite3.connect(Path(data)/'pad-state.sqlite3',timeout=5)) as db:
        with db:
            db.execute("insert or ignore into messages(id,ts,sender,text,status,logged) values (?,?, 'phil',?,'complete',0)",(identity,stamp,message))
            db.execute("insert or ignore into outbox(id,state,route) values (?,'queued','browser')",(identity,))


def notify(task_id,round_number,packet_path,reply_path):
    pad=Path(__file__).resolve().parents[3]
    sys.path.insert(0,str(pad))
    from browser_mailbox import BrowserMailbox
    identity=str(uuid.uuid5(uuid.NAMESPACE_URL,'jett-crew-mentor:'+task_id+':'+str(round_number)))
    message=('LOCAL CREW MENTOR REQUEST — origin=crew_mentor; task_id='+task_id+'; round='+str(round_number)+'. '
        'This is a backend notification, NOT a diagnosis or message authored by Jett. '
        'Read the genuine stuck packet at '+str(packet_path)+'. Local attempts failed; tests and exact errors are preserved. '
        'Please supply YOUR own diagnosis and reply using bridge fs.write at '+str(reply_path)+'. '
        'Write JSON with kind="mentor_reply", task_id="'+task_id+'", diagnosis, fix_steps (array of strings), '
        'code_skeleton (string or null), pitfalls (array of strings), round='+str(round_number)+'. '
        'Do not edit the target yourself, run tests yourself, change any local approval, or restart anything. '
        'The local model implements your advice and the approved test harness decides the outcome. '
        'Send a brief normal correlated Pad acknowledgement too so browser delivery can finish.')
    box=BrowserMailbox(Path(os.environ['LOCALAPPDATA'])/'JettPad')
    try:
        job=box.enqueue({'id':identity,'ts':time.time(),'text':message})
        register_pad_outbox(Path(os.environ['LOCALAPPDATA'])/'JettPad',identity,message,job['ts'])
        return {'origin':'crew_mentor','request_id':identity,'reply_id':job['reply_id']}
    finally:box.close()


def bridge_provenance(path,mtime,not_before):
    audit=Path(os.environ['LOCALAPPDATA'])/'MuseBridge/audit.sqlite3'
    with closing(sqlite3.connect('file:'+str(audit)+'?mode=ro',uri=True,timeout=2)) as db:
        rows=db.execute("select rowid,ts,target from audit where operation='fs.write' and ok=1 and ts>=? order by ts desc limit 100",(not_before,)).fetchall()
    for identity,stamp,target in rows:
        if Path(target).resolve()==Path(path).resolve() and abs(stamp-mtime)<3:
            return {'audit_rowid':identity,'bridge_write_ts':stamp,'path':str(path)}
    return None
