"""Bounded private recovery copies for file edits/deletions."""
from __future__ import annotations
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from .handles import authorize_open_file


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FileRecovery:
    def __init__(self, root: Path, max_bytes=500*1024*1024, retention_days=7):
        self.root = root
        self.max_bytes = max_bytes
        self.retention_secs = retention_days*86400
        root.mkdir(parents=True, exist_ok=True)

    def prune(self):
        manifests = []
        total = 0
        for manifest in self.root.glob("*.json"):
            try:
                record = json.loads(manifest.read_text())
                data = self.root / (manifest.stem + ".bin")
                size = data.stat().st_size
                manifests.append((record["created"], manifest, data, size))
                total += size
            except (OSError, ValueError, KeyError):
                continue
        for created, manifest, data, size in sorted(manifests):
            if created < time.time()-self.retention_secs or total > self.max_bytes:
                data.unlink(missing_ok=True)
                manifest.unlink(missing_ok=True)
                total -= size
        for data in self.root.glob("*.bin"):
            if not data.with_suffix(".json").exists() and data.stat().st_mtime < time.time()-3600:
                data.unlink(missing_ok=True)

    def preserve(self, path: Path, permissions=None) -> str | None:
        if not path.is_file():
            return None
        if path.stat().st_size > self.max_bytes:
            raise ValueError("File exceeds the recovery budget; change was refused")
        identifier = str(uuid.uuid4())
        data = self.root / (identifier+".bin")
        manifest_path = self.root/(identifier+".json")
        try:
            with path.open("rb") as source, data.open("xb") as destination:
                if permissions is not None: authorize_open_file(source, permissions, "write")
                initial = os.fstat(source.fileno())
                copied = 0
                while chunk := source.read(1024*1024):
                    copied += len(chunk)
                    if copied > self.max_bytes: raise ValueError("File grew beyond recovery budget")
                    destination.write(chunk)
                final = os.fstat(source.fileno())
                if (initial.st_mtime_ns, initial.st_size) != (final.st_mtime_ns, final.st_size):
                    raise ValueError("File changed during recovery copy; change refused")
            manifest = {"path": str(path), "created": time.time(), "sha256": digest_file(data)}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        except BaseException:
            data.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            raise
        self.prune()
        return identifier

    def lookup(self, identifier: str) -> tuple[dict, Path]:
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError("Invalid recovery identifier")
        manifest = json.loads((self.root/(identifier+".json")).read_text(encoding="utf-8"))
        data = self.root/(identifier+".bin")
        if digest_file(data) != manifest["sha256"]:
            raise ValueError("Recovery copy failed its integrity check")
        return manifest, data
