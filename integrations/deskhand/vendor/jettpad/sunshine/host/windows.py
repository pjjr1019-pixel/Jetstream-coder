"""Windows process/event primitives shared by staged and production integration.

No Sunshine is started by importing this module. The live bridge has NOT yet
been wired to the STOP signal; absence of that hook prevents launch.
"""
from __future__ import annotations
import ctypes,os,subprocess,threading
from ctypes import wintypes
from pathlib import Path

class StopSignal:
    def __init__(self,name:str, *, create:bool=False,initially_stopped:bool=True):
        if os.name!='nt':raise OSError('Windows only')
        self._handle_lock=threading.RLock()
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateEventW.argtypes=[wintypes.LPVOID,wintypes.BOOL,wintypes.BOOL,wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype=wintypes.HANDLE
        self.kernel.OpenEventW.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR]
        self.kernel.OpenEventW.restype=wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];self.kernel.WaitForSingleObject.restype=wintypes.DWORD
        for entry in ('SetEvent','ResetEvent','CloseHandle'):
            fn=getattr(self.kernel,entry);fn.argtypes=[wintypes.HANDLE];fn.restype=wintypes.BOOL
        self.creator=create
        self.handle=(self.kernel.CreateEventW(None,True,initially_stopped,name) if create else self.kernel.OpenEventW(0x100000,False,name))
        if not self.handle:raise OSError('Required STOP notification is unavailable; streaming remains OFF')
    def stopped(self):
        with self._handle_lock:result=self.kernel.WaitForSingleObject(self.handle,0)
        if result not in (0,258):raise OSError('STOP notification failed')
        return result==0
    def latch(self):
        with self._handle_lock:
            if not self.creator or not self.kernel.SetEvent(self.handle):raise OSError('Cannot latch STOP signal')
    def clear_from_authorized_local_path(self):
        # Only the bridge's existing local/PIN/day authorization path may call this.
        with self._handle_lock:
            if not self.creator or not self.kernel.ResetEvent(self.handle):raise OSError('Cannot clear STOP signal')
    def close(self):
        with self._handle_lock:
            if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None

def creation_time(process_handle) -> int:
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.GetProcessTimes.restype=wintypes.BOOL
    values=[wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(process_handle,*map(ctypes.byref,values)):raise OSError('Cannot verify process identity')
    return values[0].dwLowDateTime | values[0].dwHighDateTime<<32

def current_creation_time() -> int:
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    return creation_time(kernel.GetCurrentProcess())

class ProcessWitness:
    """Read-only process lifetime/creation identity; never targets by a reused PID."""
    def __init__(self,pid:int,created:int):
        self._handle_lock=threading.RLock()
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];self.kernel.OpenProcess.restype=wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];self.kernel.WaitForSingleObject.restype=wintypes.DWORD
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE];self.kernel.CloseHandle.restype=wintypes.BOOL
        self.handle=self.kernel.OpenProcess(0x100000|0x1000,False,pid)
        if not self.handle:raise OSError('Cannot verify required process identity')
        try:
            if creation_time(self.handle)!=created or not self.alive():raise OSError('Required process identity changed or exited')
        except Exception:self.close();raise
    def alive(self):
        with self._handle_lock:return self.handle is not None and self.kernel.WaitForSingleObject(self.handle,0)==258
    def close(self):
        with self._handle_lock:
            if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None

class SuspendedOwnedProcess:
    """Uses the existing tested app JobObject isolation; register before resume."""
    def __init__(self,args:list[str],cwd:Path,log, *, job_factory=None):
        if job_factory is None:
            import hashlib
            from musebridge import execution
            expected='b5631045b7a604f940fb4a838f1233ed1efff03866ea4300d7a9e5f1ce943628'
            if hashlib.sha256(Path(execution.__file__).read_bytes()).hexdigest()!=expected:
                raise OSError('App JobObject implementation changed; review before enabling streaming')
            job_factory=execution._WindowsJob
        self._lock=threading.Lock();self._closed=False;self._job=None;self.process=None
        self.process=subprocess.Popen(args,cwd=str(cwd),stdin=subprocess.DEVNULL,stdout=log,stderr=log,
            creationflags=0x00000004|0x08000000,close_fds=True)
        try:self._job=job_factory(self.process)
        except Exception:
            self.process.kill();self.process.wait(timeout=5);raise
        try:self.created=creation_time(self.process._handle)
        except Exception:self.terminate_now();raise
    def resume(self):
        with self._lock:
            if self._closed:raise OSError('Process was stopped before resume')
            self._job.resume(self.process)
    def terminate_now(self):
        with self._lock:
            if self._closed:return
            self._closed=True
            try:self._job.terminate()
            finally:self._job.close()
    def alive(self):return not self._closed and self.process.poll() is None
