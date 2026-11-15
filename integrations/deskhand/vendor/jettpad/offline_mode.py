"""Single-floor offline takeover. No network listener or model action executor."""
from concurrent.futures import Future
import json
import sqlite3
import threading
import time
import uuid

from offline_model import LocalModel, append_once


class Floor:
    def __init__(self, offline=False, down_seconds=90, up_seconds=120):
        self.offline = offline
        self.down_seconds, self.up_seconds = down_seconds, up_seconds
        self.down_at = self.up_at = None
        self.last_now = self.last_wall = None

    def step(self, healthy, now, manual=False, busy=False, wall=None):
        if self.last_now is not None and (now-self.last_now > 20 or
                (wall is not None and self.last_wall is not None and wall-self.last_wall-(now-self.last_now)>10)):
            self.down_at = self.up_at = None  # suspend/resume or a long scheduler stall: start fresh windows
        self.last_now,self.last_wall = now,wall
        if healthy:
            self.down_at = None
            if self.up_at is None: self.up_at = now
        else:
            self.up_at = None
            if self.down_at is None: self.down_at = now
        if manual:
            self.offline = True
        elif not self.offline and self.down_at is not None and now-self.down_at >= self.down_seconds:
            self.offline = True
        elif self.offline and not busy and self.up_at is not None and now-self.up_at >= self.up_seconds:
            self.offline = False
        return self.offline


