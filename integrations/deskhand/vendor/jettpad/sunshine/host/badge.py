"""Backend-owned visible badge + independent fail-safe; no network listener.

Parent sends one private stdin JSON record with an exact process identity,
then heartbeat records. No auth secrets are accepted or logged.
"""
from __future__ import annotations
import ctypes,json,os,sys,threading,time
from ctypes import wintypes
from pathlib import Path
import tkinter as tk

def run():
    from .windows import StopSignal,creation_time
    record=json.loads(sys.stdin.readline())
    signal=StopSignal(record['stop_event'])
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT];kernel.TerminateProcess.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype=wintypes.BOOL
    process=kernel.OpenProcess(0x100001|0x1000,False,int(record['pid']))
    owner=kernel.OpenProcess(0x100000|0x1000,False,int(record['owner_pid']))
    publisher=None
    if not process or not owner:raise OSError('Cannot verify streaming process owner')
    try:
        if creation_time(owner)!=record['owner_created']:raise OSError('Streaming owner identity changed')
        identity=record.get('publisher')
        if identity:
            if not identity.get('owner_created'):raise OSError('Live STOP publisher identity missing')
            publisher=kernel.OpenProcess(0x100000|0x1000,False,int(identity['owner_pid']))
            if not publisher or creation_time(publisher)!=identity['owner_created']:raise OSError('STOP publisher identity changed')
        elif not record.get('guard_test'):raise OSError('Live STOP publisher identity missing')
        size=wintypes.DWORD(32768);image=ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(process,0,image,ctypes.byref(size)):raise OSError('Cannot identify streaming process')
        if Path(image.value).resolve()!=Path(record['executable']).resolve() or creation_time(process)!=record['created']:
            raise OSError('Streaming process identity mismatch')
        if signal.stopped():raise OSError('STOP is latched')
        parent_tick=[time.monotonic()];ui_tick=[time.monotonic()];ending=threading.Event()
        def incoming():
            try:
                for line in sys.stdin:
                    msg=json.loads(line)
                    if msg.get('stop'):break
                    if msg.get('heartbeat') is True:parent_tick[0]=time.monotonic()
            finally:ending.set()
        def guard():
            while not ending.wait(.02):
                try:
                    now=time.monotonic()
                    if signal.stopped() or kernel.WaitForSingleObject(owner,0)!=258 or (publisher and kernel.WaitForSingleObject(publisher,0)!=258) or now-parent_tick[0]>1.5 or now-ui_tick[0]>1.5:
                        break
                except Exception:break
            # A held kernel handle prevents PID reuse from targeting an unrelated process.
            kernel.TerminateProcess(process,1)
            os._exit(0)
        threading.Thread(target=incoming,daemon=True).start()
        root=tk.Tk();root.title('Jett Pad — Desktop streaming');root.overrideredirect(True);root.attributes('-topmost',True)
        root.configure(bg='#0A0A0A')
        label=tk.Label(root,text=('JETT GUARD TEST · NO STREAM' if record.get('guard_test') else '● DESKTOP STREAMING ON'),fg='#D4AF37',bg='#0A0A0A',font=('Segoe UI',12,'bold'),padx=12,pady=8)
        label.pack()
        root.geometry('+%d+16'%max(0,root.winfo_screenwidth()-310))
        def stop():
            # Terminate immediately; record local intent so phone retries cannot
            # mistake this explicit stop for a crash and restart capture.
            kernel.TerminateProcess(process,1);ending.set()
            try:
                marker=Path(record['local_stop_marker']);temporary=marker.with_suffix('.tmp')
                temporary.write_text(json.dumps({'ts':time.time(),'pid':record['pid'],'created':record['created']}));os.replace(temporary,marker)
            except (OSError,KeyError):pass
        tk.Button(root,text='STOP STREAM',command=stop,bg='#C9A227',fg='#0A0A0A',relief='flat').pack(fill='x')
        root.protocol('WM_DELETE_WINDOW',stop)
        def tick():
            if not root.winfo_viewable():stop();return
            ui_tick[0]=time.monotonic();root.attributes('-topmost',True);root.after(100,tick)
        root.update_idletasks();root.update()
        if not root.winfo_viewable():raise OSError('Streaming badge is not visible')
        threading.Thread(target=guard,daemon=True).start()
        print(json.dumps({'ready':True,'pid':os.getpid(),'visible':True}),flush=True)
        root.after(100,tick);root.mainloop()
    finally:
        kernel.TerminateProcess(process,1)
        kernel.CloseHandle(process);kernel.CloseHandle(owner)
        if publisher:kernel.CloseHandle(publisher)
        signal.close()

if __name__=='__main__':run()
