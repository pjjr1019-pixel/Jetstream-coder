"""Windows emergency hotkey and local input handover; never stores keystrokes."""
from __future__ import annotations
import ctypes
import os
import threading
from ctypes import wintypes as w


class DesktopGuards:
    def __init__(self, emergency, intervention, session_changed=None):
        self.emergency, self.intervention = emergency, intervention
        self.stop_event, self.ready = threading.Event(), threading.Event()
        self.thread = None
        self.available = False
        self.error = None
        self.session_changed=session_changed;self.session_watch_available=False

    def start(self):
        if os.name != "nt": return
        self.thread = threading.Thread(target=self._run, name="MuseBridge local controls", daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or not self.available:
            raise RuntimeError(self.error or "Windows local controls could not be registered")

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(2)

    def _run(self):
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
        u.SetWindowsHookExW.argtypes, u.SetWindowsHookExW.restype = [ctypes.c_int, callback_type, w.HINSTANCE, w.DWORD], w.HANDLE
        u.CallNextHookEx.argtypes, u.CallNextHookEx.restype = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM], ctypes.c_ssize_t
        u.UnhookWindowsHookEx.argtypes = [w.HANDLE]
        k.GetModuleHandleW.argtypes, k.GetModuleHandleW.restype = [w.LPCWSTR], w.HMODULE
        class KEY(ctypes.Structure):
            _fields_ = [("vk", w.DWORD), ("scan", w.DWORD), ("flags", w.DWORD), ("time", w.DWORD), ("extra", ctypes.c_size_t)]
        class MOUSE(ctypes.Structure):
            _fields_ = [("point", w.POINT), ("data", w.DWORD), ("flags", w.DWORD), ("time", w.DWORD), ("extra", ctypes.c_size_t)]
        def keyboard(code, message, address):
            if code >= 0 and not ctypes.cast(address, ctypes.POINTER(KEY)).contents.flags & 0x10:
                self.intervention()
            return u.CallNextHookEx(None, code, message, address)
        def mouse(code, message, address):
            if code >= 0 and not ctypes.cast(address, ctypes.POINTER(MOUSE)).contents.flags & 1:
                self.intervention()
            return u.CallNextHookEx(None, code, message, address)
        # References live for the whole hook lifetime. Callbacks only queue work.
        keyboard_callback, mouse_callback = callback_type(keyboard), callback_type(mouse)
        hooks = []
        watch=None
        registered = False
        try:
            registered = bool(u.RegisterHotKey(None, 1, 0x4000 | 1 | 2 | 4, 0x1B))
            hooks = [u.SetWindowsHookExW(13, keyboard_callback, k.GetModuleHandleW(None), 0),
                     u.SetWindowsHookExW(14, mouse_callback, k.GetModuleHandleW(None), 0)]
            if not registered or not all(hooks):
                self.error = "Emergency hotkey or local input handover is unavailable"
                return
            if self.session_changed:
                from .session_watch import SessionWatch
                watch=SessionWatch(self.session_changed)
                self.session_watch_available=watch.start()
            self.available = True
            self.ready.set()
            message = w.MSG()
            while not self.stop_event.is_set():
                while u.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == 0x0312:
                        self.emergency()
                    else:
                        u.TranslateMessage(ctypes.byref(message))
                        u.DispatchMessageW(ctypes.byref(message))
                self.stop_event.wait(.01)
        finally:
            if watch:watch.stop()
            self.session_watch_available=False
            self.ready.set()
            for hook in hooks:
                if hook: u.UnhookWindowsHookEx(hook)
            if registered: u.UnregisterHotKey(None, 1)
            self.available = False
