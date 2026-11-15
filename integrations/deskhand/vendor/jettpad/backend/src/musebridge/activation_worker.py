"""Disposable native foreground helper; no keys, mouse input or global settings.

Input queues are attached only inside this short-lived process. The parent
terminates it on STOP or a hard deadline, including an unresponsive target.
"""
import ctypes
import json
import sys
from ctypes import wintypes as w
from .windows import api
from .window_targets import read_window,desktop_protection


def run(info):
    if desktop_protection() is not None:raise ValueError('Interactive desktop unavailable')
    fresh=read_window(info['hwnd'])
    if fresh['pid']!=info['pid'] or fresh.get('protection_reason'):raise ValueError('Window changed or protected')
    u,k=api()
    u.AttachThreadInput.argtypes=[w.DWORD,w.DWORD,w.BOOL]
    u.SetForegroundWindow.argtypes=[w.HWND]
    u.GetAncestor.argtypes=[w.HWND,w.UINT];u.GetAncestor.restype=w.HWND
    u.BringWindowToTop.argtypes=[w.HWND]
    k.GetCurrentThreadId.restype=w.DWORD
    current=k.GetCurrentThreadId();foreground=u.GetForegroundWindow()
    # Materialize our message queue before attaching it.
    message=w.MSG();u.PeekMessageW(ctypes.byref(message),None,0,0,0)
    attached=[]
    try:
        for hwnd in (foreground,info['hwnd']):
            thread=u.GetWindowThreadProcessId(hwnd,None) if hwnd else 0
            if thread and thread!=current and thread not in attached:
                if not u.AttachThreadInput(current,thread,True):raise ValueError('Windows declined input-queue handoff')
                attached.append(thread)
        if desktop_protection() is not None:raise ValueError('Interactive desktop changed')
        u.BringWindowToTop(info['hwnd']);u.SetForegroundWindow(info['hwnd'])
    finally:
        for thread in reversed(attached):u.AttachThreadInput(current,thread,False)


if __name__=='__main__':
    try:run(json.loads(sys.stdin.read(2048)));result={'ok':True}
    except Exception as error:result={'ok':False,'error':str(error)[:200]}
    sys.stdout.write(json.dumps(result));sys.stdout.flush()
