"""Always-on-top, one-click revocation notice for desktop control."""

from __future__ import annotations

import threading
import ctypes
import os
import logging
from typing import Callable

LOG = logging.getLogger("musebridge.indicator")


def indicator_active(callback):
    try:return True if callback is None else bool(callback())
    except Exception:return False


def indicator_size(dpi):
    """Match the 220-by-32 logical-pixel orb badge on the current monitor."""
    scale=max(.75,min(4.,dpi/96.))
    return round(220*scale),round(32*scale),scale


def _dpi_context():
    if os.name!='nt':return None,None
    try:
        user=ctypes.WinDLL('user32',use_last_error=True)
        user.SetThreadDpiAwarenessContext.argtypes=[ctypes.c_void_p]
        user.SetThreadDpiAwarenessContext.restype=ctypes.c_void_p
        previous=user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        user.GetDpiForWindow.argtypes=[ctypes.c_void_p];user.GetDpiForWindow.restype=ctypes.c_uint
        return user,previous
    except (OSError,AttributeError):return None,None


class ControlIndicator:
    def __init__(self, on_stop: Callable[[], None], *, remote=False, active=None, on_request=None):
        self.on_stop = on_stop
        self.remote=remote
        self.active=active
        self.on_request=on_request
        self._requesting=False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._visible = False
        self._stop_requested = threading.Event()
        self._stopping = False
        self._error: Exception | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._error = None
        self._ready.clear()
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, name="MuseBridgeControlIndicator", daemon=True)
        self._thread.start()
        if not self._ready.wait(3) or self._error is not None or not self._visible:
            self.stop()
            raise ValueError("Could not display the desktop-control stop indicator")

    def set_remote(self,value):
        # Tk widgets are updated only on the indicator's owning thread.
        self.remote=bool(value)

    def _run(self) -> None:
        dpi_user,previous_dpi=_dpi_context()
        try:
            import tkinter
            root = tkinter.Tk()
            self._visible = True
            root.title("MuseBridge desktop control")
            root.attributes("-topmost", True)
            root.overrideredirect(True)
            root.configure(background="#13110f")
            root.geometry("220x32+24+24")
            canvas = tkinter.Canvas(root,width=220,height=32,background="#13110f",
                                    highlightthickness=1,highlightbackground="#a57437",borderwidth=0)
            canvas.pack(fill="both",expand=True)
            title=canvas.create_text(9,16,anchor="w",text="Desktop access denied",fill="#fff6e8",font=("Segoe UI",9))
            dot=canvas.create_oval(152,12,160,20,fill="#c74a45",outline="")
            # Tk's standard canvas has no rounded rectangle; a pill is built
            # from a center rectangle and two circles.
            track_bg=canvas.create_rectangle(179,6,201,26,fill="#5c554d",outline="")
            track_left=canvas.create_oval(169,6,189,26,fill="#5c554d",outline="")
            track_right=canvas.create_oval(191,6,211,26,fill="#5c554d",outline="")
            knob=canvas.create_oval(172,8,188,24,fill="#fff2dc",outline="")
            def toggle(event):
                from tkinter import messagebox
                self._toggle_from_ui(
                    lambda: messagebox.askyesno("Desktop access", 
                        "Request desktop access for Muse? Existing DeskHand permissions and STOP still apply.",
                        parent=root, default="no"),
                    lambda message: messagebox.showinfo("Desktop access", message, parent=root))
            canvas.bind("<Button-1>",toggle)
            canvas.configure(takefocus=True, cursor="hand2")
            canvas.bind("<space>",toggle)
            canvas.bind("<Return>",toggle)
            root.update_idletasks()
            last_scale=[None]
            def apply_dpi():
                try:dpi=dpi_user.GetDpiForWindow(root.winfo_id()) if dpi_user else 96
                except (OSError,AttributeError):dpi=96
                width,height,scale=indicator_size(dpi or 96)
                if last_scale[0]==scale:return scale
                last_scale[0]=scale
                root.geometry(f"{width}x{height}")
                canvas.configure(width=width,height=height,highlightthickness=max(1,round(scale)))
                def coords(item,*values):canvas.coords(item,*(v*scale for v in values))
                coords(title,9,16);canvas.itemconfigure(title,font=("Segoe UI",-round(12*scale)))
                coords(dot,152,12,160,20);coords(track_bg,179,6,201,26)
                coords(track_left,169,6,189,26);coords(track_right,191,6,211,26)
                return scale
            root.protocol("WM_DELETE_WINDOW", self._stop_from_ui)
            def check_stop():
                scale=apply_dpi()
                active=indicator_active(self.active)
                label="Requesting access…" if self._requesting else (("PC access granted · PIN" if self.remote else "Desktop access granted") if active else "Desktop access denied")
                canvas.itemconfigure(title,text=label)
                canvas.itemconfigure(dot,fill="#4dbd6a" if active else "#d45e52")
                color="#99762d" if active else "#5c554d"
                for item in (track_bg,track_left,track_right):canvas.itemconfigure(item,fill=color)
                canvas.coords(knob,(190 if active else 172)*scale,8*scale,(206 if active else 188)*scale,24*scale)
                if self._stop_requested.is_set():root.destroy()
                else:root.after(50,check_stop)
            check_stop()
            self._ready.set()
            root.mainloop()
        except Exception as exc:
            LOG.error('Desktop STOP indicator failed (%s)', type(exc).__name__)
            self._error = exc
            self._ready.set()
        finally:
            self._visible = False
            if not self._stopping:
                self._stop_from_ui()
            # Tcl objects must be released on the thread that created them.
            # No Tk reference is shared with the asyncio/operator threads.
            if 'root' in locals():
                root = canvas = None
                import gc
                gc.collect()
            if dpi_user and previous_dpi:
                try:dpi_user.SetThreadDpiAwarenessContext(previous_dpi)
                except OSError:pass

    def _toggle_from_ui(self, confirm, notify) -> None:
        # Never infer authority from the switch position. Repaint reads active().
        if self._requesting:
            return
        if indicator_active(self.active):
            self._stop_from_ui()
            return
        self._requesting=True
        try:
            if self.on_request is None:
                raise ValueError("Open DeskHand and use Full PC access beside the AI selector to authorize access.")
            if confirm():
                self.on_request()
                if not indicator_active(self.active):
                    notify("Access is still off. Check the connection and desktop permissions in DeskHand.")
        except Exception as exc:
            notify(str(exc))
        finally:
            self._requesting=False

    def _stop_from_ui(self) -> None:
        self.on_stop()
        self._stop_requested.set()

    def stop(self) -> None:
        self._stopping = True
        self._stop_requested.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        self._thread = None
