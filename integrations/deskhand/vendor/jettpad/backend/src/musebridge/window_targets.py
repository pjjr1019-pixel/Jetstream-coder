"""Top-level window metadata and opaque, process-bound session identities.

Never switches desktops, bypasses a secure desktop or changes focus to capture.
"""
import ctypes
from ctypes import wintypes as w
import os
import threading
import time
import uuid
from .windows import api, window_info
from .redaction import redact_text


class ProtectedWindow(ValueError):
    code = 'invalid_request'
    def __init__(self, reason):
        self.protection_reason = reason
        super().__init__('protected context: '+reason)


def desktop_protection():
    """Name only a secure state we can establish; don't guess lock vs UAC."""
    u,_=api()
    u.OpenInputDesktop.argtypes=[w.DWORD,w.BOOL,w.DWORD]
    u.OpenInputDesktop.restype=w.HANDLE
    u.CloseDesktop.argtypes=[w.HANDLE]
    u.GetUserObjectInformationW.argtypes=[w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD)]
    handle=u.OpenInputDesktop(0,False,1)
    if not handle:return 'unknown'
    try:
        name=ctypes.create_unicode_buffer(256);size=w.DWORD()
        if not u.GetUserObjectInformationW(handle,2,name,ctypes.sizeof(name),ctypes.byref(size)):
            return 'unknown'
        if name.value.casefold()=='default':return None
        # WTSSessionInfoEx gives an explicit locked flag, unlike desktop name.
        try:
            k=ctypes.WinDLL('kernel32',use_last_error=True)
            session=w.DWORD();k.ProcessIdToSessionId(os.getpid(),ctypes.byref(session))
            t=ctypes.WinDLL('wtsapi32',use_last_error=True)
            t.WTSQuerySessionInformationW.argtypes=[w.HANDLE,w.DWORD,ctypes.c_int,ctypes.POINTER(w.LPVOID),ctypes.POINTER(w.DWORD)]
            t.WTSFreeMemory.argtypes=[w.LPVOID]
            pointer=w.LPVOID();length=w.DWORD()
            if t.WTSQuerySessionInformationW(None,session.value,25,ctypes.byref(pointer),ctypes.byref(length)):
                try:
                    # WTSINFOEX: DWORD level, 8-byte aligned union; level1's
                    # first three DWORDs are SessionId, State and SessionFlags.
                    if length.value>=20 and ctypes.c_uint32.from_address(pointer.value).value==1:
                        flags=ctypes.c_int32.from_address(pointer.value+16).value
                        if flags==0:return 'lock_screen'
                        if flags==1:return 'uac'
                finally:t.WTSFreeMemory(pointer)
        except (OSError,AttributeError):pass
        return 'unknown'
    finally:u.CloseDesktop(handle)


def read_window(hwnd):
    u,_=api()
    u.IsWindow.argtypes=[w.HWND];u.IsWindowVisible.argtypes=[w.HWND]
    if not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd):
        raise ValueError('Window is closed or no longer visible')
    try:
        info=window_info(hwnd)
    except ValueError:
        # Keep an inaccessible top-level window in metadata, fail closed.
        rect=w.RECT();pid=w.DWORD();title=ctypes.create_unicode_buffer(512)
        u.GetWindowRect(hwnd,ctypes.byref(rect));u.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        u.GetWindowTextW(hwnd,title,len(title))
        info={'hwnd':hwnd,'pid':pid.value,'path':'','title':title.value,
              'rect':[rect.left,rect.top,rect.right,rect.bottom]}
    u.IsIconic.argtypes=u.IsZoomed.argtypes=[w.HWND]
    info['state']='minimized' if u.IsIconic(hwnd) else 'maximized' if u.IsZoomed(hwnd) else 'normal'
    reason=None
    process=os.path.basename(info['path']).casefold();title=info['title'].casefold()
    if process in {'lockapp.exe','logonui.exe','winlogon.exe'}:reason='lock_screen'
    elif process=='consent.exe':reason='uac'
    elif process in {'credentialuibroker.exe','creduihost.exe'}:reason='credential_ui'
    elif process in {'1password.exe','bitwarden.exe','keepass.exe','keepassxc.exe','lastpass.exe','vault.exe'}:
        reason='credential_ui'
    elif ((process in {'python.exe','pythonw.exe','msedge.exe','chrome.exe','firefox.exe','jettpad.exe'}
           and title.startswith(('musebridge desktop control','musebridge operator','musebridge control','jett pad')))
          or any(x in title for x in ('operator.secret','musebridge.config.json'))):
        reason='operator_window'
    affinity=w.DWORD()
    u.GetWindowDisplayAffinity.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
    if reason is None and u.GetWindowDisplayAffinity(hwnd,ctypes.byref(affinity)) and affinity.value in (1,0x11):
        reason='drm'
    # Win32 password fields are protected regardless of which child has focus.
    callback_type=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    u.GetClassNameW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
    u.GetWindowLongW.argtypes=[w.HWND,ctypes.c_int]
    def child_check(child,unused):
        nonlocal reason
        name=ctypes.create_unicode_buffer(128);u.GetClassNameW(child,name,len(name))
        if u.IsWindowVisible(child) and 'edit' in name.value.casefold() and u.GetWindowLongW(child,-16)&0x20:
            reason='credential_ui';return False
        return True
    callback=callback_type(child_check)
    u.EnumChildWindows.argtypes=[w.HWND,callback_type,w.LPARAM]
    if reason is None and info['state']!='minimized':u.EnumChildWindows(hwnd,callback,0)
    info['classification_unknown']=not bool(info['path'])
    info['protection_reason']=reason
    return info


