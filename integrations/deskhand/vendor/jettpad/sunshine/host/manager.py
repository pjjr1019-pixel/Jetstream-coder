"""Reviewable lifecycle core. OS bindings/UI are injected; this is NOT a remote API.

STOP cancellation never waits for preflight, disk, network, or logging. A launcher
must register its suspended job through owned() BEFORE it resumes any process.
The production app has not been wired to this staged module yet.
"""
from __future__ import annotations
from dataclasses import dataclass
import threading
from typing import Callable, Protocol
from .policy import EndpointPolicy

class OwnedJob(Protocol):
    def terminate_now(self) -> None: ...

@dataclass(frozen=True)
class Preflight:
    policy: EndpointPolicy
    executable_sha256: str
    tailscale_up: bool
    firewall_verified: bool
    capture_monitor_ready: bool

    def validate(self):
        if not (self.tailscale_up and self.firewall_verified and self.capture_monitor_ready):
            raise ValueError('Tailscale, exact firewall policy and physical-interface observer must be ready')
        if len(self.executable_sha256)!=64 or any(c not in '0123456789abcdef' for c in self.executable_sha256):
            raise ValueError('Verified executable digest is required')

class StreamManager:
    def __init__(self, *, stop_latched:Callable[[],bool], preflight:Callable[[],Preflight],
                 launch:Callable[[Preflight,Callable[[OwnedJob],bool]],None], journal:Callable[[str,str],None]):
        self._stop_latched=stop_latched;self._preflight=preflight;self._launch=launch;self._journal=journal
        self._lock=threading.Lock();self._generation=0;self._job=None
        self._desired=False;self._phase='off';self._reason='Startup is always OFF';self._policy=None

    def status(self):
        with self._lock:
            return {'enabled':self._desired,'phase':self._phase,'reason':self._reason,
                    'generation':self._generation,'phone':self._policy.phone if self._policy else None}

    def start_from_local_ui(self, *, privacy_warning_accepted:bool,expected_generation:int|None=None,origin='local'):
        # Intentionally not exposed through bridge protocol or Android.
        if origin not in ('local','paired_phone'):raise ValueError('Unknown stream start source')
        if not privacy_warning_accepted:raise ValueError('Acknowledge the desktop-visibility warning at every start')
        with self._lock:
            if expected_generation is not None and expected_generation!=self._generation:raise ValueError('An OFF action cancelled this queued start')
            if self._stop_latched():raise ValueError('Local STOP is latched')
            if self._phase!='off':raise ValueError('Streaming is already starting or running')
            self._generation+=1;token=self._generation;self._desired=True;self._phase='starting';self._reason='Local toggle'
        try:
            proof=self._preflight();proof.validate()
            with self._lock:
                if token!=self._generation or not self._desired or self._stop_latched():
                    raise ValueError('Start cancelled by STOP or local toggle')
                self._policy=proof.policy
            def owned(job):
                with self._lock:
                    allowed=token==self._generation and self._desired and not self._stop_latched() and self._job is None
                    if allowed:self._job=job
                if not allowed:job.terminate_now()
                return allowed
            self._launch(proof,owned)
            with self._lock:
                if token!=self._generation or not self._desired or self._stop_latched():
                    raise ValueError('Start cancelled by STOP or local toggle')
                if self._job is None:raise ValueError('Launcher failed to register kill-on-close ownership')
                self._phase='listening';self._reason='Awaiting paired phone'
            self._journal('stream.start',('Explicit paired-phone request under local approval' if origin=='paired_phone' else 'Explicit local toggle')+'; privacy warning acknowledged')
        except Exception:
            self.stop_now('Startup failed or was cancelled',expected_generation=token)
            self._journal('stream.start_failed','Startup failed or was cancelled')
            raise

    def stop_now(self, reason:str, *, expected_generation:int|None=None):
        with self._lock:
            if expected_generation is not None and expected_generation!=self._generation:return False
            self._generation+=1;self._desired=False;self._phase='off';self._reason=reason
            job=self._job;self._job=None
        # The critical path is kill only. Callers journal AFTER this returns.
        if job is not None:job.terminate_now()
        return True

    def observed_failure(self, reason:str, generation:int):
        if self.stop_now(reason,expected_generation=generation):self._journal('stream.stop',reason)

    def connected(self,generation:int):
        with self._lock:
            if generation==self._generation and self._desired and not self._stop_latched():self._phase='streaming';self._reason='Paired phone connected'
