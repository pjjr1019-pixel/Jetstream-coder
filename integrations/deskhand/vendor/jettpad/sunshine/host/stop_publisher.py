"""Staged additive STOP broadcaster. No permission, PIN or grant writes.

The live bridge adapter is not installed. Only its authenticated LOCAL operator
handler may call arm_local(), after validating the pinned suspended Sunshine
process identity and obtaining a held termination handle. No bridge-client op
is added. Read-only consumers cannot reset or consume this manual-reset event.
"""
from __future__ import annotations
import os,threading,uuid
from typing import Callable
from .windows import StopSignal

class StopPublisher:
    def __init__(self,stop_latched:Callable[[],bool],*,signal_factory=StopSignal):
        self.name='Local\\JettPad.StreamSTOP.'+str(os.getpid())+'.'+str(uuid.uuid4())
        self._stop_latched=stop_latched;self._lock=threading.RLock();self._epoch=0
        self._terminate=None;self._armed=False;self._fault=None;self._signal=None
        try:self._signal=signal_factory(self.name,create=True,initially_stopped=True)
        except Exception:self._fault='STOP signal unavailable; streaming disabled'

    def status(self):
        with self._lock:
            return {'available':self._signal is not None and self._fault is None,'event':self.name,
                    'epoch':self._epoch,'armed':self._armed,'fault':self._fault,'owner_pid':os.getpid()}

    def arm_local(self,*,expected_epoch:int,terminate_verified_process:Callable[[],None],local_action_verified:bool):
        if not local_action_verified:raise ValueError('Explicit authenticated local operator action is required')
        with self._lock:
            if self._fault or self._signal is None:raise ValueError('Streaming STOP notification is unavailable')
            if self._stop_latched():raise ValueError('Local STOP is latched; this action cannot clear it')
            if expected_epoch!=self._epoch:raise ValueError('STOP epoch changed; do not retry automatically')
            if self._armed:raise ValueError('Another streaming lease already exists')
            self._terminate=terminate_verified_process
            try:
                self._signal.clear_from_authorized_local_path()
                if self._stop_latched():raise ValueError('STOP arrived during local stream preparation')
                self._armed=True
                return self.status()
            except Exception:
                self.notify_stop('Stream preparation failed')
                raise

    def notify_stop(self,reason:str):
        """Never raise into the existing revoke path. Kill before disk/PIN/audit."""
        terminate=None
        try:
            with self._lock:
                self._epoch+=1;self._armed=False;terminate=self._terminate;self._terminate=None
                if self._signal is not None:
                    try:self._signal.latch()
                    except Exception:self._fault='STOP signal failed; streaming disabled'
        finally:
            if terminate is not None:
                try:terminate()
                except Exception:self._fault='Streaming termination failed; local intervention required'

    def release_local(self):
        self.notify_stop('Local stream off')

    def close(self):
        self.notify_stop('Bridge shutting down')
        with self._lock:
            if self._signal is not None:
                try:self._signal.close()
                except Exception:pass
                self._signal=None
