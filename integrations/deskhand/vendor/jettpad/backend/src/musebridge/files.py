"""Scope-checked filesystem operations."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .permissions import PermissionManager
from .redaction import contains_sensitive
from .handles import authorize_open_file
from .recovery import FileRecovery

MAX_READ_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_LIST_ENTRIES = 2000


class FileOperations:
    def __init__(self, permissions: PermissionManager, recovery_root: Path | None = None):
        self.permissions = permissions
        self.recovery = FileRecovery(recovery_root) if recovery_root is not None else None

    def list(self, value: str, limit: int = MAX_LIST_ENTRIES, offset: int = 0) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_ENTRIES or type(offset) is not int or not 0 <= offset <= 1000000:
            raise ValueError("Invalid directory page")
        path = self.permissions.authorize(value, "read")
        if not path.is_dir():
            raise ValueError("not a directory")
        entries = []
        scanned = 0
        truncated = False
        with os.scandir(path) as iterator:
            for index, item in enumerate(iterator):
                if index < offset:
                    continue
                if scanned >= limit:
                    truncated = True
                    break
                scanned += 1
                try:
                    self.permissions.authorize(str(Path(item.path)), "read")
                    # B2-E (B-15): stat inside the guarded block -- a file
                    # vanishing between scandir and stat is skipped, not
                    # raised out of fs.list.
                    info = item.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except ValueError as exc:
                    if str(exc) == "blocked path":
                        continue
                    raise
                entries.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir(follow_symlinks=False) else "file",
                    "size": 0 if item.is_dir(follow_symlinks=False) else info.st_size,
                })
        entries.sort(key=lambda row: (row["type"] != "directory", row["name"].casefold()))
        return {"entries": entries, "truncated": truncated, "next_offset": offset+scanned if truncated else None}

    def read(self, value: str) -> dict[str, str]:
        path = self.permissions.authorize(value, "read")
        if not path.is_file():
            raise ValueError("not a file")
        if path.stat().st_size > MAX_READ_BYTES:
            raise ValueError("file too large")
        with path.open("rb") as stream:
            authorize_open_file(stream, self.permissions)
            data = stream.read(MAX_READ_BYTES + 1)
        if len(data) > MAX_READ_BYTES:
            raise ValueError("file too large")
        if b"\x00" in data:
            raise ValueError("not a text file")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("not a text file") from None
        if contains_sensitive(content):
            raise ValueError("possible secret content; withheld")
        return {"content": content}

    def write(self, value: str, content: str) -> dict[str, Any]:
        if not isinstance(content, str) or len(content) > MAX_WRITE_BYTES:
            raise ValueError("content exceeds 1 MiB")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise ValueError("content exceeds 1 MiB")
        if contains_sensitive(content):
            raise ValueError("possible secret content; remove it before writing")
        return self.write_bytes(value, encoded)

    def write_bytes(self, value: str, encoded: bytes) -> dict[str, Any]:
        path = self.permissions.authorize(value, "write")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-authorize after directory creation to catch symlink changes.
        path = self.permissions.authorize(str(path), "write")
        recovery_id = self.recovery.preserve(path, self.permissions) if self.recovery is not None else None
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.musebridge-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                authorize_open_file(stream, self.permissions, "write")
                stream.write(encoded)
            path = self.permissions.authorize(str(path), "write")
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return {"ok": True, **({"recovery_id": recovery_id} if recovery_id else {})}

    def delete(self, value: str) -> dict[str, Any]:
        path = self.permissions.authorize(value, "write")
        if path.is_dir():
            raise ValueError("directory deletion is not supported")
        recovery_id = self.recovery.preserve(path, self.permissions) if self.recovery is not None else None
        path = self.permissions.authorize(str(path), "write")
        path.unlink()
        return {"ok": True, **({"recovery_id": recovery_id} if recovery_id else {})}

    def restore(self, recovery_id: str) -> dict[str, Any]:
        if self.recovery is None:
            raise ValueError("Recovery storage is unavailable")
        manifest, data = self.recovery.lookup(recovery_id)
        target = self.permissions.authorize(manifest["path"], "write")
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = self.recovery.preserve(target, self.permissions)
        descriptor, name = tempfile.mkstemp(prefix=".musebridge-restore-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output, data.open("rb") as source:
                authorize_open_file(output, self.permissions, "write")
                for chunk in iter(lambda: source.read(1024*1024), b""):
                    output.write(chunk)
            target = self.permissions.authorize(str(target), "write")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"ok": True, "restored": str(target), "previous_recovery_id": previous}

    def mkdir(self, value: str) -> dict[str, bool]:
        path = self.permissions.authorize(value, "write")
        path.mkdir(parents=True, exist_ok=True)
        self.permissions.authorize(str(path), "write")
        return {"ok": True}
