"""Atomic files, service ownership and conservative Windows scheduling facts."""
import ctypes
import json
import os
from pathlib import Path
import uuid


def atomic_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=True,allow_nan=False);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


class Ownership:
    def __init__(self,path): self.path=Path(path);self.stream=None
    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.stream=self.path.open('a+b')
        if not self.path.stat().st_size:self.stream.write(b'0');self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return True
        except OSError:self.stream.close();self.stream=None;return False
    def close(self):
        if self.stream:self.stream.close();self.stream=None


def machine_facts():
    if os.name!='nt':return {'memory_free':None,'ac':None,'idle_seconds':None,'fullscreen':None}
    from ctypes import wintypes as w
    class MEMORY(ctypes.Structure):
        _fields_=[('length',w.DWORD),('load',w.DWORD)]+[(n,ctypes.c_ulonglong) for n in
            ('total','available','total_page','avail_page','total_virtual','avail_virtual','extended')]
    class POWER(ctypes.Structure):
        _fields_=[('ac',w.BYTE),('flag',w.BYTE),('percent',w.BYTE),('reserved',w.BYTE),('life',w.DWORD),('full_life',w.DWORD)]
    class INPUT(ctypes.Structure):_fields_=[('size',w.UINT),('tick',w.DWORD)]
    k=ctypes.WinDLL('kernel32');u=ctypes.WinDLL('user32')
    memory=MEMORY();memory.length=ctypes.sizeof(memory)
    free=memory.available if k.GlobalMemoryStatusEx(ctypes.byref(memory)) else None
    power=POWER();ac=(power.ac==1) if k.GetSystemPowerStatus(ctypes.byref(power)) and power.ac!=255 else None
    last=INPUT();last.size=ctypes.sizeof(last)
    idle=((k.GetTickCount()-last.tick)&0xffffffff)/1000 if u.GetLastInputInfo(ctypes.byref(last)) else None
    u.GetForegroundWindow.restype=w.HWND
    u.GetWindowRect.argtypes=[w.HWND,ctypes.POINTER(w.RECT)]
    u.MonitorFromWindow.argtypes=[w.HWND,w.DWORD];u.MonitorFromWindow.restype=w.HANDLE
    class MONITOR(ctypes.Structure):_fields_=[('size',w.DWORD),('monitor',w.RECT),('work',w.RECT),('flags',w.DWORD)]
    u.GetMonitorInfoW.argtypes=[w.HANDLE,ctypes.POINTER(MONITOR)]
    hwnd=u.GetForegroundWindow();rect=w.RECT();mon=MONITOR();mon.size=ctypes.sizeof(mon)
    fullscreen=None
    if hwnd and u.GetWindowRect(hwnd,ctypes.byref(rect)) and u.GetMonitorInfoW(u.MonitorFromWindow(hwnd,2),ctypes.byref(mon)):
        fullscreen=rect.left<=mon.monitor.left and rect.top<=mon.monitor.top and rect.right>=mon.monitor.right and rect.bottom>=mon.monitor.bottom
    return {'memory_free':free,'ac':ac,'idle_seconds':idle,'fullscreen':fullscreen}
