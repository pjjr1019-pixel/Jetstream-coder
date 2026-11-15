"""Read-only Pad queue observations for the single existing body-state publisher."""
from pathlib import Path
import sqlite3,time

TERMINAL={'browser_complete','browser_abandoned','offline_complete','offline_failed','offline_interrupted'}

def compose(base,chat,connected,audio_fault=None,listening=False,playing=False):
    if not connected:return 'asleep'
    if audio_fault:return 'error'
    if listening:return 'listening'
    if playing:return 'speaking'
    if base=='error':return base
    return chat or base

class ChatActivity:
    def __init__(self,data,clock=time.time):
        self.data=Path(data);self.clock=clock;self.db=None;self.initialized=False
        self.last_terminal=None;self.pulse_until=0.;self.pulse=None
    def close(self):
        if self.db:self.db.close();self.db=None
    def connect(self):
        if self.db:return
        db=sqlite3.connect((self.data/'pad-state.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=.02,isolation_level=None,check_same_thread=False)
        try:
            db.row_factory=sqlite3.Row
            db.execute('ATTACH DATABASE ? AS relay',((self.data/'browser-relay.sqlite3').resolve().as_uri()+'?mode=ro',))
            db.execute('PRAGMA query_only=ON')
        except Exception:db.close();raise
        self.db=db
    def poll(self):
        now=self.clock()
        try:
            self.connect()
            rows=self.db.execute("""SELECT m.id,m.ts,o.route,o.state,j.state AS relay_state,
                j.updated,j.held_epoch,r.id AS answer_id,r.ts AS answer_ts,r.sender AS answer_from
                FROM messages m JOIN outbox o ON o.id=m.id
                LEFT JOIN relay.jobs j ON j.id=m.id
                LEFT JOIN messages r ON r.id=CASE WHEN o.route='offline' THEN 'offline:'||m.id ELSE j.reply_id END
                    AND r.status='complete' AND r.sender IN ('jett','offline')
                WHERE m.sender='phil' AND o.route IN ('browser','offline')
                ORDER BY CASE WHEN o.state IN ('browser_complete','browser_abandoned','offline_complete','offline_failed','offline_interrupted') THEN 1 ELSE 0 END,
                m.ts DESC LIMIT 128""").fetchall()
            status=self.db.execute('SELECT code,seen FROM relay.status WHERE id=1').fetchone()
        except (OSError,sqlite3.Error):
            self.close()
            return dict(state=None,intensity=0,source=None,ref=None,reason='Chat activity unavailable; no activity inferred')
        finished=[r for r in rows if r['answer_id'] or r['state'] in TERMINAL]
        latest=max(finished,key=lambda r:r['answer_ts'] or r['updated'] or r['ts'],default=None)
        key=(latest['id'],latest['answer_id'],latest['state']) if latest else None
        if self.initialized and key and key!=self.last_terminal:
            failed=latest['state'] in ('offline_failed','offline_interrupted')
            if latest['answer_id'] or failed:
                self.pulse=dict(state='error' if failed else 'done_pulse',intensity=30 if failed else 65,
                    source=latest['route'],ref=latest['id'],reason='Local reply failed' if failed else 'Correlated reply saved in Pad')
                self.pulse_until=now+2
        self.initialized=True;self.last_terminal=key
        pending=[r for r in rows if not r['answer_id'] and r['state'] not in TERMINAL]
        # An already-sent request takes precedence over a newer queued prompt.
        pending.sort(key=lambda r:(0 if r['relay_state'] in ('sending','sent') or r['state']=='offline_running' else 1,r['ts']))
        if pending:
            row=pending[0];phase=row['relay_state'] or row['state'];source=row['route']
            state='thinking';reason='Pad accepted a prompt; awaiting '+('local reply' if source=='offline' else 'Jett reply')
            if phase=='uncertain' or row['held_epoch'] is not None or row['state']=='uncertain':
                state='needs_phil';reason='Interrupted send held for review; no automatic resend'
            elif source=='browser':
                code=status['code'] if status and now-status['seen']<12 else 'offline'
                if phase=='queued' and code in ('draft','needs_pair','wrong_chat','layout'):
                    state='needs_phil';reason='Browser queue requires local attention: '+code
                elif phase=='queued' and code!='ready':
                    state='blocked';reason='Prompt queued; browser waiting: '+code
                elif phase in ('sent','sending') and now-(row['updated'] or row['ts'])>45:
                    state='blocked';reason='Prompt sent; waiting for the external reply'
            return dict(state=state,intensity=52 if state=='thinking' else 25,source=source,ref=row['id'],reason=reason)
        if self.pulse and now<self.pulse_until:return self.pulse
        return dict(state=None,intensity=0,source=None,ref=None,reason='No unanswered Pad prompts')
