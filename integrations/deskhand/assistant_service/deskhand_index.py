"""Universal incremental app/file index, v100.008 (Batch 3, smarts #6).

Audit F47 (not implemented): the plan's phase-5 index. Installed apps
(name, exe, CIM launch command), recent files, favourites — searchable
by name, not folder-by-folder desktop.list.

Safety rules:
- Read-only metadata. Opening/launching from the index still needs the
  normal grant + (for launch) phone approval. The index never authorizes
  anything.
- Scope is enforced: file results are returned only when their path is
  inside one of the configured scope roots. App entries come only from
  the host-owned app map — never a whole-disk trawl, never a guessed
  exe path.
- No file contents are stored in the index, ever. Only names, paths,
  and timestamps.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

__version__ = "100.008"

MAX_APPS = 256
MAX_RECENT = 50
MAX_BYTES = 262144


class AppFileIndex:
    def __init__(self, roots=()):
        self.roots = [str(root) for root in roots]
        self.apps = {}
        self.recent = []

    def _in_scope(self, path):
        # B2-D M5: a real path check, not a string prefix. The old
        # text.startswith(root) let root "/tmp/foo" match
        # "/tmp/foobar/secret.txt". Normalize both separators (the
        # index runs on Windows but is tested on Linux) and require
        # the root itself or root + one separator.
        text = str(path).replace("\\", "/")
        for root in self.roots:
            norm = str(root).replace("\\", "/").rstrip("/")
            if text == norm or text.startswith(norm + "/"):
                return True
        return False

    def add_app(self, name, exe, cim_command):
        """Add one host-owned app-map entry. `name` is the launch-by-name
        key; `exe`/`cim_command` come from the host, never from the AI."""
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("invalid app name")
        if not isinstance(exe, str) or not exe.strip() or len(exe) > 500:
            raise ValueError("invalid app exe")
        if not isinstance(cim_command, str) or not cim_command.strip() or len(cim_command) > 500:
            raise ValueError("invalid app launch command")
        if len(self.apps) >= MAX_APPS:
            raise ValueError("app index is full")
        self.apps[name.strip().lower()] = {"name": name.strip(), "exe": exe,
                                           "cim_command": cim_command}

    def launch_by_name(self, name):
        """Look up an app by name. Returns the app entry or None. Unknown
        names error at the caller — never fall back to a guessed exe path."""
        if not isinstance(name, str):
            return None
        return self.apps.get(name.strip().lower())

    def add_recent_file(self, path, mtime=None):
        if not isinstance(path, str) or not path.strip() or len(path) > 500:
            raise ValueError("invalid file path")
        if not self._in_scope(path):
            raise ValueError("file is outside the granted scope")
        entry = {"path": path, "mtime": mtime}
        self.recent = [item for item in self.recent if item["path"] != path]
        self.recent.insert(0, entry)
        self.recent = self.recent[:MAX_RECENT]

    def search(self, query):
        """Search apps by name and recent files by filename. Returns
        {"apps": [...], "files": [...]}. Query is a plain substring,
        case-insensitive, 1..200 chars. No contents are ever returned."""
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("invalid search query")
        needle = query.strip().lower()
        apps = [app for key, app in self.apps.items() if needle in key]
        files = [item for item in self.recent
                 if needle in Path(item["path"]).name.lower()
                 and self._in_scope(item["path"])]
        return {"apps": apps, "files": files}

    def to_json(self):
        return {"version": 1, "roots": self.roots,
                "apps": self.apps, "recent": self.recent}

    def save(self, path):
        raw = json.dumps(self.to_json(), ensure_ascii=True).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("index too large to save")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, path)

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            return cls()
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError("index file too large")
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ValueError("invalid index file")
        index = cls(document.get("roots", []))
        for name, app in (document.get("apps") or {}).items():
            index.apps[name] = app
        index.recent = document.get("recent") or []
        return index
