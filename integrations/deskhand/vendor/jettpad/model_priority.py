"""Cross-process local model admission, yielding at completed HTTP calls.

OS-held locks release automatically on process death. Renewed demand leases
give interactive callers priority without permanently blocking crew on a crash.
No model is interrupted, unloaded, or given additional execution authority.
"""
from contextlib import contextmanager
from pathlib import Path
import os
import threading
import time
import uuid


class ModelPriority:
    def __init__(self, folder=None, *, lease_seconds=30, heartbeat_seconds=2):
        self.folder=Path(folder) if folder is not None else Path(os.environ['LOCALAPPDATA'])/'JettPad'
        self.root=self.folder/'model-admission'
        self.lease_seconds=lease_seconds
        self.heartbeat_seconds=heartbeat_seconds

    def interactive_waiting(self):
        self.root.mkdir(parents=True,exist_ok=True)
        now=time.time()
        for marker in self.root.glob('interactive-*.lease'):
            try:
                if now-marker.stat().st_mtime <= self.lease_seconds:
                    return True
                # Old demand is not authority; never hold background work forever.
            except FileNotFoundError:
                continue
        return False

    @contextmanager
    def demand(self):
        self.root.mkdir(parents=True,exist_ok=True)
        marker=self.root/('interactive-'+uuid.uuid4().hex+'.lease')
        marker.touch(exist_ok=False)
        stop=threading.Event()
        def renew():
            while not stop.wait(self.heartbeat_seconds):
                try:marker.touch()
                except OSError:pass  # current OS lock still prevents overlap
        thread=threading.Thread(target=renew,daemon=True,name='Jett interactive model demand')
        thread.start()
        try:yield
        finally:
            stop.set();thread.join();marker.unlink(missing_ok=True)

    @staticmethod
    def _try_lock(stream):
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:return False
        else:
            import fcntl
            try:fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
        return True

    @staticmethod
    def _unlock(stream):
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

    @contextmanager
    def _slot(self, interactive, cancel):
        self.root.mkdir(parents=True,exist_ok=True)
        with (self.root/'model.lock').open('a+b') as stream:
            if not stream.seek(0,2):stream.write(b'0');stream.flush()
            locked=False
            try:
                while not locked:
                    if cancel is not None and cancel():raise RuntimeError('Model admission cancelled')
                    if interactive or not self.interactive_waiting():
                        locked=self._try_lock(stream)
                        if locked and not interactive and self.interactive_waiting():
                            self._unlock(stream);locked=False
                    if not locked:time.sleep(.05)
                yield
            finally:
                if locked:self._unlock(stream)

    @contextmanager
    def interactive(self, cancel=None):
        with self.demand(), self._slot(True,cancel):
            yield

    def background(self, cancel=None):
        return self._slot(False,cancel)
