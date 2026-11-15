"""Small Windows bindings shared by capture, input and acceptance tests."""
from __future__ import annotations
import ctypes
import io
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes as w

from PIL import Image
from .execution import scrub_environment, _WindowsJob, CREATE_SUSPENDED


def api():
    if os.name != "nt":
        raise ValueError("Windows is required")
    u, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    u.GetForegroundWindow.restype = w.HWND
    u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k.CloseHandle.argtypes = [w.HANDLE]
    try:
        u.SetProcessDpiAwarenessContext.argtypes = [w.HANDLE]
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        pass
    # A host may have initialized another DPI mode already. Worker threads still
    # need physical coordinates, so apply a per-thread override as well.
    try:
        u.SetThreadDpiAwarenessContext.argtypes = [w.HANDLE]
        u.SetThreadDpiAwarenessContext.restype = w.HANDLE
        if not u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
            raise ValueError("Per-monitor DPI awareness is unavailable")
    except AttributeError:
        raise ValueError("Desktop access requires per-monitor DPI support") from None
    return u, k


def window_info(hwnd=None):
    u, k = api()
    hwnd = u.GetForegroundWindow() if hwnd is None else hwnd
    if not hwnd:
        raise ValueError("No foreground window")
    pid, rect = w.DWORD(), w.RECT()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ValueError("Window geometry unavailable")
    handle = k.OpenProcess(0x1000, False, pid.value)
    if not handle:
        raise ValueError("protected context")
    try:
        path, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
        if not k.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            raise ValueError("protected context")
    finally:
        k.CloseHandle(handle)
    title = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(hwnd, title, len(title))
    return {"hwnd": hwnd, "pid": pid.value, "path": path.value,
            "rect": [rect.left, rect.top, rect.right, rect.bottom], "title": title.value}


def run_helper(arguments, timeout, cancel=None):
    from .runtime_paths import helper_command
    if cancel is not None and cancel.is_set(): raise ValueError("Desktop operation was revoked")
    process = subprocess.Popen(helper_command("musebridge.windows", *arguments),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=scrub_environment(), creationflags=(0x08000000 | CREATE_SUSPENDED) if os.name == "nt" else 0,
        start_new_session=os.name != "nt")
    job = None
    try:
        job = _WindowsJob(process)
        if cancel is not None and cancel.is_set(): raise ValueError("Desktop operation was revoked")
        if os.name == "nt": job.resume(process)
        deadline = time.monotonic()+timeout
        while True:
            if cancel is not None and cancel.is_set(): raise ValueError("Desktop operation was revoked")
            if time.monotonic() >= deadline: raise ValueError("Desktop helper timed out; operation outcome may be uncertain")
            try:
                output, _ = process.communicate(timeout=min(.05, max(.001, deadline-time.monotonic())))
                return subprocess.CompletedProcess(process.args, process.returncode, output)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if job is not None:
            job.terminate()
        elif process.poll() is None:
            process.kill()
        try: process.communicate(timeout=1)
        except subprocess.TimeoutExpired: pass
        if job is not None: job.close()


def capture_window(info):
    # PrintWindow can block in another application's window procedure. Keep it
    # in a disposable process so cancellation has a real upper bound.
    result = run_helper(["capture", str(info["hwnd"])], 3)
    if result.returncode or not result.stdout:
        raise ValueError("Window capture unavailable for this application")
    image = Image.open(io.BytesIO(result.stdout))
    image.load()
    return image.convert("RGB")


def _capture(hwnd):
    u, _ = api()
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    for name, args, result in (
        ("CreateCompatibleDC", [w.HDC], w.HDC),
        ("CreateCompatibleBitmap", [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP),
        ("SelectObject", [w.HDC, w.HANDLE], w.HANDLE),
        ("DeleteObject", [w.HANDLE], w.BOOL), ("DeleteDC", [w.HDC], w.BOOL),
        ("GetDIBits", [w.HDC, w.HBITMAP, w.UINT, w.UINT, w.LPVOID, w.LPVOID, w.UINT], ctypes.c_int),
    ):
        function = getattr(g, name)
        function.argtypes, function.restype = args, result
    u.GetWindowDC.argtypes, u.GetWindowDC.restype = [w.HWND], w.HDC
    u.ReleaseDC.argtypes = [w.HWND, w.HDC]
    u.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
    rect = w.RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ValueError("Window unavailable")
    width, height = rect.right-rect.left, rect.bottom-rect.top
    if width <= 0 or height <= 0 or width*height > 32_000_000:
        raise ValueError("Unsupported window geometry")
    dc = u.GetWindowDC(hwnd)
    memory, bitmap, previous = None, None, None
    try:
        memory = g.CreateCompatibleDC(dc)
        bitmap = g.CreateCompatibleBitmap(dc, width, height)
        if not dc or not memory or not bitmap:
            raise ValueError("Capture allocation failed")
        previous = g.SelectObject(memory, bitmap)
        if not u.PrintWindow(hwnd, memory, 2):
            raise ValueError("Window does not support capture")
        g.SelectObject(memory, previous)
        previous = None
        header = __import__("struct").pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width*height*4, 0, 0, 0, 0)
        buffer, bitmap_info = ctypes.create_string_buffer(width*height*4), ctypes.create_string_buffer(header+b"\0"*12)
        if g.GetDIBits(memory, bitmap, 0, height, buffer, bitmap_info, 0) != height:
            raise ValueError("Capture unavailable")
        image = Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX")
        if image.getextrema() == ((0, 0), (0, 0), (0, 0)):
            raise ValueError("Application returned an empty frame")
        image.save(sys.stdout.buffer, format="PNG")
    finally:
        if previous: g.SelectObject(memory, previous)
        if bitmap: g.DeleteObject(bitmap)
        if memory: g.DeleteDC(memory)
        if dc: u.ReleaseDC(hwnd, dc)


