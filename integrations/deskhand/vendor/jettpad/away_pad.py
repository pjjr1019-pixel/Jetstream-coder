"""Main-thread Pad parking. Files carry UI requests, never desktop grants."""
import ctypes
import json
import os
import time
from ctypes import wintypes as w
from musebridge.away import atomic


class AwayPad:
    def __init__(self, window):
        self.window=window;self.data=window.engine.data;self.active=False;self.since=None
        self.publish()
        window.root.bind('<Map>',lambda event:self.shown() if event.widget is window.root else None,add='+')

    def publish(self):
        atomic(self.data/'away-state.json',{'active':self.active,'app_pid':os.getpid(),'since':self.since})

    def shown(self):
        if self.active:
            self.active=False;self.since=None;self.publish()

    def poll(self):
        path=self.data/'away-command.json'
        try:command=json.loads(path.read_text(encoding='utf-8')[:2048])
        except (OSError,ValueError):return
        try:
            path.unlink()
            if (command.get('app_pid')!=os.getpid() or type(command.get('expires')) not in (int,float)
                    or not time.time()<command['expires']<=time.time()+4 or type(command.get('active')) is not bool):return
            from musebridge.cli import default_home
            runtime=json.loads((default_home()/'runtime.json').read_text())
            if command.get('bridge_pid')!=runtime.get('pid'):return
            if command['active']:
                # Only a foreground owner can delegate. Grant solely this bridge
                # PID, never ASFW_ANY; this is focus permission, not control approval.
                if os.name=='nt':
                    u=ctypes.WinDLL('user32',use_last_error=True)
                    u.GetForegroundWindow.restype=w.HWND
                    u.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
                    u.AllowSetForegroundWindow.argtypes=[w.DWORD]
                    pid=w.DWORD();u.GetWindowThreadProcessId(u.GetForegroundWindow(),ctypes.byref(pid))
                    if pid.value==os.getpid():u.AllowSetForegroundWindow(command['bridge_pid'])
                self.window.close()
                self.active=True;self.since=time.time();self.publish()
            else:
                self.shown();self.window.root.deiconify();self.window.root.lift()
            atomic(self.data/'away-ack.json',{'id':command['id'],'active':self.active,'ts':time.time()})
        except (OSError,ValueError,KeyError):
            atomic(self.data/'away-ack.json',{'id':command.get('id'),'error':'Pad could not complete the Away handoff','ts':time.time()})
