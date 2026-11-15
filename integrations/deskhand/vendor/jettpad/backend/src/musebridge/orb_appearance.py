"""Read-only presentation facts and one-way label blackout; grants nothing."""
from pathlib import Path
import json,os,threading,time,uuid

class LabelSafety:
    def __init__(self,stopped,executor,*,folder=None,signal_factory=None):
        if signal_factory is None:
            from sunshine.host.windows import StopSignal
            signal_factory=StopSignal
        self.folder=Path(folder or Path(os.environ['LOCALAPPDATA'])/'JettOrb');self.folder.mkdir(parents=True,exist_ok=True)
        self.stopped=stopped;self.executor=executor;self.lock=threading.RLock();self.closed=threading.Event();self.hold_until=0.
        self.name='Local\\JettPad.LabelSTOP.'+str(os.getpid())+'.'+uuid.uuid4().hex
        self.signal=signal_factory(self.name,create=True,initially_stopped=True)
        self.thread=threading.Thread(target=self.run,name='Orb label safety',daemon=True);self.thread.start()
    def latch(self):
        # Called before the existing revoke path does IO or ends child processes.
        with self.lock:
            if self.closed.is_set():return
            self.hold_until=time.monotonic()+.5;self.signal.latch()
    def publish(self):
        with self.lock:
            blocked=bool(self.stopped()) or time.monotonic()<self.hold_until
            if blocked:self.signal.latch()
            else:self.signal.clear_from_authorized_local_path() # Only this DISPLAY event; no authorization mutation.
            active=bool(self.executor()) and not blocked
            row={'ts':time.time(),'event':self.name,'owner_pid':os.getpid(),'stopped':blocked,'executor_running':active}
            path=self.folder/'label-guard.json';temp=path.with_suffix('.new');temp.write_text(json.dumps(row),encoding='utf-8');os.replace(temp,path)
    def run(self):
        while not self.closed.is_set():
            try:self.publish()
            except Exception:
                try:self.signal.latch()
                except Exception:pass
            self.closed.wait(.25)
    def close(self):
        if self.closed.is_set():return
        self.closed.set()
        with self.lock:self.signal.latch()
        self.thread.join(timeout=1)
        with self.lock:self.signal.close()
