"""An optional local runtime owned by the Deskhand assistant process."""
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _make_kill_job(process):
    """On Windows, closing this job terminates the owned runtime process tree."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", wintypes.DWORD),
                    ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "Could not create local runtime job")
    limits = ExtendedLimits()
    limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, int(process._handle)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise OSError(error, "Could not attach the local runtime to Deskhand")
    return lambda: kernel.CloseHandle(job)


class ManagedRuntime:
    def __init__(self, executable=None):
        self.executable = Path(executable) if executable else self._find_executable()
        self.process = None
        self.endpoint = None
        self.close_job = None
        self.lock = threading.Lock()

    @staticmethod
    def _find_executable():
        bundled = Path(__file__).resolve().parent / "runtime" / "ollama.exe"
        if bundled.is_file():
            return bundled
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / "Programs" / "Ollama" / "ollama.exe" if local else None

    def start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return self.endpoint
            if self.close_job is not None:
                self.close_job()
            self.process, self.endpoint, self.close_job = None, None, None
            if self.executable is None or not self.executable.is_file():
                raise RuntimeError("The managed local runtime is not installed with Deskhand and no compatible local runtime was found.")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            endpoint = f"http://127.0.0.1:{port}"
            env = os.environ.copy()
            env["OLLAMA_HOST"] = f"127.0.0.1:{port}"
            try:
                process = subprocess.Popen([str(self.executable), "serve"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as error:
                raise RuntimeError("Could not start the local runtime: " + str(error)) from None
            close_job = None
            client = build_opener(ProxyHandler({}), _NoRedirect())
            deadline = time.monotonic() + 15
            try:
                close_job = _make_kill_job(process)
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("The local runtime exited before it was ready.")
                    try:
                        with client.open(endpoint + "/api/version", timeout=.5) as response:
                            version = json.loads(response.read(4097))
                        if isinstance(version, dict) and isinstance(version.get("version"), str):
                            self.process, self.endpoint, self.close_job = process, endpoint, close_job
                            return endpoint
                    except (OSError, ValueError):
                        time.sleep(.1)
                raise RuntimeError("The local runtime did not become ready.")
            except Exception:
                if close_job is not None:
                    close_job()
                if process.poll() is None:
                    if close_job is None:
                        process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()
                raise

    def close(self):
        with self.lock:
            process, close_job = self.process, self.close_job
            self.process, self.endpoint, self.close_job = None, None, None
            if close_job is not None:
                close_job()
            if process is not None and process.poll() is None:
                if close_job is None:
                    process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()

    def require_model(self, model):
        if not isinstance(model, str) or not model.strip() or len(model) > 128 or model != model.strip():
            raise ValueError("Enter a local model name.")
        endpoint = self.start()
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(endpoint + "/api/tags", timeout=5) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("model catalog too large")
            document = json.loads(raw)
            entries = document.get("models") if isinstance(document, dict) else None
            if not isinstance(entries, list):
                raise ValueError("invalid model catalog")
            names = {entry.get("name") for entry in entries if isinstance(entry, dict) and isinstance(entry.get("name"), str)}
            if model not in names and (":" in model or model + ":latest" not in names):
                raise RuntimeError("The selected model is not installed in the local runtime: " + model)
        except (OSError, ValueError, TypeError) as error:
            raise RuntimeError("Could not check local model availability: " + str(error)) from None