class OfflineCoordinator:
    def __init__(self, engine, model=None, health=None, clock=time.monotonic, floor=None):
        self.engine, self.clock = engine, clock
        self.lock = engine.routing_lock
        self.package = engine.documents/'jett-offline'
        if model is None:
            from offline_actions import LocalActions
            model=LocalModel(self.package,engine.data,actions=LocalActions(self.package))
        self.model = model
        from model_priority import ModelPriority
        self.model_priority = ModelPriority(engine.data)
        self.health = health or self._health
        self.manual = self.setting('offline_manual') == '1'
        saved = engine.mailbox.floor_state()['mode']=='local'
        self.floor = floor or Floor(offline=saved)
        self.epoch = self.setting('offline_epoch') or str(uuid.uuid4())
        self.last_healthy = False
        self.quota_active=False
        self.last_evidence = {'bridge_connected': False, 'browser_available': False}
        self.future = None
        self.active = None
        self.active_epoch = None
        self.pending_transition = None
        self.last_ping = -100.0
        self.ping_ok = False
        self.flap_latched = self.setting('offline_flap_latched')=='1'
        try: self.activations = json.loads(self.setting('offline_activations') or '[]')
        except ValueError: self.activations = []
        self.error = None
        self._handoff_refresh_at=0.0
        self.stopping = threading.Event()
        self.thread = None
        self.warm_epoch=None;self.warm_thread=None
        self.engine.mailbox.set_offline_hold(self.floor.offline or self.manual)
        if not self.floor.offline:
            with engine.store.lock, engine.store.db:
                engine.store.db.execute("UPDATE messages SET status='complete' WHERE status='deferred_cloud'")
        with engine.store.lock, engine.store.db:
            interrupted = list(engine.store.db.execute("SELECT id FROM outbox WHERE route='offline' AND state='offline_running'"))
            engine.store.db.execute("UPDATE outbox SET state='offline_interrupted' WHERE route='offline' AND state='offline_running'")
        for row in interrupted:
            with engine.store.lock:
                completed=engine.store.db.execute("SELECT 1 FROM messages WHERE id=? AND sender='offline' AND status='complete'",('offline:'+row['id'],)).fetchone()
            if completed:
                self.engine.store.delivery(row['id'],'offline_complete')
                self.engine.mailbox.finish_local(row['id'])
                continue
            self.reply(row['id'], 'Local reply was interrupted by the app closing. The outcome of any pending action is unknown; check existing receipts before retrying. This prompt was not resent.', failed=True)
            self.engine.store.delivery(row['id'],'offline_interrupted')
            self.engine.mailbox.interrupt_local(row['id'])
        if self.floor.offline:
            self._migrate_never_sent()
        elif self.manual:
            self.tick(False,{'startup_manual':True})

    def setting(self, key, value=None):
        store = self.engine.store
        with store.lock, store.db:
            if value is not None:
                store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key,value))
            row = store.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def _health(self):
        from musebridge.cli import default_home
        from musebridge.lifecycle import local_request
        browser = getattr(self.engine, 'browser_controller', None)
        if browser and getattr(browser,'_probe_code',None)=='quota':
            return False,{'muse_quota':True,'browser_available':False,'reason':'Muse usage limit reached'}
        state = local_request(default_home(), timeout=2)
        connected = state.get('connected') is True
        if connected and self.clock()-self.last_ping >= 10:
            try:
                reply = local_request(default_home(),'ping_muse',timeout=6)
                self.ping_ok = reply.get('result',{}).get('method')=='websocket_ping'
            except Exception: self.ping_ok = False
            self.last_ping = self.clock()
        if not connected: self.ping_ok=False;self.last_ping=-100.0
        evidence = {'bridge_connected': connected, 'authenticated_daemon_round_trip':self.ping_ok,
                    'browser_available': bool(browser and browser.cloud_ready),
                    'stop_latched':state.get('stop_latched') is True,
                    'meaning':'Daemon transport reachability, not ordinary model-worker readiness'}
        return connected and self.ping_ok and evidence['browser_available'], evidence

    def event(self, event, **values):
        append_once(self.engine.data/'offline-events.jsonl', str(uuid.uuid4()),
                    {'event': event, 'epoch': self.epoch, **values})

    def notify(self, identity, text):
        self.engine.store.put({'id':identity,'ts':time.time(),'from':'system','text':text})
        self.engine.store.queue_changed.set()

    def cutoff(self):
        try: return self.model.cutoff()
        except Exception: return 'unavailable — offline package needs verification'

    def set_manual(self, value):
        if type(value) is not bool: raise ValueError('Offline mode must be on or off')
        self.manual = value
        self.setting('offline_manual', '1' if value else '0')
        if not value:
            self.flap_latched=False;self.activations=[]
            self.setting('offline_flap_latched','0');self.setting('offline_activations','[]')
        self.event('manual_mode_changed', enabled=value)

    def _pending(self):
        with self.engine.store.lock:
            return bool(self.engine.store.db.execute("SELECT 1 FROM outbox WHERE route='offline' AND state IN ('queued','offline_running') LIMIT 1").fetchone())

    def _migrate_never_sent(self):
        """Leave already-queued cloud messages on their original route.

        New messages use the clearly displayed active mode. A queued prompt
        is never silently redirected to a different assistant or model.
        """
        return

    def _handoff(self):
        self.package.mkdir(parents=True, exist_ok=True)
        folder = self.package/'handoffs';folder.mkdir(exist_ok=True)
        destination = folder/(self.epoch+'.json')
        refresh=not destination.exists()
        if destination.exists() and self.engine.mailbox.floor_state()['mode']=='local':
            previous=json.loads(destination.read_text(encoding='utf-8'))
            claims_now=self.engine.mailbox.local_handoff_claims()
            refresh=(previous['request_ids']!=[r['id'] for r in claims_now] or
                     previous['interrupted_ids']!=[r['id'] for r in claims_now if r['state']=='interrupted'])
            if refresh:
                import hashlib
                raw=destination.read_bytes();archive=folder/(self.epoch+'.prepared-'+hashlib.sha256(raw).hexdigest()[:12]+'.json')
                if not archive.exists():archive.write_bytes(raw)
        if refresh:
            claims=self.engine.mailbox.local_handoff_claims()
            ids=[r['id'] for r in claims]
            actions_path=self.package/'OFFLINE_ACTIONS.jsonl'
            receipts=[json.loads(line) for line in actions_path.read_text(encoding='utf-8').splitlines() if line.strip()] if actions_path.exists() else []
            receipts=[row for row in receipts if row.get('request_id') in ids]
            value = {'schema_version':1,'session_id':self.epoch,'finished_ts':time.time(),
                     'next_floor_epoch':self.engine.mailbox.floor_state()['epoch']+1,
                     'last_sync':self.cutoff(),'request_ids':ids,'actions_executed':any(row.get('status')=='executed-confirmed' for row in receipts),
                     'action_receipts':receipts,'actions_path':str(actions_path),
                     'read_receipts':[row for row in __import__('offline_context').rows(self.package/'OFFLINE_READS.jsonl') if row.get('request_id') in ids],
                     'unconfirmed_cloud_ids':self.engine.mailbox.held_cloud_ids(),
                     'notes_path':str(self.package/'OFFLINE_NOTES.md'),
                     'pending_path':str(self.package/'PENDING_FOR_JETT.md'),
                     'interrupted_ids':[r['id'] for r in claims if r['state']=='interrupted'],
                     'rule':'Read original notes/pending requests before replying; attribute new facts via offline stand-in until Phil confirms. Reconcile duplicates as superseded; record each pending item executed or explicitly declined only under real authorization. Refresh package and manifest after an actual sync, never fabricate a sync.'}
            temporary = destination.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2),encoding='utf-8');temporary.replace(destination)
        value = json.loads(destination.read_text(encoding='utf-8'))
        pointer = self.package/'HANDOFF.json';temp = pointer.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2),encoding='utf-8');temp.replace(pointer)
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'jett-pad-offline-handoff:'+self.epoch))
        text = ('Jett Pad automatic handoff — software-generated, not a message authored by Phil. '
                'The local stand-in has yielded after stable recovery or the Offline toggle was released. '
                'Read '+str(destination)+' and the listed original OFFLINE_NOTES.md/PENDING_FOR_JETT.md entries for its request_ids. '
                'Reconcile new notes with Phil/Jett records, update your SYNC-MANIFEST only after an actual sync, '
                'and return one normal correlated text-only Pad acknowledgement. Nothing pending was executed. '
                'Read action_receipts: successful or unknown local launches must NEVER be replayed. '
                'Do not clear STOP, change permissions, restart anything or send audio. Preserve standing file history.')
        store = self.engine.store
        with store.lock, store.db:
            store.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,0)',(identity,time.time(),'phil',text,'complete'))
            store.db.execute("INSERT OR IGNORE INTO outbox(id,state,route) VALUES (?,'queued','cloud_notice')",(identity,))
        self.event('handoff_written', path=str(destination), notice_id=identity, request_count=len(value['request_ids']))
        return value['next_floor_epoch']

    def _switch(self, local):
        if local:
            floor_epoch = self.engine.mailbox.advance_floor(True)
            # Reuse the session identity if an interrupted cross-store transition already saved it.
            new_session=self.setting('offline_floor') != 'local'
            if new_session:
                self.epoch = str(uuid.uuid4())
                self.setting('offline_epoch', self.epoch)
                self.setting('offline_started', str(time.time()))
            self.setting('offline_floor', 'local')
            if not self.manual and new_session:
                self.activations=[t for t in self.activations if 0<=time.time()-t<3600]+[time.time()]
                if len(self.activations)>=3:self.flap_latched=True
                self.setting('offline_activations',json.dumps(self.activations))
                self.setting('offline_flap_latched','1' if self.flap_latched else '0')
            self._migrate_never_sent()
            self.event('offline_entered', manual=self.manual, floor_epoch=floor_epoch,evidence=self.last_evidence)
            self.notify('offline-enter:'+self.epoch,
                ('Offline mode selected. ' if self.manual else 'Muse usage limit reached; automatic routing uses the local qwen3.5:9b model. ' if self.quota_active else 'Jett is unreachable from this PC. ')+
                'The local stand-in is answering in text. Last sync: '+self.cutoff()+'. Approved app launches and scoped reads can run locally; other actions wait for Jett.')
        else:
            handoff_epoch=self._handoff()
            with self.engine.store.lock,self.engine.store.db:
                self.engine.store.db.execute("UPDATE messages SET status='complete' WHERE status='deferred_cloud'")
            self.engine.mailbox.advance_floor(False,handoff_epoch=handoff_epoch)
            self.setting('offline_floor', 'cloud')
            self.event('cloud_resumed', evidence=self.last_evidence)
            self.notify('offline-exit:'+self.epoch,'Cloud connection restored. The local stand-in has yielded to Jett; a handoff is queued.')

    def tick(self, healthy, evidence=None):
        with self.lock:
            if self.pending_transition is not None and not (self.pending_transition is False and (self.future is not None or self._pending())):
                target=self.pending_transition
                try:
                    self._switch(target)
                    self.floor.offline=target
                    self.pending_transition=None
                except BaseException:
                    self.floor.offline=self.engine.mailbox.floor_state()['mode']=='local'
                    raise
            self.last_healthy, self.last_evidence = healthy, evidence or {}
            self.quota_active=self.last_evidence.get('muse_quota') is True
            self.model.fallback_enabled=False
            before = self.floor.offline
            local = self.floor.step(healthy, self.clock(), self.manual or self.flap_latched or self.quota_active,
                                    self.future is not None or self._pending(),wall=time.time())
            if local != before:
                try:self._switch(local)
                except BaseException:
                    self.pending_transition=local
                    self.floor.offline=self.engine.mailbox.floor_state()['mode']=='local'
                    raise
            if local:
                self._migrate_never_sent()
            self.engine.store.queue_changed.set()

    def status(self):
        try:self.publish_handoff_status()
        except (OSError,sqlite3.Error):pass  # Keep connection status available if history is busy.
        if self.error and self.floor.offline: return 'Offline assistant unavailable — '+self.error
        if self.quota_active:return 'Muse usage limit reached · Offline Jett uses qwen3.5:9b'+(' · answering' if self.future else '')
        if self.flap_latched:return 'Offline Jett · repeated drops · use Auto to return to automatic mode'
        if self.manual and not self.floor.offline:return 'Switching to Offline Jett · finishing a cloud send safely'
        if self.floor.offline:
            if not self.manual and self.last_healthy:
                up_at=self.floor.up_at if self.floor.up_at is not None else self.clock()
                remaining=max(0,int(self.floor.up_seconds-(self.clock()-up_at)+1))
                return 'Offline Jett · cloud recovery in '+str(remaining)+'s, then finishing local replies'
            return 'Offline Jett · qwen3.5:9b · '+('manual mode' if self.manual else 'automatic takeover')+(' · answering locally' if self.future else ' · approved actions only')
        if not self.last_healthy:
            return 'Checking cloud connection · automatic local takeover after 90 seconds unreachable'
        browser_status=getattr(self.engine,'browser_status','Ready')
        if browser_status not in ('Ready','connecting to Muse'):
            handoff=self.handoff_summary()
            return 'Cloud Jett · '+browser_status+(' · '+handoff if handoff else '')
        handoff=self.handoff_summary()
        return 'Cloud Jett · automatic offline takeover ready'+(' · '+handoff if handoff else '')

    def handoff_summary(self):
        """Only report locally evidenced delivery; a reply is not proof of file reads."""
        try:
            path=self.package/'handoffs'/(self.epoch+'.json')
            if not path.is_file():return ''
            value=json.loads(path.read_text(encoding='utf-8'))
            identity=str(uuid.uuid5(uuid.NAMESPACE_URL,'jett-pad-offline-handoff:'+self.epoch))
            with self.engine.store.lock:
                row=self.engine.store.db.execute('SELECT state FROM outbox WHERE id=?',(identity,)).fetchone()
            raw=row[0] if row else 'unknown'
            delivery={'queued':'queued','browser_queued':'queued','browser_sending':'sending','browser_complete':'acknowledged','uncertain':'delivery unknown','browser_abandoned':'delivery unknown (marked abandoned)'}.get(raw,'pending or unknown')
            confirmed=sum(isinstance(r,dict) and r.get('status')=='executed-confirmed' for r in value.get('action_receipts',[]))
            return ('handoff '+delivery+'; '+str(len(value.get('request_ids',[])))+' items pending cloud verification, '+str(len(value.get('interrupted_ids',[])))+' interrupted, '+str(confirmed)+' confirmed local actions, '+str(len(value.get('read_receipts',[])))+' local read receipts; cloud file reads and reconciliation not verified')
        except (OSError,ValueError,TypeError,KeyError):return 'handoff status unknown; cloud file reads and reconciliation not verified'

    def publish_handoff_status(self):
        now=time.monotonic()
        if now<getattr(self,'_handoff_refresh_at',0):return
        self._handoff_refresh_at=now+2
        text=self.handoff_summary()
        if not text:return
        store=self.engine.store;identity='handoff-status:'+self.epoch
        with store.lock,store.db:
            old=store.db.execute('SELECT text FROM messages WHERE id=?',(identity,)).fetchone()
            if old and old[0]!=text:store.db.execute('UPDATE messages SET text=? WHERE id=?',(text,identity))
            elif not old:store.db.execute("INSERT INTO messages(id,ts,sender,text,status,logged) VALUES (?,?,'system',?,'complete',0)",(identity,time.time(),text))
            else:return
        store.queue_changed.set()

    def reply(self, identity, text, failed=False):
        self.engine.store.put({'id':'offline:'+identity,'ts':time.time(),'from':'offline',
                              'text':text if text.startswith('Offline Jett') else 'Offline Jett — local stand-in\n\n'+text})
        self.engine.store.delivery(identity, 'offline_failed' if failed else 'offline_complete')

    def pump(self):
        with self.lock:
            if self.future and self.future.done():
                try:
                    if not self.engine.mailbox.local_result_allowed(self.active,self.active_epoch):
                        raise ValueError('Local reply belongs to a stale floor epoch')
                    answer = self.future.result();self.reply(self.active,answer);self.error=None
                    self.engine.mailbox.finish_local(self.active)
                    self.event('local_reply_complete', request_id=self.active, speech_allowed=False)
                except Exception as exc:
                    from selected_models import local_error
                    self.error = local_error(exc)
                    self.engine.mailbox.interrupt_local(self.active)
                    self.reply(self.active,local_error(exc)+' Nothing was sent to the cloud.',failed=True)
                    self.event('local_reply_failed', request_id=self.active, error_type=type(exc).__name__)
                self.future = self.active = None
            if not self.floor.offline or self.future or self.stopping.is_set() or self.pending_transition is True: return
            store = self.engine.store
            with store.lock, store.db:
                row = store.db.execute("SELECT m.* FROM messages m JOIN outbox o ON m.id=o.id WHERE o.route='offline' AND o.state='queued' ORDER BY m.ts,m.id LIMIT 1").fetchone()
                if row is None: return
                epoch=self.engine.mailbox.claim_local(row['id'])
                if epoch is None:return
                store.db.execute("UPDATE outbox SET state='offline_running' WHERE id=?",(row['id'],))
            self.active = row['id']
            self.active_epoch = epoch
            history = [r for r in store.recent_messages(31) if r['id'] != row['id']][-30:]
            self.event('local_reply_started', request_id=self.active, model_policy='offline default: qwen3.5:9b', speech_allowed=False)
            self.future = Future()
            future,identity,text = self.future,row['id'],row['text']
            def generate():
                if not future.set_running_or_notify_cancel(): return
                try:
                    with self.model_priority.interactive(cancel=self.stopping.is_set):
                        future.set_result(self.model.answer(identity,text,history))
                except BaseException as error: future.set_exception(error)
            threading.Thread(target=generate,daemon=True,name='jett-local-model').start()

    def start(self):
        self.thread = threading.Thread(target=self.run,daemon=True,name='jett-offline-health');self.thread.start()

    def run(self):
        last = -100.0
        while not self.stopping.is_set():
            try:
                if self.clock()-last >= 2:
                    try: healthy,evidence = self.health()
                    except Exception: healthy,evidence = False,{'local_health_unavailable':True}
                    self.tick(healthy,evidence);last=self.clock()
                self.pump()
                if (self.floor.offline and self.future is None and not self._pending()
                        and self.warm_epoch!=self.epoch and hasattr(self.model,'prewarm')):
                    self.warm_epoch=self.epoch
                    def warm():
                        try:
                            with self.model_priority.background(cancel=lambda:self.stopping.is_set() or not self.floor.offline or self.future is not None):
                                if not self.stopping.is_set() and self.floor.offline and self.future is None:self.model.prewarm()
                        except Exception:pass  # optional; ordinary reply path remains available
                    self.warm_thread=threading.Thread(target=warm,daemon=True,name='Jett idle model warmup')
                    self.warm_thread.start()
            except Exception as exc:
                self.error = type(exc).__name__
            self.stopping.wait(.1)

    def close(self):
        self.stopping.set()
        if self.thread: self.thread.join(4)
        # In-flight model has no actions. Its output is discarded, not written by a closed app.
        if self.future: self.future.cancel()
