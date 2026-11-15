"""Wake the installed Edge in the background; the extension opens only Muse."""
import os
from pathlib import Path
import subprocess
import time


class BrowserLauncher:
    def __init__(self, *, launch=subprocess.Popen, clock=time.monotonic, edge=None):
        self.launch, self.clock, self.edge = launch, clock, edge
        self.delay, self.next_attempt, self.connected_since = 5, 0, None
        self.background_attempted = False
        self.window_requested = False

    def ensure(self, *, connected, pending, new_message=False):
        now=self.clock()
        if connected:
            self.background_attempted = False
            self.window_requested = False
            if self.connected_since is None: self.connected_since=now
            if now-self.connected_since>30: self.delay=5
            if not new_message: return
        else: self.connected_since=None
        if not pending or now<self.next_attempt: return
        candidates=[Path(value)/'Microsoft'/'Edge'/'Application'/'msedge.exe'
                    for value in (os.environ.get('PROGRAMFILES(X86)',''),os.environ.get('PROGRAMFILES',''),
                                  os.environ.get('LOCALAPPDATA','')) if value]
        executable=Path(self.edge) if self.edge else next((p for p in candidates if p.is_file()),None)
        if executable is None: raise OSError('Installed Microsoft Edge was not found')
        # Edge may exit successfully without starting extensions when all its
        # windows are closed. A missing heartbeat after the first wake needs a
        # real Muse window, not an endless loop of background-only launches.
        open_window = not connected and self.background_attempted and not self.window_requested
        self.next_attempt=now+self.delay
        self.delay=min(30,self.delay*2)
        options={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
        if os.name=='nt':
            startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow=4 if open_window else 0  # Show without requesting focus.
            options.update(startupinfo=startup,creationflags=subprocess.CREATE_NO_WINDOW)
        args = [str(executable),'--new-window','https://muse.ai/'] if open_window else [str(executable),'--no-startup-window']
        self.launch(args,**options)
        if open_window: self.window_requested = True
        self.background_attempted = True
