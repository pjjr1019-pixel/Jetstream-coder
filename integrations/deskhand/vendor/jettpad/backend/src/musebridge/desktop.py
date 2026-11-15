"""Opt-in Windows desktop observation and bounded input."""

from __future__ import annotations

import ctypes
import importlib.util
import io
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

from PIL import Image

from .execution import SlidingLimit, RateLimitExceeded
from .indicator import ControlIndicator
from .redaction import contains_sensitive
from .windows import window_info, capture_window, unicode_text, structured
from .window_targets import WindowCatalog, ProtectedWindow, read_window, desktop_protection, activate_window
from .menu_session import foreground_menu, MenuOpen

LOG = logging.getLogger("musebridge.desktop")


class DesktopAccess:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.enabled = False
        # B2-E (B-10): no live session exists until the bridge admits one;
        # a directly constructed DesktopAccess must not pass the authority
        # check. MuseBridge.__init__ sets this False explicitly as well.
        self.connected = False
        self.operator_pids = set()
        self._menu_session = None
        self._last_control = None
        self.armed = False
        self._active_control_operations = 0
        self.last_revocation = None
        self.catalog = WindowCatalog()
        self.window_limiter = SlidingLimit()
        self.limiter = SlidingLimit()
        self.screenshot_limiter = SlidingLimit()
        self._lock = threading.RLock()
        self._generation = 0
        self._source_size: tuple[int, int] | None = None
        self._image_size: tuple[int, int] | None = None
        self._frame_at: float | None = None
        self._frame_id: str | None = None
        self._window: dict | None = None
        self._origin = (0, 0)
        self._owner_handle = None
        self._cancel_event = threading.Event()
        self.indicator: ControlIndicator | None = None
        self.on_revoked = None
        self.remote_control = None
        self.executor_interrupt = None
        self.stream_interrupt = None
        self.executor_busy = None

    @property
    def stop_latched(self):
        # STOP latch removed (Phil 2026-11-09). Stub kept so existing readers
        # keep working: the latch is always absent.
        return False

    @staticmethod
    def _win_api():
        if os.name != "nt":
            raise ValueError("Desktop access is supported only on Windows")
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL
        user32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetUserObjectInformationW.restype = wintypes.BOOL
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, wintypes.LPVOID]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return user32, kernel32, wintypes

    def _protected_context(self) -> None:
        secure=desktop_protection()
        if secure in {'lock_screen','uac'}:raise ProtectedWindow(secure)
        info=read_window(window_info()['hwnd'])
        reason=self._target_reason(info)
        if reason:raise ProtectedWindow(reason)

    def _rate_limit(self) -> None:
        retry = self.limiter.accept(
            "desktop", self.config["rate_limits"]["desktop_per_second"], 1
        )
        if retry:
            raise RateLimitExceeded(retry, "Desktop action rate limit exceeded")

    @staticmethod
    def screenshot_available() -> bool:
        return os.name == "nt"

    @staticmethod
    def local_control_available() -> bool:
        return (
            os.name == "nt"
            and importlib.util.find_spec("pyautogui") is not None
            and importlib.util.find_spec("tkinter") is not None
        )

    def foreground_app(self) -> str:
        if os.name != "nt":
            return "unavailable"
        try:
            secure=desktop_protection()
            if secure in {'lock_screen','uac'}:return 'protected:'+secure
            info=read_window(window_info()['hwnd'])
            reason=self._target_reason(info)
            return 'protected:'+reason if reason else os.path.basename(info['path'])
        except (ValueError,OSError):return 'unknown'

    def _target_reason(self, info):
        if info.get('pid') in self.operator_pids:return 'operator_window'
        reason=info.get('protection_reason')
        return reason if reason and reason!='unknown' else None

    def _target(self, identity):
        info=self.catalog.resolve(identity)
        reason=self._target_reason(info)
        if reason:raise ProtectedWindow(reason)
        return info

    def list_windows(self):
        retry=self.window_limiter.accept('window.list',self.config['rate_limits']['desktop_per_second'],1)
        if retry:raise RateLimitExceeded(retry,'Window metadata rate limit exceeded')
        return self.catalog.enumerate(self._target_reason)

    def describe_window(self, identity=None):
        info=self.catalog.resolve(identity) if identity is not None else read_window(window_info()['hwnd'])
        return self.catalog.public(info,self._target_reason(info))

    def activate(self, identity):
        generation=self._generation
        self._ensure_authority('desktop_control')
        self._rate_limit()
        info=self._target(identity)
        self._invalidate_frame()
        activate_window(info,self._cancel_event,away=bool(getattr(self,'away_active',lambda:False)()))
        if generation!=self._generation:raise ValueError('Desktop operation was revoked')
        fresh=self._target(identity)
        if window_info()['hwnd']!=fresh['hwnd']:raise ValueError('Foreground changed during activation')
        metadata=self.catalog.public(fresh)
        return {'ok':True,'foreground_window_id':metadata['window_id'],'window':metadata,
                'requires':'Take a fresh frame before desktop input'}

    def screenshot(self, max_edge: int = 640, crop=None, window_id=None) -> dict[str, Any]:
        generation = self._generation
        self._ensure_authority('screenshots')
        if self._refresh_menu()['is_menu']:raise MenuOpen()
        if type(max_edge) is not int or not 64 <= max_edge <= 2048:
            raise ValueError("max_edge must be between 64 and 2048")
        self._invalidate_frame()
        self._last_control=None
        retry = self.screenshot_limiter.accept(
            "screenshots", self.config["rate_limits"]["screenshots_per_minute"], 60
        )
        if retry:
            raise RateLimitExceeded(retry, "Screenshot rate limit exceeded")
        targeted=window_id is not None
        if not targeted:self._protected_context()
        try:
            target=self._target(window_id) if targeted else read_window(window_info()['hwnd'])
            reason=self._target_reason(target)
            if reason:raise ProtectedWindow(reason)
            info={k:target[k] for k in ('hwnd','pid','path','rect','title')}
            self.capture_active=True
            try:
                image = capture_window(info)
            finally:
                self.capture_active=False
            if window_info(info['hwnd']) != info or (not targeted and window_info()!=info):
                raise ValueError("Window changed during capture; take a fresh screenshot")
            rechecked=read_window(info['hwnd'])
            reason=self._target_reason(rechecked)
            if reason:raise ProtectedWindow(reason)
            origin = tuple(info["rect"][:2])
            if crop is not None:
                if (not isinstance(crop, list) or len(crop) != 4 or any(type(n) is not int for n in crop)
                    or not 0 <= crop[0] < crop[2] <= image.width or not 0 <= crop[1] < crop[3] <= image.height):
                    raise ValueError("crop must be a rectangle inside the target window")
                image = image.crop(tuple(crop))
                origin = (origin[0]+crop[0], origin[1]+crop[1])
            width, height = image.size
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            data = output.getvalue()
            if len(data) > 1024 * 1024:
                raise ValueError("screenshot exceeds 1 MiB; reduce max_edge or crop")
            with self._lock:
                if generation != self._generation or not self.config["features"].get("screenshots"):
                    raise ValueError("Screenshot access was revoked during capture")
                self._source_size = (width, height)
                self._image_size = image.size
                self._frame_at = time.monotonic()
                self._frame_id = str(uuid.uuid4())
                self._window, self._origin = info, origin
                self.armed = self.enabled
            return {"format": "png", "width": image.width, "height": image.height,
                    "source_width": width, "source_height": height,
                    "frame_id": self._frame_id, "expires_in_secs": 15, "window": info,
                    "window_id": self.catalog.remember(rechecked),
                    "process_name": os.path.basename(info['path']), "title": info['title'],
                    "capture_scope": "target_window" if targeted else "foreground_window",
                    "origin": list(origin), "trust": "untrusted_application_content",
                    "data_b64": __import__("base64").b64encode(data).decode("ascii")}
        except ValueError:
            raise
        except Exception as exc:
            LOG.error("Desktop screenshot failed (%s)", type(exc).__name__)
            raise ValueError("screenshot unavailable") from None

    def _pyautogui(self, menu=False):
        if menu:
            self._ensure_authority('desktop_control')
            reason=self._target_reason(read_window(self._menu_session['owner_hwnd']))
            if reason:raise ProtectedWindow(reason)
        else:self._ensure_control_context()
        self._rate_limit()
        try:
            import pyautogui
        except ImportError:
            raise ValueError("Install the optional desktop dependencies to enable control") from None
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0
        return pyautogui

    def _desktop_point(self, x: Any, y: Any) -> tuple[int, int]:
        if type(x) is not int or type(y) is not int or self._source_size is None or self._image_size is None:
            raise ValueError("Desktop coordinates are outside the screen")
        image_width, image_height = self._image_size
        if not 0 <= x < image_width or not 0 <= y < image_height:
            raise ValueError("Desktop coordinates are outside the screenshot")
        width, height = self._source_size
        return (self._origin[0] + min(width-1, round(x*width/image_width)),
                self._origin[1] + min(height-1, round(y*height/image_height)))

    def _invalidate_frame(self) -> None:
        self._source_size = None
        self._image_size = None
        self._frame_at = None
        self._frame_id = None
        self._window = None

    @staticmethod
    def _release_input(gui, keys=(), mouse=False):
        # The corner failsafe must not prevent releasing a held button/key.
        previous = getattr(gui, "FAILSAFE", True)
        failed = False
        try:
            gui.FAILSAFE = False
            if mouse:
                try: gui.mouseUp(button="left")
                except Exception: failed = True
            for key in reversed(keys):
                try: gui.keyUp(key)
                except Exception: failed = True
        finally:
            gui.FAILSAFE = previous
        if failed:
            raise ValueError("Some input releases failed; local intervention is required")

    def _end_menu(self):
        self._menu_session=None;self._last_control=None;self._invalidate_frame()

    def _refresh_menu(self):
        candidate=self._menu_session or self._last_control
        state=foreground_menu(candidate['owner_hwnd'] if candidate else None)
        if candidate and (time.monotonic()-candidate['started_at']>=60 or
                          candidate['generation']!=self._generation or not self.connected):
            self._end_menu();return state
        if self._menu_session and not (state['is_menu'] and state['owned']):
            self._end_menu()
        elif not self._menu_session and candidate and state['is_menu'] and state['owned']:
            self._menu_session=candidate
        return state

    def menu_status(self):
        self._refresh_menu()
        return {'active':self._menu_session is not None,
                'owner_window_id':self._menu_session['owner_window_id'] if self._menu_session else None}

    def _consume_frame(self):
        if not self._menu_session:self._invalidate_frame()

    def act(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_authority('desktop_control')
        self._refresh_menu()
        session=self._menu_session
        if session:
            if operation not in {'key','click'} or payload.get('frame_id')!=session['frame_id']:
                raise ValueError('A matching menu session frame_id is required for click or key')
            for key,value in session['frame'].items():setattr(self,key,value)
            try:
                result=self._act_once(operation,payload,menu=True)
                time.sleep(.03)
                self._refresh_menu()
                return {**result,'menu_session':self.menu_status()}
            except BaseException:
                if session['generation']!=self._generation:self._end_menu()
                raise
        snapshot={key:getattr(self,key) for key in ('_frame_id','_frame_at','_window','_source_size','_image_size','_origin')}
        result=self._act_once(operation,payload)
        if snapshot['_window'] is not None:
            owner=snapshot['_window']
            self._last_control={'frame_id':snapshot['_frame_id'],'owner_hwnd':owner['hwnd'],
                'owner_window_id':self.catalog.remember(owner),'started_at':time.monotonic(),
                'generation':self._generation,'frame':snapshot}
        return result

    def _act_once(self, operation: str, payload: dict[str, Any], menu=False) -> dict[str, Any]:
        generation = self._generation
        bound_window = self._window
        if self._source_size is None or self._image_size is None or self._frame_at is None:
            raise ValueError("Take a fresh screenshot before controlling the desktop")
        if not menu and time.monotonic() - self._frame_at > 15:
            self._invalidate_frame()
            raise ValueError("Screenshot expired; take a fresh screenshot before controlling the desktop")
        gui = self._pyautogui(menu=True) if menu else self._pyautogui()
        if self.config.get("require_frame_id", True) and (not self._frame_id or payload.get("frame_id") != self._frame_id):
            raise ValueError("A matching frame_id is required")
        if not menu and (self._window is None or window_info() != self._window):
            self._invalidate_frame()
            raise ValueError("Target window changed; take a fresh screenshot")
        if generation != self._generation:
            raise ValueError("Desktop control was revoked")
        if operation in ("click", "double_click", "scroll", "move"):
            x, y = self._desktop_point(payload.get("x"), payload.get("y"))
            if operation == "click":
                button = payload.get("button", "left")
                if button not in ("left", "right", "middle"):
                    raise ValueError("Invalid mouse button")
                self._consume_frame()
                gui.click(x, y, button=button)
            elif operation == "double_click":
                self._consume_frame()
                gui.doubleClick(x, y)
            elif operation == "scroll":
                dx, dy = payload.get("dx"), payload.get("dy")
                if type(dx) is not int or type(dy) is not int or abs(dx) > 100 or abs(dy) > 100:
                    raise ValueError("Invalid scroll delta")
                self._consume_frame()
                if dx:
                    gui.hscroll(dx, x=x, y=y)
                if dy:
                    gui.scroll(dy, x=x, y=y)
            else:
                self._consume_frame()
                gui.moveTo(x, y)
        elif operation == "drag":
            x1, y1 = self._desktop_point(payload.get("x1"), payload.get("y1"))
            x2, y2 = self._desktop_point(payload.get("x2"), payload.get("y2"))
            self._consume_frame()
            gui.moveTo(x1, y1)
            try:
                gui.mouseDown(button="left")
                for step in range(1, 11):
                    if generation != self._generation or window_info() != bound_window:
                        raise ValueError("Desktop control or target window changed during drag")
                    gui.moveTo(round(x1+(x2-x1)*step/10), round(y1+(y2-y1)*step/10))
                    time.sleep(.02)
            finally:
                self._release_input(gui, mouse=True)
        elif operation == "type":
            text = payload.get("text")
            if not isinstance(text, str) or len(text) > 2000:
                raise ValueError("Invalid desktop text")
            if contains_sensitive(text):
                raise ValueError("Possible secret in desktop text; remove it before typing")
            self._consume_frame()
            # Small chunks let a local stop or disconnect interrupt long input.
            for start in range(0, len(text), 8):
                if generation != self._generation:
                    raise ValueError("Desktop control was revoked")
                self._ensure_control_context()
                if window_info() != bound_window:
                    raise ValueError("Target window changed during typing")
                unicode_text(text[start:start + 8])
        elif operation == "key":
            key = payload.get("key")
            allowed = {"enter", "tab", "escape", "up", "down", "left", "right",
                       "home", "end", "pageup", "pagedown", "backspace", "delete",
                       "space", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8",
                       "f9", "f10", "f11", "f12"}
            parts = key.lower().split("+") if isinstance(key, str) else []
            modifiers = {"ctrl", "alt", "shift"}
            if (not parts or len(parts) > 4 or len(set(parts)) != len(parts)
                or sum(part in modifiers for part in parts) > 2
                or any(
                part not in allowed and part not in modifiers and not (
                    len(part) == 1 and part.isascii() and part.isalnum()
                )
                for part in parts
            )):
                raise ValueError("Unsupported desktop key")
            self._consume_frame()
            try:
                for part in parts:
                    if generation != self._generation:
                        raise ValueError("Desktop control was revoked")
                    gui.keyDown(part)
            finally:
                self._release_input(gui, keys=parts)
        else:
            raise ValueError("Unsupported desktop action")
        return {"ok": True, "verified": False, "verification": "observe a fresh frame or use a structured action"}

    def ui(self, operation, payload):
        if operation=='inspect' and payload.get('window_id') is not None:
            generation=self._generation
            self._ensure_authority('desktop_control')
            self._rate_limit()
            info=self._target(payload['window_id'])
            result=structured(info,operation,{},self._cancel_event)
            self._target(payload['window_id'])
            if generation!=self._generation:raise ValueError('Desktop operation was revoked')
            return {**result,'window':self.catalog.public(info)}
        self._ensure_control_context()
        self._rate_limit()
        info = window_info()
        self.armed = self.enabled
        cancel = self._cancel_event
        if operation != "inspect":
            if self._window != info or payload.get("frame_id") != self._frame_id or self._frame_at is None or time.monotonic()-self._frame_at > 15:
                raise ValueError("A fresh frame_id bound to this window is required")
            if operation == "set_text":
                text = payload.get("text")
                if not isinstance(text, str) or len(text) > 2000 or contains_sensitive(text):
                    raise ValueError("Invalid or sensitive text")
            selector = payload.get("selector")
            if not isinstance(selector, dict) or not selector or set(selector)-{"name", "automation_id", "control_type"}:
                raise ValueError("Use an exact bounded selector")
            if any(not isinstance(value, str) or not 1 <= len(value) <= 200 for value in selector.values()):
                raise ValueError("Invalid selector value")
            if operation == "invoke" and (not isinstance(payload.get("expected_name"), str) or not 1 <= len(payload["expected_name"]) <= 200):
                raise ValueError("A bounded expected_name is required")
            self._invalidate_frame()
        generation = self._generation
        fields = {"inspect": set(), "set_text": {"selector", "text"}, "invoke": {"selector", "expected_name"}}.get(operation, set())
        payload = {key: value for key, value in payload.items() if key in fields}
        result = structured(info, operation, payload, cancel)
        if generation != self._generation:
            raise ValueError("Desktop control was revoked; the action outcome may be uncertain")
        return result

    @contextmanager
    def control_operation(self):
        """Keep human handover active while an operation consumes its frame."""
        with self._lock:
            if getattr(self,'phone_input_busy',lambda:False)():raise ValueError('Paired phone owns desktop input; retry after its gesture')
            if self.executor_busy and self.executor_busy():raise ValueError('Local executor owns desktop input; wait for its result or cancel it')
            self._active_control_operations += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_control_operations -= 1

    def intervene(self) -> None:
        """Human input stops live control, not an already-expired observation."""
        if self._menu_session or self._last_control:self._refresh_menu()
        with self._lock:
            if not self.enabled:
                return
            active = self._active_control_operations > 0
            fresh = (self.armed and self._frame_at is not None
                     and time.monotonic() - self._frame_at <= 15)
            fresh = fresh or self._menu_session is not None
            if not active and not fresh:
                self.armed = False
                self._invalidate_frame()
                return
        # Handover pauses runtime control; only an explicit local toggle
        # cancels the durable approval. Never auto-resume within this session.
        self.revoke(reason="local_input_handover")

    def revoke(self, *, preserve_approval=False, reason="local_or_safety_revoke") -> None:
        self._cancel_event.set()
        if self.stream_interrupt:
            try:self.stream_interrupt(reason)
            except Exception:pass  # A stream publisher fault must never block the original revoke.
        if self.executor_interrupt:self.executor_interrupt(reason)
        remote_ended=self.remote_control.revoked(reason=reason) if self.remote_control else False
        with self._lock:
            # STOP latch removed: revoke ends the session only; it never
            # latches STOP and never writes STOP state to disk.
            self._generation += 1
            was_enabled = self.enabled
            self.last_revocation = {"at": time.time(), "reason": reason,
                                    "remote_unlock_ended": remote_ended,
                                    "day_authorization_revoked":bool(self.remote_control and self.remote_control.last_day_revoked),
                                    "resume_approved": bool(self.config.get("desktop_resume_approved"))}
            self.enabled = False
            self.armed = False
            self._source_size = None
            self._image_size = None
            self._frame_at = None
            self._frame_id = None
            self._window = None
            self._menu_session=None
            self._last_control=None
            handle, self._owner_handle = self._owner_handle, None
            if handle is not None:
                _, kernel, _ = self._win_api()
                kernel.CloseHandle(handle)
        if self.indicator is not None:
            self.indicator.stop()
        if self.on_revoked is not None:
            try:
                self.on_revoked()
            except Exception as exc:
                LOG.error("Could not audit desktop-control revocation (%s)",
                          type(exc).__name__)

    def _ensure_control_context(self) -> None:
        self._ensure_authority('desktop_control')
        self._protected_context()

    def _ensure_authority(self, feature):
        from .host_stop import ensure_host_allows
        ensure_host_allows()
        if not self.config['features'].get(feature):
            raise ValueError('Screenshot access is disabled' if feature=='screenshots' else 'Desktop control is disabled')
        if not self.connected:raise ValueError('No live bridge session')
        if feature=='desktop_control' and not self.enabled:
            raise ValueError('No active control session')

    def interrupt(self):
        """Invalidate an operation without changing its local authorization."""
        with self._lock:
            self._cancel_event.set()
            self._cancel_event=threading.Event()
            self._generation+=1
            self.armed=False
            self._invalidate_frame()
            self._menu_session=None;self._last_control=None

    def disconnect(self):
        """End runtime session; never change operator approval or STOP."""
        if self.remote_control and self.remote_control.expires is not None:
            self.revoke(reason='bridge_disconnect')
        self.interrupt()
        self.connected=False;self.enabled=False
        return False

    def request_from_indicator(self) -> None:
        """A local request may use existing authority, never create or clear it."""
        if os.environ.get('DESKHAND_MUSE_SHARED_ACTIONS') == '1':
            raise ValueError('Open DeskHand, select Muse, and turn on Full PC access. This bridge indicator cannot grant host permissions.')
        if self.config['features'].get('desktop_control') is not True:
            raise ValueError('Desktop control is disabled. Review desktop permissions in DeskHand first.')
        if not self.connected:
            raise ValueError('Muse is disconnected. Reconnect Muse in DeskHand first.')
        self.enable()

    def enable(self) -> None:
        from .host_stop import ensure_host_allows
        ensure_host_allows()
        generation = self._generation
        if self.config["features"].get("desktop_control") is not True:
            raise ValueError("Desktop control is disabled in settings")
        if not self.connected:raise ValueError('No live bridge session')
        if os.name != "nt":
            raise ValueError("Desktop control is supported only on Windows")
        if self.enabled:
            return
        if self._owner_handle is not None and self.indicator is not None:
            with self._lock:
                self._cancel_event=threading.Event();self.enabled=True;self.armed=False
            return
        _, kernel, wintypes = self._win_api()
        kernel.CreateMutexW.argtypes, kernel.CreateMutexW.restype = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, "Local\\MuseBridge-Desktop-Control")
        if not handle:
            raise ValueError("Desktop ownership could not be established")
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(handle)
            raise ValueError("Another MuseBridge instance owns desktop control")
        self._owner_handle = handle
        # Phil 2026-11-09: no on-screen control indicator. Desktop control is
        # always on by his direction, so the pill is never shown and is not
        # required for control to be enabled. self.indicator stays None;
        # every other use of it already guards for None.
        indicator = None
        with self._lock:
            revoked = generation != self._generation
            if not revoked:
                self.indicator = indicator
                self._cancel_event = threading.Event()
                self.enabled = True
                self.armed = False  # Human selects target before the first observation.
        if revoked:
            self.revoke()
            raise ValueError("Desktop control was revoked during activation")

    def enable_remote_indicator(self):
        # Phil 2026-11-09: no on-screen control indicator (see enable()).
        # Remote control no longer requires a visible indicator window.
        if self.config['features']['desktop_control']:
            self.enable()
        if self.indicator is not None:
            self.indicator.set_remote(True)
