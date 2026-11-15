"""Local-only companion mailbox. Never opens a network listener."""
from pathlib import Path
from contextlib import closing
import hashlib, json, os, secrets, sqlite3, time, uuid

from tailscale_addresses import PHONE_TAILSCALE_IP as PHONE  # kept for musebridge/companion.py
def home():
    from musebridge.runtime_paths import pad_data
    return pad_data()
def atomic(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+secrets.token_hex(8)+'.tmp')
    try:
        temp.write_text(json.dumps(value),encoding='utf-8')
        for attempt in range(5):
            try: os.replace(temp,path); return
            except PermissionError:
                if attempt==4: raise
                time.sleep(.02)
    finally: temp.unlink(missing_ok=True)
def settings():
    try: value=json.loads((home()/'companion-settings.json').read_text())
    except (OSError,ValueError): value={}
    return {'chat':value.get('chat') is True,'remote_start':value.get('remote_start') is True,'away_access':value.get('away_access') is True}
def stream_root():
    from musebridge.runtime_paths import root
    if root() is not None:
        raise ValueError('JettPad streaming is not available in Deskhand')
    return Path.home()/'Desktop/JettPad/sunshine'
def paired_certificates():
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    state=json.loads((stream_root()/'private/sunshine-state.json').read_text())
    result={}
    for row in state.get('root',{}).get('named_devices',[]):
        enabled=row.get('enabled',True)
        if enabled is not True and enabled!='true': continue
        cert=x509.load_pem_x509_certificate(row['cert'].encode())
        raw=cert.public_bytes(serialization.Encoding.DER)
        key=hashlib.sha256(raw).hexdigest()
        if key in result: raise ValueError('Duplicate phone identity; local review required')
        result[key]=(cert,raw)
    return result
def request(identifier, kind, payload, fingerprint, *, created=None):
    if str(uuid.UUID(identifier))!=identifier: raise ValueError('A dashed UUID is required')
    from companion_queue import admit
    admit(home(), {'id':identifier,'kind':kind,'payload':payload,'phone':fingerprint,
                   'created':time.time() if created is None else created})

def pending_requests(kinds, limit=64):
    from companion_queue import pending
    return pending(home(), kinds, limit)

def complete_request(identifier, value):
    from companion_queue import finish
    atomic(home()/'companion-results'/(identifier+'.json'), value)
    finish(home(), identifier)

def messages():
    path=home()/'pad-state.sqlite3'
    if not path.exists():return []
    from workspace_store import PROJECTION
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        db.row_factory=sqlite3.Row
        rows=[dict(r) for r in db.execute(PROJECTION+"WHERE m.status!='deferred_cloud' ORDER BY m.ts DESC,m.rowid DESC LIMIT 100")][::-1]
    for row in rows:
        row.pop('logged',None);row.pop('position',None)
        row['pinned']=bool(row['pinned']);row['tags']=json.loads(row['tags'])
    return rows

def consume_chat(store):
    for path,value in pending_requests(('chat.send',)):
        try:
            if value.get('kind')!='chat.send':continue
            result=home()/'companion-results'/(value['id']+'.json')
            if result.exists():continue
            if value['phone'] not in paired_certificates() or not settings()['chat']:continue
            store.submit_once(value['id'],value['payload']['text'])
            complete_request(value['id'],{'ok':True})
            # Keep the canonical request as the durable duplicate-content check.
        except (OSError,ValueError,KeyError):continue