class WindowCatalog:
    def __init__(self):
        self.ids={};self.by_handle={};self.lock=threading.RLock()

    def remember(self,info):
        key=(info['hwnd'],info.get('pid',0))
        with self.lock:
            identity=self.by_handle.get(key)
            if identity is None:
                identity=str(uuid.uuid4());self.by_handle[key]=identity;self.ids[identity]=key
            return identity

    def resolve(self,identity):
        if not isinstance(identity,str):raise ValueError('window_id must be a listed window id')
        with self.lock:key=self.ids.get(identity)
        if key is None:raise ValueError('Unknown or expired window_id; call window.list')
        info=read_window(key[0])
        if (info['hwnd'],info['pid'])!=key:
            raise ValueError('Window identity changed; call window.list')
        return info

    def public(self,info,reason=None):
        u,_=api();left,top,right,bottom=info['rect']
        return {'window_id':self.remember(info),'process_name':os.path.basename(info['path']),
                'title':redact_text(info['title']),'bounds':{'x':left,'y':top,'width':right-left,'height':bottom-top},
                'state':info['state'],'is_foreground':u.GetForegroundWindow()==info['hwnd'],
                'protected':reason is not None,'classification_unknown':info.get('classification_unknown',False),
                **({'protection_reason':reason} if reason else {})}

    def enumerate(self,protection):
        u,_=api();rows=[];seen=set();errors=[]
        callback_type=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
        def visit(hwnd,unused):
            if len(rows)>=512:return False
            if u.IsWindowVisible(hwnd):
                try:
                    info=read_window(hwnd);seen.add((info['hwnd'],info['pid']))
                    rows.append(self.public(info,protection(info)))
                except (ValueError,OSError):errors.append(True)
            return True
        callback=callback_type(visit);u.EnumWindows.argtypes=[callback_type,w.LPARAM]
        u.IsWindowVisible.argtypes=[w.HWND]
        u.EnumWindows(callback,0)
        # Expire closed/replaced identities. Never silently retarget an old id.
        with self.lock:
            for key in list(self.by_handle):
                if key not in seen:
                    self.ids.pop(self.by_handle.pop(key),None)
        return {'windows':rows,'truncated':len(rows)>=512,'desktop_protection':desktop_protection(),
                'metadata_unavailable':len(errors),'trust':'untrusted_application_content'}


def activate_window(info,cancel,away=False):
    secure=desktop_protection()
    if secure in {'lock_screen','uac'}:raise ProtectedWindow(secure)
    u,_=api()
    u.ShowWindowAsync.argtypes=[w.HWND,ctypes.c_int]
    u.SetForegroundWindow.argtypes=[w.HWND]
    if cancel.is_set():raise ValueError('Desktop operation was revoked')
    if info['state']=='minimized':u.ShowWindowAsync(info['hwnd'],9)
    u.SetForegroundWindow(info['hwnd'])
    deadline=time.monotonic()+(.15 if away else 2)
    while time.monotonic()<deadline:
        if cancel.is_set():raise ValueError('Desktop operation was revoked')
        if u.GetForegroundWindow()==info['hwnd']:return
        time.sleep(.025)
    if away:
        # A hung input queue must not hang the bridge or delay STOP. The
        # disposable process owns all attachments and is killed on cancellation.
        import json,subprocess,sys
        from .runtime_paths import helper_command
        child=subprocess.Popen(helper_command('musebridge.activation_worker'),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            child.stdin.write(json.dumps({'hwnd':info['hwnd'],'pid':info['pid']}).encode());child.stdin.close()
            deadline=time.monotonic()+2
            while child.poll() is None:
                if cancel.wait(.025):raise ValueError('Desktop operation was revoked')
                if time.monotonic()>=deadline:raise ValueError('Windows activation helper timed out; target may be unresponsive')
            if cancel.is_set():raise ValueError('Desktop operation was revoked')
            if u.GetForegroundWindow()==info['hwnd']:return
        finally:
            if child.poll() is None:child.kill()
            child.wait();child.stdout.close()
    raise ValueError('Windows declined window activation; select the window locally')
