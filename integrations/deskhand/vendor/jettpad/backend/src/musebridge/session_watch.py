"""Windows session notifications and a fail-closed interactive-desktop check."""
import ctypes,os
from ctypes import wintypes as w

def interactive_session():
    if os.name!='nt':return False
    u=ctypes.WinDLL('user32',use_last_error=True);k=ctypes.WinDLL('kernel32',use_last_error=True)
    t=ctypes.WinDLL('wtsapi32',use_last_error=True)
    session=w.DWORD();k.ProcessIdToSessionId.argtypes=[w.DWORD,ctypes.POINTER(w.DWORD)]
    if not k.ProcessIdToSessionId(os.getpid(),ctypes.byref(session)):return False
    t.WTSQuerySessionInformationW.argtypes=[w.HANDLE,w.DWORD,ctypes.c_int,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(w.DWORD)]
    t.WTSFreeMemory.argtypes=[ctypes.c_void_p]
    value=ctypes.c_void_p();size=w.DWORD()
    if not t.WTSQuerySessionInformationW(None,session.value,8,ctypes.byref(value),ctypes.byref(size)):return False
    try:
        if size.value<4 or ctypes.cast(value,ctypes.POINTER(ctypes.c_int)).contents.value!=0:return False
    finally:t.WTSFreeMemory(value)
    u.OpenInputDesktop.argtypes=[w.DWORD,w.BOOL,w.DWORD];u.OpenInputDesktop.restype=w.HANDLE
    u.GetUserObjectInformationW.argtypes=[w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD)]
    u.CloseDesktop.argtypes=[w.HANDLE]
    desktop=u.OpenInputDesktop(0,False,1)
    if not desktop:return False
    try:
        name=ctypes.create_unicode_buffer(256);needed=w.DWORD()
        return bool(u.GetUserObjectInformationW(desktop,2,name,ctypes.sizeof(name),ctypes.byref(needed)) and name.value.casefold()=='default')
    finally:u.CloseDesktop(desktop)

class SessionWatch:
    """Created and pumped on DesktopGuards' existing message thread."""
    def __init__(self,changed):self.changed=changed;self.hwnd=None;self.registered=False
    def start(self):
        u=self.user=ctypes.WinDLL('user32',use_last_error=True)
        k=self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        t=self.wts=ctypes.WinDLL('wtsapi32',use_last_error=True)
        callback=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM)
        class WNDCLASS(ctypes.Structure):
            _fields_=[('style',w.UINT),('proc',callback),('cls_extra',ctypes.c_int),('wnd_extra',ctypes.c_int),('instance',w.HINSTANCE),('icon',w.HICON),('cursor',w.HANDLE),('background',w.HBRUSH),('menu',w.LPCWSTR),('name',w.LPCWSTR)]
        u.DefWindowProcW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM];u.DefWindowProcW.restype=ctypes.c_ssize_t
        def procedure(hwnd,msg,wp,lp):
            if msg==0x2B1 and wp in (1,2,3,4,5,6,7,8,9,11):self.changed('windows_session_changed')
            elif msg in (0x11,0x16):self.changed('windows_sign_out')
            return u.DefWindowProcW(hwnd,msg,wp,lp)
        self.proc=callback(procedure);self.name='MuseBridgePrivateSessionWatch-'+str(os.getpid())
        k.GetModuleHandleW.argtypes=[w.LPCWSTR];k.GetModuleHandleW.restype=w.HMODULE
        self.instance=k.GetModuleHandleW(None);wc=WNDCLASS();wc.proc=self.proc;wc.instance=self.instance;wc.name=self.name
        u.RegisterClassW.argtypes=[ctypes.POINTER(WNDCLASS)]
        if not u.RegisterClassW(ctypes.byref(wc)):return False
        u.CreateWindowExW.argtypes=[w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HWND,w.HMENU,w.HINSTANCE,w.LPVOID]
        u.CreateWindowExW.restype=w.HWND
        # Invisible top-level window receives WTS changes and shutdown broadcasts.
        self.hwnd=u.CreateWindowExW(0,self.name,'MuseBridge operator session watch',0,0,0,0,0,None,None,self.instance,None)
        t.WTSRegisterSessionNotification.argtypes=[w.HWND,w.DWORD]
        self.registered=bool(self.hwnd and t.WTSRegisterSessionNotification(self.hwnd,0))
        return self.registered
    def stop(self):
        if self.registered:
            self.wts.WTSUnRegisterSessionNotification.argtypes=[w.HWND];self.wts.WTSUnRegisterSessionNotification(self.hwnd)
        if self.hwnd:
            self.user.DestroyWindow.argtypes=[w.HWND];self.user.DestroyWindow(self.hwnd)
        if hasattr(self,'user'):
            self.user.UnregisterClassW.argtypes=[w.LPCWSTR,w.HINSTANCE];self.user.UnregisterClassW(self.name,self.instance)
        self.hwnd=None;self.registered=False
