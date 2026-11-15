"""Single-instance ownership and authenticated local lifecycle requests."""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from .runtime_paths import root as isolated_root


class InstanceLock:
    def __init__(self, home: Path):
        self.home = home
        self.handle = None
        self.stream = None

    def __enter__(self):
        self.home.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            name = "Local\\MuseBridge-" + hashlib.sha256(str(self.home.resolve()).casefold().encode()).hexdigest()[:24]
            ctypes.set_last_error(0)
            handle = kernel.CreateMutexW(None, True, name)
            if not handle:
                raise RuntimeError("Could not acquire MuseBridge instance ownership")
            if ctypes.get_last_error() == 183:
                kernel.CloseHandle(handle)
                raise RuntimeError("MuseBridge is already running. Use status, console, stop or restart.")
            self.kernel, self.handle = kernel, handle
        else:
            import fcntl
            self.stream = (self.home/"instance.lock").open("a")
            try:
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.stream.close()
                raise RuntimeError("MuseBridge is already running") from None
        return self

    def __exit__(self, *args):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.stream:
            self.stream.close()


def local_request(home: Path, action: str | None = None, payload: dict | None = None, timeout=8):
    token = (home/"operator.secret").read_text(encoding="ascii").strip()
    runtime = home/"runtime.json"
    # The historical fallback port belongs to JettPad. Never contact it from
    # Deskhand while the separate backend is starting or has already exited.
    if isolated_root() is not None and not runtime.is_file():
        raise RuntimeError("Deskhand MuseBridge is not running")
    state = json.loads(runtime.read_text()) if runtime.exists() else {}
    port = state.get("operator_port") if isolated_root() is not None else state.get("operator_port", 8766)
    if type(port) is not int or not 1 <= port <= 65535:
        raise RuntimeError("MuseBridge operator endpoint is invalid")
    origin = f"http://127.0.0.1:{port}"
    body = json.dumps({"action": action, **(payload or {})}).encode() if action else None
    request = Request(origin+("/api/action" if action else "/api/state?compact=1"), data=body,
                      headers={"Authorization": "Bearer "+token, "Origin": origin,
                               "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        try: message = json.load(exc).get("error", exc.reason)
        except (ValueError, AttributeError): message = exc.reason
        raise RuntimeError(f"Local action refused: {str(message)[:500]}") from exc
    except URLError as exc:
        raise RuntimeError("MuseBridge is not reachable; start it with the Windows shell launcher") from exc
