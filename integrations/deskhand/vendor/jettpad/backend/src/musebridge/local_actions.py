"""Bounded, local-only app launch broker. Never accepts executable paths or code."""
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid

APPS = {
    'calculator': ('System32/calc.exe', {'calculator.exe','calculatorapp.exe'}),
    'notepad': ('System32/notepad.exe', {'notepad.exe'}),
    'explorer': ('explorer.exe', {'explorer.exe'}),
    'settings': ('explorer.exe', {'systemsettings.exe'}),
}


def parse_launch(text):
    match = re.fullmatch(r'\s*(?:(?:please|can you|could you|would you)\s+)?(?:open|launch|start)\s+(?:up\s+)?(?:the\s+)?(calculator|calc|notepad|file explorer|explorer|windows settings|settings)(?:\s+please)?[.!?]*\s*', text, re.I)
    if not match:return None
    name=match[1].lower()
    return {'calc':'calculator','file explorer':'explorer','windows settings':'settings'}.get(name,name)


class LocalAppBroker:
    def __init__(self, bridge, pad_data, *, launch=subprocess.Popen, windows=None, secure=None, presence=None):
        self.bridge,self.pad_data,self.launch=bridge,Path(pad_data),launch
        self.windows=Path(windows or os.environ.get('SystemRoot',r'C:\Windows'))
        self.secure=secure or self._secure
        self.presence=presence or self._presence
        self.path=bridge.data_dir/'offline-action-receipts.sqlite3'
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, app TEXT, status TEXT, ts REAL, detail TEXT, epoch INTEGER)')

    def _secure(self):
        from .window_targets import desktop_protection
        return desktop_protection()

    def _presence(self, app):
        return any(row['process_name'].lower() in APPS[app][1] and not row['protected'] for row in self.bridge.desktop.list_windows()['windows'])

    def policy(self):
        path=self.bridge.data_dir/'offline-actions.json'
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=(path.with_suffix('.sha256')).read_text(encoding='ascii').strip():
            raise ValueError('Offline actions approval checksum mismatch')
        value=json.loads(raw)
        if value.get('schema_version')!=1 or not isinstance(value.get('apps'),list) or set(value['apps'])-set(APPS):
            raise ValueError('Invalid local offline action approvals')
        return value

    def execute(self, identity):
        if str(uuid.UUID(identity))!=identity or uuid.UUID(identity).version!=4:
            raise ValueError('A genuine request UUID4 is required')
        # Lock ordering matches floor transitions: relay first, then Pad store.
        with closing(sqlite3.connect(self.pad_data/'browser-relay.sqlite3',timeout=5)) as relay:
            relay.execute('BEGIN IMMEDIATE')
            floor=dict(relay.execute('SELECT * FROM relay_control'))
            claim=relay.execute('SELECT epoch,state FROM local_claims WHERE id=?',(identity,)).fetchone()
            if floor.get('floor_mode')!='local' or not claim or claim!=(int(floor.get('floor_epoch',-1)),'running'):
                raise ValueError('No current local claim for this request')
            with closing(sqlite3.connect('file:'+str(self.pad_data/'pad-state.sqlite3')+'?mode=ro',uri=True)) as store:
                row=store.execute("SELECT m.text FROM messages m JOIN outbox o ON m.id=o.id WHERE m.id=? AND m.sender='phil' AND o.route='offline' AND o.state='offline_running'",(identity,)).fetchone()
            app=parse_launch(row[0]) if row else None
            if app is None:raise ValueError('Only an explicit standalone supported app launch is allowed')
            relative,allowed=APPS[app]
            with self.bridge.desktop._lock,closing(sqlite3.connect(self.path,timeout=5)) as db:
                config=self.bridge.config
                policy=self.policy()
                if policy.get('enabled') is not True:raise ValueError('Offline actions are switched off locally')
                if app not in policy['apps']:raise ValueError('This app has no local offline-action approval')
                if not config.get('features',{}).get('desktop_control'):raise ValueError('Desktop control is disabled')
                if self.secure() is not None:raise ValueError('Windows session is locked, secure or unavailable')
                if not allowed.intersection(n.lower() for n in config.get('desktop_apps',[])):
                    raise ValueError('This app is not locally approved for desktop control')
                db.execute('BEGIN IMMEDIATE')
                previous=db.execute('SELECT app,status,detail FROM receipts WHERE id=?',(identity,)).fetchone()
                if previous:
                    return {'request_id':identity,'app':previous[0],'status':previous[1],'detail':previous[2],'replayed':False}
                if db.execute('SELECT count(*) FROM receipts WHERE epoch=?',(claim[0],)).fetchone()[0]>=10:
                    raise ValueError('Offline session action cap: ten requests')
                if db.execute('SELECT count(*) FROM receipts WHERE ts>?',(time.time()-60,)).fetchone()[0]>=5:
                    raise ValueError('Offline app launch rate limit: five per minute')
                executable=self.windows/relative
                if not executable.is_file():raise ValueError('Installed Windows app launcher is unavailable')
                # Persist intent BEFORE launch. A crash in the gap is unknown, never retried.
                if self.presence(app):
                    detail='An approved app window is already present; no duplicate launch.'
                    with db:db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)',(identity,app,'already-running',time.time(),detail,claim[0]))
                    self.bridge.audit.record('offline.app.result',app,True,json.dumps({'id':identity,'status':'already-running'}))
                    return {'request_id':identity,'app':app,'status':'already-running','detail':detail,'replayed':False}
                db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)',(identity,app,'unknown-held',time.time(),'Launch outcome not confirmed; never automatically retry',claim[0]))
                db.commit()
                args=[str(executable)]
                if app=='settings':args.append('ms-settings:')
                self.bridge.audit.record('offline.app.requested',app,True,identity)
                try:
                    child=self.launch(args,shell=False,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    detail='Windows accepted the launch; PID '+str(child.pid)+'. Presence confirmation pending.'
                    status='unknown-held'
                except Exception as exc:
                    status='failed';detail='Windows launch failed: '+type(exc).__name__
                with db:db.execute('UPDATE receipts SET status=?,detail=? WHERE id=?',(status,detail,identity))
                self.bridge.audit.record('offline.app.result',app,False,json.dumps({'id':identity,'status':status}))
            # Keep the epoch fence through the actual launch, then release it
            # before waiting for UI presence.
            relay.commit()
            # Do not hold the desktop lock while waiting for an app window.
            if status=='unknown-held':
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    if self.presence(app):
                        status='executed-confirmed';detail='Approved app window independently detected after launch.';break
                    time.sleep(.2)
                with closing(sqlite3.connect(self.path)) as db,db:
                    db.execute('UPDATE receipts SET status=?,detail=? WHERE id=?',(status,detail,identity))
                self.bridge.audit.record('offline.app.confirmed',app,status=='executed-confirmed',json.dumps({'id':identity,'status':status}))
            return {'request_id':identity,'app':app,'status':status,'detail':detail,'replayed':False}