def unicode_text(text):
    u, _ = api()
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                    ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                    ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
    class UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
    class INPUT(ctypes.Structure):
        _fields_ = [("type", w.DWORD), ("data", UNION)]
    u.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    encoded = text.encode("utf-16-le")
    units = [int.from_bytes(encoded[i:i+2], "little") for i in range(0, len(encoded), 2)]
    for unit in units:
        down, up = INPUT(1, UNION(ki=KEYBDINPUT(0, unit, 4, 0, 0))), INPUT(1, UNION(ki=KEYBDINPUT(0, unit, 6, 0, 0)))
        try:
            if u.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT)) != 1:
                raise ValueError("Windows blocked text input")
        finally:
            if u.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT)) != 1:
                raise ValueError("Windows blocked key release")


def structured(info, operation, payload, cancel=None):
    request = {"hwnd": info["hwnd"], "operation": operation, "payload": payload}
    result = run_helper(["uia", json.dumps(request, ensure_ascii=False)], 5, cancel)
    if result.returncode:
        raise ValueError("Application accessibility unavailable")
    response = json.loads(result.stdout)
    if "error" in response: raise ValueError(response["error"])
    return response


def _uia(request):
    from pywinauto import Desktop
    root = Desktop(backend="uia").window(handle=request["hwnd"]).wrapper_object()
    operation, payload = request["operation"], request["payload"]
    if operation == "inspect":
        rows = []
        for index, node in enumerate(root.descendants()):
            if index >= 500: break
            if node.is_visible() and not node.element_info.element.CurrentIsPassword:
                rows.append({"name": node.window_text()[:200], "automation_id": node.element_info.automation_id,
                             "control_type": node.element_info.control_type, "enabled": node.is_enabled()})
        return {"controls": rows, "truncated": index >= 500 if 'index' in locals() else False,
                "trust": "untrusted_application_content"}
    selector = payload.get("selector")
    if not isinstance(selector, dict) or not selector or set(selector)-{"name", "automation_id", "control_type"}:
        raise ValueError("Use an exact name, automation_id or control_type selector")
    if any(not isinstance(value, str) or not 1 <= len(value) <= 200 for value in selector.values()):
        raise ValueError("Selector values must be bounded, nonempty strings")
    matches = [node for node in root.descendants() if all(
        (node.window_text() if key == "name" else getattr(node.element_info, key)) == value
        for key, value in selector.items())]
    if len(matches) != 1:
        raise ValueError("Selector must identify exactly one control")
    node = matches[0]
    if node.element_info.element.CurrentIsPassword or not node.is_visible() or not node.is_enabled():
        raise ValueError("Control is protected or unavailable")
    if window_info()["hwnd"] != request["hwnd"]:
        raise ValueError("Foreground window changed")
    if operation == "set_text":
        node.set_edit_text(payload["text"])
        if node.get_value() != payload["text"]:
            raise ValueError("Text change could not be verified")
        return {"ok": True, "verified": True, "verification": "control_value"}
    if operation == "invoke":
        expected = payload.get("expected_name")
        if not isinstance(expected, str) or not expected or len(expected) > 200:
            raise ValueError("expected_name is required to verify invoke")
        node.invoke()
        import time
        for _ in range(15):
            if any(n.window_text() == expected for n in root.descendants()):
                return {"ok": True, "verified": True, "verification": "expected_control_present"}
            time.sleep(.1)
        raise ValueError("Action was invoked but its expected result was not observed; do not retry automatically")
    raise ValueError("Unsupported structured action")


if __name__ == "__main__":
    if sys.argv[1] == "capture":
        _capture(int(sys.argv[2]))
    else:
        try: print(json.dumps(_uia(json.loads(sys.argv[2]))))
        except Exception as exc: print(json.dumps({"error": str(exc)[:300]}))
