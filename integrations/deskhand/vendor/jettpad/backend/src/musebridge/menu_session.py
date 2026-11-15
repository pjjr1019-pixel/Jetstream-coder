"""Recognize OS menu mode and bind it to its actual owner, never just a PID."""
import ctypes
from ctypes import wintypes as w
from .windows import api


class MenuOpen(ValueError):
    code='menu_open'
    def __init__(self):super().__init__('menu open')


def foreground_menu(owner=None):
    u,_=api();hwnd=u.GetForegroundWindow()
    if not hwnd:return {'is_menu':False,'owned':False}
    u.GetClassNameW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
    u.GetAncestor.argtypes=[w.HWND,w.UINT];u.GetAncestor.restype=w.HWND
    u.GetWindow.argtypes=[w.HWND,w.UINT];u.GetWindow.restype=w.HWND
    u.IsChild.argtypes=[w.HWND,w.HWND]
    class GUI(ctypes.Structure):
        _fields_=[('size',w.DWORD),('flags',w.DWORD),('active',w.HWND),('focus',w.HWND),
                  ('capture',w.HWND),('menu_owner',w.HWND),('move',w.HWND),('caret',w.HWND),('rect',w.RECT)]
    state=GUI();state.size=ctypes.sizeof(state)
    u.GetGUIThreadInfo.argtypes=[w.DWORD,ctypes.POINTER(GUI)]
    thread=u.GetWindowThreadProcessId(hwnd,None)
    valid=u.GetGUIThreadInfo(thread,ctypes.byref(state))
    name=ctypes.create_unicode_buffer(128);u.GetClassNameW(hwnd,name,len(name))
    is_menu=name.value=='#32768' or bool(valid and state.flags&(4|8|16))
    candidates=[u.GetWindow(hwnd,4),u.GetAncestor(hwnd,3),state.menu_owner if valid else None]
    owned=bool(owner and is_menu and any(candidate and
        (candidate==owner or u.IsChild(owner,candidate) or u.GetAncestor(candidate,3)==owner)
        for candidate in candidates))
    return {'is_menu':is_menu,'owned':owned,'foreground_hwnd':hwnd,
            'menu_owner_hwnd':state.menu_owner if valid else None}
