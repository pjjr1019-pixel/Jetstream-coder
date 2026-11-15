"""Bounded resumable binary uploads; only an integrity-checked commit changes a target."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from .files import FileOperations
from .handles import authorize_open_file
from .redaction import contains_sensitive

MAX_TRANSFER = 16*1024*1024
CHUNK = 64*1024
UPLOAD_IDLE_SECONDS = 600


class Transfers:
    def __init__(self, permissions, home):
        self.permissions = permissions
        self.root = Path(home)/"musebridge-uploads"
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = FileOperations(permissions, Path(home)/"musebridge-recovery")

    def _paths(self, identifier):
        if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
            raise ValueError("Invalid upload id")
        return self.root/(identifier+".json"), self.root/(identifier+".bin")

    def prune(self):
        # Expiry belongs to the manifest (last accepted activity), not the data
        # file: an identical repeated chunk refreshes idle time without rewriting it.
        # B2-E (B-6): a file vanishing between glob and stat must not raise
        # into the safety monitor; skip it.
        for entry in self.root.glob('*.json'):
            try:
                idle = time.time() - entry.stat().st_mtime
            except FileNotFoundError:
                continue
            if idle >= UPLOAD_IDLE_SECONDS:
                try:
                    self.abort(entry.stem)
                except FileNotFoundError:
                    continue
        for entry in self.root.glob('*.bin'):
            try:
                idle = time.time() - entry.stat().st_mtime
            except FileNotFoundError:
                continue
            if not entry.with_suffix('.json').exists() and idle >= UPLOAD_IDLE_SECONDS:
                entry.unlink(missing_ok=True)

    def _active(self,identifier):
        manifest,data=self._paths(identifier)
        try:idle=time.time()-manifest.stat().st_mtime
        except FileNotFoundError:raise ValueError('Upload is missing or expired') from None
        if idle>=UPLOAD_IDLE_SECONDS:
            self.abort(identifier)
            raise ValueError('Upload expired after 600 seconds of inactivity')
        return manifest,data,json.loads(manifest.read_text(encoding='utf-8'))

    def begin(self, path, size, sha256):
        self.prune()
        if type(size) is not int or not 0 <= size <= MAX_TRANSFER:
            raise ValueError("Upload size must be between 0 and 16 MiB")
        if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("A lowercase SHA256 is required")
        target = self.permissions.authorize(path, "write")
        pending = [json.loads(p.read_text()) for p in self.root.glob("*.json")]
        if len(pending) >= 8 or sum(row["size"] for row in pending)+size > 32*1024*1024:
            raise ValueError("Pending upload budget exhausted")
        identifier = str(uuid.uuid4())
        manifest, data = self._paths(identifier)
        data.touch(exist_ok=False)
        manifest.write_text(json.dumps({"path": str(target), "size": size, "sha256": sha256}), encoding="utf-8")
        return {"upload_id": identifier, "chunk_bytes": CHUNK, "offset": 0, "expires_in_secs": 600}

    def chunk(self, identifier, offset, encoded):
        manifest, data, record = self._active(identifier)
        self.permissions.authorize(record["path"], "write")
        if type(offset) is not int or offset < 0 or not isinstance(encoded, str) or len(encoded) > 4*((CHUNK+2)//3):
            raise ValueError("Invalid upload chunk")
        try: chunk = base64.b64decode(encoded, validate=True)
        except ValueError: raise ValueError("Invalid chunk encoding") from None
        if len(chunk) > CHUNK or offset+len(chunk) > record["size"]:
            raise ValueError("Chunk exceeds upload bounds")
        with data.open("r+b") as stream:
            end = stream.seek(0, 2)
            if offset == end:
                stream.write(chunk)
            elif offset < end:
                stream.seek(offset)
                if stream.read(len(chunk)) != chunk:
                    raise ValueError("Conflicting repeated chunk")
            else:
                raise ValueError("Upload offset gap")
        manifest.touch()
        return {"upload_id": identifier, "offset": data.stat().st_size}

    def commit(self, identifier):
        manifest, data, record = self._active(identifier)
        content = data.read_bytes()
        if len(content) != record["size"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise ValueError("Upload size or SHA256 mismatch")
        if contains_sensitive(content.decode("utf-8", errors="replace")):
            raise ValueError("Possible secret in upload; remove it before writing")
        result = self.files.write_bytes(record["path"], content)
        self.abort(identifier)
        return {**result, "committed_path": record["path"], "size": record["size"], "sha256": record["sha256"]}

    def abort(self, identifier):
        manifest, data = self._paths(identifier)
        manifest.unlink(missing_ok=True)
        data.unlink(missing_ok=True)
        return {"ok": True}


def read_chunk(permissions, path, offset=0, count=CHUNK, version=None):
    if type(offset) is not int or offset < 0 or type(count) is not int or not 1 <= count <= CHUNK:
        raise ValueError("Invalid download bounds")
    target = permissions.authorize(path, "read")
    with target.open("rb") as stream:
        authorize_open_file(stream, permissions)
        info = os.fstat(stream.fileno())
        if info.st_size > MAX_TRANSFER: raise ValueError("Binary download exceeds 16 MiB")
        actual = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}"
        if version is not None and version != actual: raise ValueError("File changed during download")
        # Scan the complete bounded file so secrets split across chunks are withheld.
        content = stream.read(MAX_TRANSFER+1)
        if os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns: raise ValueError("File changed during read")
    if len(content) > MAX_TRANSFER or contains_sensitive(content.decode("utf-8", errors="replace")):
        raise ValueError("File too large or possible secret content; withheld")
    data = content[offset:offset+count]
    return {"data_b64": base64.b64encode(data).decode(), "offset": offset,
            "next_offset": offset+len(data), "eof": offset+len(data) >= len(content),
            "version": actual, "size": len(content), "trust": "untrusted_file_content"}
