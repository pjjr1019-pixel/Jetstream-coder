"""Narrow temporary see/control authority for a remote session.

STOP latch removed (Phil 2026-11-09): there is no persisted STOP underneath
remote access any more. The 30-minute session (plus the 24-hour day
authorization) is the only authority; fs/exec/chat never consult it.
"""
import json,os,sqlite3,threading,time,uuid
from .remote_pin import PinStore,UnlockError

SESSION_ONLY=frozenset({'timeout','bridge_disconnect','windows_session_changed','windows_sign_out',
    'windows_lock','session_monitor_stopping','server_stopping','permission_session_expired',
    'remote_guard_unavailable','remote_unlock_activation_failed'})
SESSION_LATCH_REASONS=frozenset()  # STOP latch removed; retained for import compatibility, no longer consulted.

class RemoteControl:
    def __init__(self,bridge,*,clock=time.time,monotonic=time.monotonic,session_check=None):
        self.bridge=bridge;self.clock=clock;self.monotonic=monotonic
        self.pins=PinStore(bridge.data_dir,clock)
        self.lock=threading.RLock();self.epoch=0;self.expires=None;self.deadline=None;self.pin_revision=None
        self._watch_available=False;self.watch_probe=None
        self.last_day_revoked=False;self._day_fault=False;self._day_expiry=None;self._day_deadline=None
        self._last_day_public=None
        if session_check is None:
            from .session_watch import interactive_session
            session_check=interactive_session
        self.session_check=session_check
    @property
    def watch_available(self):
        return bool(self.watch_probe()) if self.watch_probe else self._watch_available
    @watch_available.setter
    def watch_available(self,value):self._watch_available=bool(value)
    def revoked(self,reason='local_operator_revoke',preserve_day=None):
        with self.lock:
            was_active=self.expires is not None
            self.expires=self.deadline=self.pin_revision=None;self.epoch+=1
            self.last_day_revoked=False
            if not (reason in SESSION_ONLY if preserve_day is None else preserve_day):
                self._day_expiry=self._day_deadline=None
                try:self.last_day_revoked=self.pins.revoke_day()
                except (OSError,sqlite3.Error):
                    self._day_fault=True;self._day_event('persistence_failed')
                self._publish_day({'day_authorized':False,'day_authorization_expires_at':None})
            return was_active

    def _day_event(self,result):
        loop=self.bridge._loop
        if loop is None or loop.is_closed():return
        def record():
            import asyncio
            task=asyncio.create_task(self.bridge._audit('control.day_authorization','see/desktop',result!='persistence_failed',result))
            self.bridge._background_audits.add(task);task.add_done_callback(self.bridge._background_audits.discard)
            self.bridge.state_changed()
        loop.call_soon_threadsafe(record)

    def day_status(self):
        with self.lock:
            try:state,ended=self.pins.day_state()
            except (OSError,sqlite3.Error):
                if not self._day_fault:self._day_event('persistence_failed')
                self._day_fault=True
                return self._publish_day({'day_authorized':False,'day_authorization_expires_at':None})
            blocked=self._day_fault
            if state['day_authorized'] and (blocked or (self._day_expiry==state['day_authorization_expires_at'] and self._day_deadline is not None and self.monotonic()>=self._day_deadline)):
                # B2-E (B-6): a transient SQLite/FS failure revoking the day
                # grant must not kill the safety monitor (and with it the
                # bridge). Record the fault and report the day grant as
                # unauthorised; the monitor retries on its next tick.
                try:
                    self.pins.revoke_day()
                except (OSError, sqlite3.Error):
                    if not self._day_fault: self._day_event('persistence_failed')
                    self._day_fault=True
                    ended='persistence_failed'
                else:
                    ended='expired'
                state={'day_authorized':False,'day_authorization_expires_at':None}
            if ended:self._day_event(ended)
            if state['day_authorized']:
                expiry=state['day_authorization_expires_at']
                if expiry!=self._day_expiry:
                    self._day_expiry=expiry;self._day_deadline=self.monotonic()+max(0,expiry-self.clock())
            else:self._day_expiry=self._day_deadline=None
            return self._publish_day(state)

    def _publish_day(self,state):
        # Display metadata only. The UI never uses this file to grant authority.
        if state!=self._last_day_public:
            path=self.bridge.data_dir/'day-authorization-status.json'
            temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                temporary.write_text(json.dumps(state),encoding='utf-8');os.replace(temporary,path)
                self._last_day_public=dict(state)
            except OSError:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass
        return state

    def require_day(self):
        if not self.day_status()['day_authorized']:raise UnlockError('day_authorization_required','A fresh PIN unlock is required')
        return self.pins.require_day()

    def authorize_day(self,revision,epoch,generation,issued):
        with self.lock:
            if self.epoch!=epoch or self.expires is None:raise UnlockError('unlock_cancelled')
            created=self.pins.grant_day(revision,generation,issued)
            self._day_fault=False
            if created:self._day_event('granted_24h')
            return self.day_status()
    def session_changed(self,reason='windows_session_changed'):
        with self.lock:active=self.expires is not None
        if active:self.bridge.desktop.revoke(reason=reason)
    def check(self):
        with self.lock:
            if self.expires is None:return False
            reason=None
            if self.clock()>=self.expires or self.monotonic()>=self.deadline:reason='timeout'
            elif not self.bridge.connected:reason='bridge_disconnect'
            else:
                try:
                    if not self.watch_available or not self.session_check():reason='windows_session_changed'
                    elif self.pins.revision()!=self.pin_revision:reason='local_pin_changed'
                except (OSError,ValueError,sqlite3.Error):reason='remote_guard_unavailable'
        # Never acquire the desktop lock while holding the remote-state lock.
        if reason:self.bridge.desktop.revoke(reason=reason);return False
        return True
    def stopped(self):
        # STOP latch removed: stub kept for callers; the latch is always absent.
        return False
    def begin(self,revision,epoch,generation,day_generation=None):
        with self.lock:
            if self.epoch!=epoch or self.bridge.desktop._generation!=generation:raise UnlockError('unlock_cancelled','Control state changed; request a fresh unlock')
            if not self.bridge.connected or not self.watch_available or not self.session_check():raise UnlockError('unlock_unavailable','Interactive PC session unavailable')
            if self.pins.revision()!=revision:raise UnlockError('unlock_cancelled','PIN changed locally; request a fresh unlock')
            if day_generation is not None and self.require_day()!=(revision,day_generation):raise UnlockError('unlock_cancelled')
            self.expires=self.clock()+1800;self.deadline=self.monotonic()+1800;self.pin_revision=revision
    def status(self):
        active=self.check();public=self.pins.public()
        day=self.day_status()
        available=bool(public['pin_set'] and self.watch_available and not public['unlock_lockout_until']
                and any(self.bridge.config['features'][name] for name in ('screenshots','desktop_control')))
        return {**public,**day,'remote_day_unlock_available':available and day['day_authorized'],
                'remote_unlock_available':bool(public['pin_set'] and self.watch_available and not public['unlock_lockout_until']
                and any(self.bridge.config['features'][name] for name in ('screenshots','desktop_control'))),
                'remote_unlocked':active,'unlock_expires_at':self.expires if active else None,
                'remote_unlock_monitor_ready':self.watch_available}
