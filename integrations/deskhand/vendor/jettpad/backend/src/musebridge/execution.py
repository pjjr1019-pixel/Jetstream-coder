"""Bounded, non-elevated process execution."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import time
from collections import deque
from typing import Any

from .permissions import PermissionManager
from .redaction import contains_sensitive, redact_text

MAX_OUTPUT_BYTES = 256 * 1024
CREATE_SUSPENDED = 0x00000004
SECRET_ENV_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ValueError("Could not create a bounded command process job")
        limits = _JobExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x100 | 0x8
        limits.BasicLimitInformation.ActiveProcessLimit = 64
        limits.ProcessMemoryLimit = 2 * 1024 * 1024 * 1024
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(handle)
            raise ValueError("Could not set command process limits")
        transport = getattr(process, "_transport", None)
        popen = transport.get_extra_info("subprocess") if transport is not None else process
        if popen is None or not kernel.AssignProcessToJobObject(handle, popen._handle):
            kernel.CloseHandle(handle)
            raise ValueError("Could not isolate command child processes")
        self.handle = handle
        self.kernel = kernel

    @staticmethod
    def resume(process) -> None:
        """Resume the initial thread only after its suspended process joins the job."""
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ("Thread32First", "Thread32Next"):
            function = getattr(kernel, name)
            function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
            function.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise ValueError("Could not find the suspended command thread")
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = kernel.OpenThread(0x2, False, entry.th32ThreadID)
                    if not thread:
                        raise ValueError("Could not open the suspended command thread")
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ValueError("Could not resume the bounded command")
                        return
                    finally:
                        kernel.CloseHandle(thread)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise ValueError("The suspended command thread was not found")
        finally:
            kernel.CloseHandle(snapshot)

    def terminate(self) -> None:
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class SlidingLimit:
    def __init__(self):
        self.events: dict[str, deque[float]] = {}

    def accept(self, key: str, count: int, window: float) -> int:
        now = time.monotonic()
        events = self.events.setdefault(key, deque())
        while events and events[0] <= now - window:
            events.popleft()
        if len(events) >= count:
            return max(1, int(events[0] + window - now + 0.999))
        events.append(now)
        return 0


class RateLimitExceeded(ValueError):
    code = "rate_limited"
    def __init__(self, retry_after_secs: int, message="Rate limit exceeded"):
        self.retry_after_secs = retry_after_secs
        super().__init__(message)


class ExecutionRateLimitExceeded(RateLimitExceeded):
    def __init__(self, retry_after_secs):
        super().__init__(retry_after_secs, "Execution rate limit exceeded")


def scrub_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if environment is None else environment)
    return {
        key: value for key, value in source.items()
        if not any(marker in key.upper() for marker in SECRET_ENV_MARKERS)
    }


async def launch_bounded(*args, **kwargs):
    """Cancellation during CreateProcess must not lose ownership of its child."""
    pending = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        process = await pending
        if process.returncode is None:
            if os.name != "nt" and kwargs.get("start_new_session"):
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            else:
                process.kill()
        await process.communicate()
        raise


class CommandRunner:
    def __init__(self, permissions: PermissionManager, config: dict[str, Any]):
        self.permissions = permissions
        self.config = config
        self.limiter = SlidingLimit()
        self._slots = asyncio.Semaphore(2)

    async def run(
        self, command: str, cwd: str | None = None, timeout_secs: int = 60
    ) -> dict[str, Any]:
        if self.config.get("features", {}).get("exec") is not True:
            raise ValueError("exec.run is disabled")
        if self.config.get("execution_mode", "host") != "host":
            raise ValueError("OS-isolated execution is not installed; command refused. Configure a dedicated restricted account or a reviewed sandbox executor before selecting this mode.")
        if not isinstance(command, str) or not command.strip() or len(command) > 4000:
            raise ValueError("Invalid command")
        if contains_sensitive(command):
            raise ValueError("Possible secret in command; remove it before execution")
        if type(timeout_secs) is not int:
            raise ValueError("timeout_secs must be an integer")
        timeout_secs = min(600, max(1, timeout_secs))
        if cwd is None:
            roots = self.permissions.exec_scope_roots()
            if not roots:
                raise ValueError("No exec scope is configured")
            target = roots[0]
        else:
            if not isinstance(cwd, str):
                raise ValueError("cwd must be an absolute directory")
            if len(cwd) > 32767:
                raise ValueError("cwd is too long")
            target = self.permissions.authorize_cwd(cwd)
        target = self.permissions.authorize_cwd(str(target))
        self.permissions.check_session("exec")
        limit = self.config["rate_limits"]["exec_count"]
        window = self.config["rate_limits"]["exec_window_secs"]
        retry = self.limiter.accept("exec", limit, window)
        if retry:
            raise ExecutionRateLimitExceeded(retry)
        async with self._slots:
            # Permissions may change while another command owns a slot.
            if self.config.get("features", {}).get("exec") is not True:
                raise ValueError("exec.run is disabled")
            target = self.permissions.authorize_cwd(str(target))
            from .pad_delivery import deliver_notepad_reply
            delivered = await asyncio.to_thread(deliver_notepad_reply, command, self.permissions)
            if delivered is not None:
                return {**delivered, '_cwd': str(target)}
            return await self._run_process(command, target, timeout_secs)

    async def _run_process(self, command: str, target, timeout_secs: int) -> dict[str, Any]:
        try:
            if os.name == "nt":
                shell = os.environ.get("COMSPEC", "cmd.exe")
                process = await launch_bounded(
                    shell, "/d", "/c", command, cwd=str(target),
                    env=scrub_environment(), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    creationflags=CREATE_SUSPENDED,
                )
            else:
                process = await launch_bounded(
                    "/bin/sh", "-c", command, cwd=str(target), env=scrub_environment(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
        except OSError as exc:
            raise ValueError(f"Could not start command: {exc.strerror or type(exc).__name__}") from None
        job = None
        try:
            job = _WindowsJob(process)
            if os.name == "nt":
                job.resume(process)
        except Exception:
            process.kill()
            await process.wait()
            if job is not None:
                job.close()
            raise

        async def read_bounded(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
            output = bytearray()
            truncated = False
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return bytes(output), truncated
                remaining = MAX_OUTPUT_BYTES - len(output)
                if remaining:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True

        try:
            stdout_task = asyncio.create_task(read_bounded(process.stdout))
            stderr_task = asyncio.create_task(read_bounded(process.stderr))
            wait_task = asyncio.create_task(process.wait())
            completed = asyncio.gather(stdout_task, stderr_task, wait_task)
            done, _ = await asyncio.wait({completed}, timeout=timeout_secs)
            timed_out = not done
            if timed_out:
                self._terminate(process, job)
            stdout, stderr, exit_code = await completed
            result = {
                "ok": not timed_out and exit_code == 0,
                "stdout": redact_text(stdout[0].decode("utf-8", errors="replace")),
                "stderr": redact_text(stderr[0].decode("utf-8", errors="replace")),
                "exit_code": -1 if timed_out else exit_code,
                "_cwd": str(target),
            }
            if stdout[1] or stderr[1]:
                result["truncated"] = True
            if timed_out:
                result["message"] = f"timed out after {timeout_secs}s"
            return result
        except (asyncio.CancelledError, Exception) as exc:
            self._terminate(process, job)
            await process.wait()
            for task in (stdout_task, stderr_task, wait_task):
                task.cancel()
            await asyncio.gather(
                stdout_task, stderr_task, wait_task, completed, return_exceptions=True,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ValueError("Command execution failed") from None
        finally:
            job.close()

    @staticmethod
    def _terminate(process, job) -> None:
        if os.name == "nt":
            job.terminate()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
