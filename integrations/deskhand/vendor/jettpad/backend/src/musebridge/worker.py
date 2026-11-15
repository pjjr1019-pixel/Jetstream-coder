"""Warm, killable file worker. The parent owns authorization, deadlines and replay IDs."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import sys
from pathlib import Path

from .execution import CREATE_SUSPENDED, _WindowsJob, scrub_environment, launch_bounded
from .files import FileOperations
from .permissions import PermissionManager
from .runtime_paths import helper_command

LOG = logging.getLogger("musebridge.worker")

# Last lines of file-worker stderr kept for crash diagnostics.
MAX_KEPT_STDERR_LINES = 20


class FileWorker:
    def __init__(self, home: Path, config_path: Path):
        self.home, self.config_path = home, config_path
        self.process = self.job = None
        self.lock = asyncio.Lock()
        self._stderr_lines: collections.deque[str] = collections.deque(maxlen=MAX_KEPT_STDERR_LINES)
        self._stderr_task: asyncio.Task | None = None

    async def _drain_stderr(self, process) -> None:
        # A worker whose stderr pipe is never read blocks once the pipe
        # fills, so drain it into a bounded buffer instead of discarding it.
        stream = process.stderr
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr_lines.append(text)
        except (asyncio.CancelledError, OSError, ValueError):
            return

    def _log_stderr(self) -> None:
        if self._stderr_lines:
            LOG.warning("File worker stderr: %s", " | ".join(self._stderr_lines))

    async def close(self):
        process, self.process = self.process, None
        if process is not None:
            if self.job is not None:
                self.job.terminate()
            elif process.returncode is None:
                process.kill()
            await process.wait()
        if self.job is not None:
            self.job.close()
            self.job = None
        task, self._stderr_task = self._stderr_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def call(self, operation, args, permissions, timeout=15):
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                await self.close()
                self.process = await launch_bounded(
                    *helper_command("musebridge.worker"),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=8*1024*1024,
                    env=scrub_environment(), creationflags=CREATE_SUSPENDED if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                # B2-E (B-13): capture stderr instead of discarding it, so a
                # worker crash leaves a diagnostic in the bridge log.
                self._stderr_task = asyncio.create_task(self._drain_stderr(self.process))
                try:
                    self.job = _WindowsJob(self.process)
                    if os.name == "nt":
                        self.job.resume(self.process)
                except BaseException:
                    await self.close()
                    raise
            config = {key: value for key, value in permissions.config.items() if key != "token"}
            body = {"operation": operation, "args": args, "config": config,
                    "config_path": str(self.config_path), "home": str(self.home),
                    "profile": permissions.session_profile, "expires": permissions.session_expires_at}
            try:
                async with asyncio.timeout(timeout):
                    self.process.stdin.write(json.dumps(body).encode()+b"\n")
                    await self.process.stdin.drain()
                    line = await self.process.stdout.readline()
                    if not line:
                        self._log_stderr()
                        raise RuntimeError("The file worker stopped; the operation outcome may be uncertain")
                    result = json.loads(line)
                    if "error" in result:
                        raise ValueError(result["error"])
                    return result["result"]
            except TimeoutError:
                self._log_stderr()
                await self.close()
                raise RuntimeError("File operation exceeded its deadline; worker stopped, outcome uncertain") from None
            except (asyncio.CancelledError, RuntimeError, OSError):
                self._log_stderr()
                await self.close()
                raise


def main():
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            operation = request["operation"]
            if operation not in {"list", "read", "write", "delete", "mkdir", "restore", "read_chunk", "upload_begin", "upload_chunk", "upload_commit", "upload_abort"}:
                raise ValueError("Unsupported file operation")
            permissions = PermissionManager(request["config"], Path(request["config_path"]))
            permissions.session_profile = request["profile"]
            permissions.session_expires_at = request["expires"]
            files = FileOperations(permissions, Path(request["home"])/"musebridge-recovery")
            if operation == "read_chunk":
                from .transfers import read_chunk
                value = read_chunk(permissions, *request["args"])
            elif operation.startswith("upload_"):
                from .transfers import Transfers
                value = getattr(Transfers(permissions, request["home"]), operation[7:])(*request["args"])
            else:
                value = getattr(files, operation)(*request["args"])
            result = {"result": value}
        except ValueError as exc:
            result = {"error": str(exc)[:500]}
        except Exception:
            result = {"error": "File operation failed; inspect the local audit log"}
        print(json.dumps(result, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
