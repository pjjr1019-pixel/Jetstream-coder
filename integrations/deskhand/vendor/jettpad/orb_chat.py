"""Private file mailbox. Only the existing Pad owner writes its message store."""
from pathlib import Path
import hashlib,json,os,uuid,threading

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('w',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)

def read(path,limit=524288):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>limit:raise ValueError('Invalid orb-chat file')
    return json.loads(path.read_text(encoding='utf-8'))

class OrbChatBroker:
    """Instantiated by PadEngine only; never creates another PadStore/browser owner."""
    def __init__(self,data,store,mailbox):
        self.root=Path(data)/'orb-chat';self.store=store;self.mailbox=mailbox;self.signature=None
        for folder in ('requests','receipts'):(self.root/folder).mkdir(parents=True,exist_ok=True)
    def consume(self):
        import sqlite3
        candidates=[]
        for path in sorted((self.root/'requests').glob('*.json')):
            try:valid=str(uuid.UUID(path.stem))==path.stem
            except ValueError:valid=False
            if valid and not (self.root/'receipts'/path.name).exists():candidates.append(path)
            if len(candidates)>=20:break
        for path in candidates:
            if path.is_symlink():continue
            identity=path.stem
            try:
                if str(uuid.UUID(identity))!=identity:continue
                receipt=self.root/'receipts'/path.name
                if receipt.exists():continue
                row=read(path,110000)
                if not isinstance(row,dict) or row.get('id')!=identity or not isinstance(row.get('text'),str):raise ValueError('Invalid orb message')
                self.store.submit_once(identity,row['text'],row.get('model'),row.get('codex_model'),row.get('codex_effort'))
                atomic(receipt,{'id':identity,'ok':True})
            except (ValueError,TypeError) as error:atomic(self.root/'receipts'/path.name,{'id':identity,'ok':False,'error':str(error)[:160]})
            except (OSError,RuntimeError,sqlite3.Error):continue # Keep request/draft for durable retry.
    def publish(self,status):
        with self.store.lock:
            replies_rows=[dict(r) for r in self.store.db.execute("SELECT * FROM messages WHERE (sender IN ('jett','offline') OR (sender='system' AND (id LIKE 'image:%' OR id LIKE 'action-status:%' OR id LIKE 'handoff-status:%'))) AND status='complete' ORDER BY ts DESC,rowid DESC LIMIT 4")][::-1]
            outgoing_rows=[dict(r) for r in self.store.db.execute("SELECT m.*,o.state AS delivery,o.requested_model FROM messages m LEFT JOIN outbox o ON m.id=o.id WHERE sender='phil' ORDER BY ts DESC,m.rowid DESC LIMIT 64")][::-1]
        with self.store.lock:
            metadata={}
            for reply in replies_rows:
                if reply['id'].startswith('offline:'):
                    item=self.store.db.execute('SELECT route,requested_model,codex_model,codex_effort FROM outbox WHERE id=?',(reply['id'][8:],)).fetchone()
                    if item:metadata[reply['id']]=dict(item)
        rows=replies_rows+outgoing_rows
        with self.mailbox.lock:
            links={r['reply_id']:r['id'] for r in self.mailbox.db.execute('SELECT id,reply_id FROM jobs ORDER BY ts DESC LIMIT 512')}
        replies=[]
        for row in rows:
            if row['sender'] not in ('jett','offline','system') or row['status']!='complete':continue
            parent=links.get(row['id'])
            if row['sender']=='offline' and row['id'].startswith('offline:'):parent=row['id'][8:]
            if row['sender']=='system' and row['id'].startswith('image:'):parent=row['id'][6:]
            meta=metadata.get(row['id'],{})
            replies.append({k:row.get(k) for k in ('id','ts','sender','status')}|{'text':row['text'],'reply_to':parent,'reply_route':meta.get('route'),'reply_model':meta.get('requested_model'),'reply_codex_model':meta.get('codex_model'),'reply_effort':meta.get('codex_effort')})
        outgoing=[{k:row.get(k) for k in ('id','ts','sender','delivery','requested_model')}|{'text':row['text'][:800]} for row in rows if row['sender']=='phil'][-64:]
        snapshot={'replies':replies[-4:],'outgoing':outgoing,'status':str(status)[:240]}
        signature=hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest()
        if signature!=self.signature:atomic(self.root/'snapshot.json',snapshot);self.signature=signature

class OrbChatService:
    """Lifecycle owned by Pad; shares its existing store, never constructs another."""
    def __init__(self,engine):
        self.engine=engine;self.broker=OrbChatBroker(engine.data,engine.store,engine.mailbox);self.stopping=threading.Event();self.error=None
        self.thread=threading.Thread(target=self.run,name='Jett orb presentation mailbox',daemon=True)
    def start(self):self.thread.start()
    def run(self):
        while not self.stopping.is_set():
            try:
                self.broker.consume();self.broker.publish(self.engine.offline.status() if self.engine.offline else self.engine.browser_status);self.error=None
            except Exception:self.error='Orb chat unavailable; messages and drafts retained'
            self.stopping.wait(.1)
    def close(self):
        self.stopping.set()
        if self.thread.is_alive():self.thread.join(timeout=6)
