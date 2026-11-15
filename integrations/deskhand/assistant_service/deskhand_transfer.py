"""Byte-exact file transfer for Deskhand (100.047, vault B35).

The laptop agent's ``files-write`` normalizes ALL line endings to LF
(proven 2026-11-09/10, Gate 4), so every CRLF file had to go via manual
base64 (encode on VM, files-write the ``.b64`` text, decode on the PC
with a ``.py`` script, re-hash) or a ZIP. This module is the transfer
path that preserves bytes exactly: the service runs ON the PC, so the
transfer op writes RAW bytes to disk itself, bypassing the agent's
``files-write`` entirely — no normalization, no re-encoding — then
re-hashes the written bytes before success is claimed.

Ownership: one file per session (``transfer_init`` -> N x
``transfer_chunk`` -> ``transfer_finalize``), plus a single-shot
``transfer_put`` for payloads up to one chunk. Chunk parts are stored
as individual files under the service data dir; the target is written
only by an atomic rename of a fully verified temp file.

Safety (BUILD-SPEC-100047 SS1.3/SS1.5/SS1.6/SS1.7):
  * Destination paths must resolve inside an allowlist of roots
    (Documents, Desktop, the service data dir by default; see
    ``_resolve_roots``). Resolution is post-``resolve()``, so ``..``
    traversal and a symlink pointing outside a root are both caught
    by the containment check — the check is on the resolved path,
    never on the raw string (the agent ``check_path`` precedent).
  * No shell anywhere — this file has no subprocess at all. A payload
    is data, never code: it is base64-decoded to bytes exactly once
    and never decoded as text, never executed.
  * Size caps (SS1.5): per-chunk base64 400 KiB / decoded 300 KiB
    (the proven laptop-agent part size), single-shot = one chunk,
    per-file 256 MiB, at most 1024 chunks, at most 8 concurrent
    sessions, sessions expire after 1 h.
  * STOP gate (SS1.6): the write ops (put / init / chunk / finalize)
    refuse while chat/agent work is stopped, raising RuntimeError.
    Observing, cancelling, or verifying always works.
  * Atomicity + self-verification (SS1.7): every write goes to a
    temp file in the destination directory and lands via
    ``os.replace``; the temp is hashed before the rename and the
    final file is re-hashed after it. On a hash mismatch the op
    returns ``{"ok": false, ...}`` with both hashes, the target is
    never touched, and the bad bytes are quarantined under the data
    dir. A half-written file never sits at the final path.
  * No vm-event ever carries payload bytes — events are metadata
    only (path, bytes, sha256, chunks), and only on completed writes
    (E41 scrub rule). Read-only ops emit nothing.

Stdlib only: base64, binascii, hashlib, logging, os, re, shutil,
threading, time, uuid, pathlib, datetime.
"""

import base64
import binascii
import hashlib
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["Transfer", "get_transfer"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Caps (BUILD-SPEC SS1.5).
# ---------------------------------------------------------------------------

MAX_CHUNK_B64_CHARS = 400 * 1024            # 409,600 chars of base64 text
MAX_CHUNK_DECODED_BYTES = 300 * 1024        # 307,200 bytes once decoded
MAX_SINGLESHOT_DECODED_BYTES = MAX_CHUNK_DECODED_BYTES
MAX_FILE_BYTES = 256 * 1024 * 1024          # 256 MiB per transfer
MAX_TOTAL_CHUNKS = 1024
MAX_READ_BYTES = 1024 * 1024                # 1 MiB, same as agent files-read
MAX_SESSIONS = 8
SESSION_MAX_AGE_S = 3600                    # 1 hour
MAX_PATH_CHARS = 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WS_RE = re.compile(r"\s+")
_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})

_READ_BLOCK = 64 * 1024


def _utc_now_iso():
    """UTC timestamp in the service's shape (isoformat, seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path):
    """sha256 hex of the exact bytes of the file at ``path``."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(_READ_BLOCK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Roots (SS1.3).
# ---------------------------------------------------------------------------

def _resolve_roots(allowed_roots, data_dir):
    """The allowlist of destination roots, per the SS1.3 order.

    Explicit ``allowed_roots`` arg wins; else env
    ``DESKHAND_TRANSFER_ROOTS`` (os.pathsep-separated absolute dirs);
    else the defaults: ~/Documents, ~/Desktop, and the service data
    dir. A root must be an absolute path; a non-existent root is
    skipped (not fatal — e.g. a test box without Documents). If no
    root resolves, construction raises ValueError.
    """
    candidates = None
    if allowed_roots is not None:
        if not isinstance(allowed_roots, (list, tuple)):
            raise ValueError("allowed_roots must be a list of absolute paths")
        candidates = list(allowed_roots)
    else:
        env_roots = os.environ.get("DESKHAND_TRANSFER_ROOTS")
        if env_roots:
            candidates = env_roots.split(os.pathsep)
        else:
            candidates = [str(Path.home() / "Documents"),
                          str(Path.home() / "Desktop")]
            if data_dir is not None:
                candidates.append(str(data_dir))
    roots = []
    for raw in candidates:
        if isinstance(raw, os.PathLike):
            raw = os.fspath(raw)
        if not isinstance(raw, str) or not raw:
            raise ValueError("allowed_roots entries must be non-empty strings")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("allowed root must be an absolute path: %r"
                             % (raw,))
        if path.is_dir():
            roots.append(path.resolve())
    if not roots:
        raise ValueError("no usable transfer roots")
    return roots


def _path_inside(path, roots):
    """True when resolved ``path`` is inside (or equal to) any root."""
    for root in roots:
        try:
            if os.name == "nt":
                candidate = os.path.normcase(str(path))
                base = os.path.normcase(str(root))
                if os.path.commonpath([candidate, base]) == base:
                    return True
            elif os.path.commonpath([str(path), str(root)]) == str(root):
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Validation (SS1.1/SS1.3/SS1.5).
# ---------------------------------------------------------------------------

def _validate_path(path, roots):
    """Resolve ``path`` and check it against the allowlist (SS1.3).

    Returns the resolved absolute Path. Raises ValueError for a
    non-string / empty / over-long path, a resolved path outside every
    root, or a Windows reserved device name as the final component.
    """
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if len(path) > MAX_PATH_CHARS:
        raise ValueError("path must be at most %d chars" % MAX_PATH_CHARS)
    if not Path(path).is_absolute():
        raise ValueError("path must be absolute: %r" % (path,))
    resolved = Path(path).expanduser().resolve()
    if os.name == "nt" and resolved.name.lower() in _RESERVED_NAMES:
        raise ValueError("path names a reserved device: %r" % (path,))
    if not _path_inside(resolved, roots):
        raise ValueError("path is outside the allowed roots: %r" % (path,))
    return resolved


def _validate_sha256(value, field="sha256"):
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise ValueError("%s must be 64 lowercase hex chars" % field)
    return value


def _decode_b64(data):
    """Decode a base64 payload to exact bytes (SS1.1).

    Whitespace (including newlines) is stripped first; the remainder
    must decode with validate=True. Raises ValueError, never a bare
    binascii.Error.
    """
    if not isinstance(data, str):
        raise ValueError("data must be a base64 string")
    compact = _WS_RE.sub("", data)
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("data is not valid base64")


def _require(command, field):
    """Pull a required arg out of a dispatch dict (SS1.1).

    A missing arg raises ValueError naming the field — never a bare
    KeyError leaking out of dispatch.
    """
    value = command.get(field)
    if value is None:
        raise ValueError("missing required arg: %s" % field)
    return value


def _validate_int(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an int in %d..%d"
                         % (field, minimum, maximum))
    if not minimum <= value <= maximum:
        raise ValueError("%s must be in %d..%d"
                         % (field, minimum, maximum))
    return value


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

class Transfer:
    """Owns byte-exact file transfers into the allowlisted roots.

    Constructed as ``Transfer(emit_event=None, data_dir=None,
    allowed_roots=None, is_stopped=None)``; the service wires
    ``emit_event=self._append_vm_event``, ``data_dir=self.data``, and
    ``is_stopped`` to the dispatcher state via ``get_transfer``
    (below). Direct use (tests, drivers) passes the same pieces
    explicitly. ``is_stopped=None`` means the gate is open.
    """

    def __init__(self, emit_event=None, data_dir=None, allowed_roots=None,
                 is_stopped=None):
        self._emit_event = emit_event
        if data_dir is None:
            data_dir = Path(__file__).resolve().parent
        self._data_dir = Path(data_dir)
        self._roots = _resolve_roots(allowed_roots, self._data_dir)
        self._is_stopped = is_stopped
        self._transfers_dir = self._data_dir / "transfers"
        self._quarantine_dir = self._transfers_dir / "quarantine"
        self._registry = {}          # transfer_id -> session record (dict)
        self._registry_lock = threading.Lock()
        self._prune_sweep()

    # ------------------------------------------------------------------
    # Public API (SS1.2)
    # ------------------------------------------------------------------

    def put(self, path, data_b64, sha256, overwrite=False):
        """Single-shot write of the exact bytes in ``data_b64`` (SS1.1).

        Returns the success result, or ``{"ok": false, ...}`` with both
        hashes on a hash mismatch (target untouched, bad bytes
        quarantined). Raises ValueError for bad args/paths/caps and
        RuntimeError when stopped.
        """
        self._check_not_stopped()
        target = _validate_path(path, self._roots)
        expected = _validate_sha256(sha256)
        raw = _decode_b64(data_b64)
        if len(raw) > MAX_SINGLESHOT_DECODED_BYTES:
            raise ValueError("data exceeds the single-shot cap of %d bytes"
                             % MAX_SINGLESHOT_DECODED_BYTES)
        self._check_write_target(target, overwrite)
        transfer_id = "put-" + uuid.uuid4().hex[:12]
        temp = self._temp_path(target, transfer_id)
        try:
            with open(temp, "wb") as handle:
                handle.write(raw)
            actual = _sha256_file(temp)
            if actual != expected:
                quarantined = self._quarantine(temp, transfer_id, target)
                self._emit_failed(transfer_id, target, len(raw), actual,
                                  expected, 1)
                return {
                    "ok": False,
                    "error": "sha256 mismatch after write: file hashes to "
                             "%s, expected %s" % (actual, expected),
                    "path": str(target),
                    "bytes": len(raw),
                    "sha256": actual,
                    "expected_sha256": expected,
                    "quarantined": quarantined,
                }
            os.replace(temp, target)
            final_hash = _sha256_file(target)
            if final_hash != expected:
                # The rename landed different bytes than the verified
                # temp held (disk error in between). Report honestly:
                # the result names the hash of the file now at the
                # target; nothing is hidden.
                self._emit_failed(transfer_id, target, len(raw), final_hash,
                                  expected, 1)
                return {
                    "ok": False,
                    "error": "sha256 mismatch after rename: file at target "
                             "hashes to %s, expected %s"
                             % (final_hash, expected),
                    "path": str(target),
                    "bytes": len(raw),
                    "sha256": final_hash,
                    "expected_sha256": expected,
                    "quarantined": False,
                }
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass
        self._emit_done(transfer_id, target, len(raw), final_hash, 1)
        return {
            "ok": True,
            "path": str(target),
            "bytes": len(raw),
            "sha256": final_hash,
            "chunks": 1,
        }

    def init(self, path, total_bytes, total_chunks, sha256, overwrite=False):
        """Open a chunked transfer session (SS1.1/SS1.4).

        Creates no file at the target; chunk parts land under the data
        dir. Returns the session record incl. ``transfer_id``.
        """
        self._check_not_stopped()
        target = _validate_path(path, self._roots)
        expected = _validate_sha256(sha256)
        total_bytes = _validate_int(total_bytes, "total_bytes",
                                    0, MAX_FILE_BYTES)
        total_chunks = _validate_int(total_chunks, "total_chunks",
                                     0, MAX_TOTAL_CHUNKS)
        if (total_bytes == 0) != (total_chunks == 0):
            raise ValueError("total_bytes == 0 requires total_chunks == 0")
        if total_chunks and total_bytes > (total_chunks
                                          * MAX_CHUNK_DECODED_BYTES):
            raise ValueError("total_chunks too small for total_bytes: "
                             "%d bytes cannot fit in %d chunks of at most "
                             "%d bytes"
                             % (total_bytes, total_chunks,
                                MAX_CHUNK_DECODED_BYTES))
        self._check_write_target(target, overwrite)

        transfer_id = "xfer-" + uuid.uuid4().hex[:12]
        record = {
            "transfer_id": transfer_id,
            "path": target,
            "total_bytes": total_bytes,
            "total_chunks": total_chunks,
            "sha256": expected,
            "overwrite": bool(overwrite),
            "created_at": _utc_now_iso(),
            "created_mono": time.monotonic(),
            "received": set(),
            "chunk_bytes": {},
        }
        with self._registry_lock:
            self._prune_locked()
            if len(self._registry) >= MAX_SESSIONS:
                raise ValueError("too many concurrent transfers")
            self._registry[transfer_id] = record
        try:
            self._parts_dir(transfer_id).mkdir(parents=True, exist_ok=True)
        except OSError:
            with self._registry_lock:
                self._registry.pop(transfer_id, None)
            raise ValueError("cannot create transfer staging dir")
        return {
            "ok": True,
            "transfer_id": transfer_id,
            "path": str(target),
            "total_bytes": total_bytes,
            "total_chunks": total_chunks,
            "received_chunks": 0,
            "created_at": record["created_at"],
        }

    def chunk(self, transfer_id, index, data_b64):
        """Accept one chunk of an open session (SS1.1/SS1.4).

        Out-of-order delivery is allowed. A duplicate chunk with
        identical bytes is an idempotent success (``"duplicate": true``);
        a duplicate with different bytes raises ValueError.
        """
        self._check_not_stopped()
        if not isinstance(transfer_id, str) or not transfer_id:
            raise ValueError("transfer_id must be a non-empty string")
        index = _validate_int(index, "index", 0, MAX_TOTAL_CHUNKS - 1)
        raw = _decode_b64(data_b64)
        if len(raw) > MAX_CHUNK_DECODED_BYTES:
            raise ValueError("chunk exceeds the cap of %d decoded bytes"
                             % MAX_CHUNK_DECODED_BYTES)
        with self._registry_lock:
            record = self._get_session_locked(transfer_id)
            if index >= record["total_chunks"]:
                raise ValueError("chunk index %d out of range for transfer "
                                 "%s (%d chunks)"
                                 % (index, transfer_id,
                                    record["total_chunks"]))
            part = self._part_path(transfer_id, index)
            if index in record["received"]:
                # Duplicate: identical bytes = retry-safe success;
                # different bytes = protocol error, never a silent
                # overwrite of the stored part.
                try:
                    stored = part.read_bytes()
                except OSError:
                    raise ValueError("stored chunk %d for transfer %s is "
                                     "unreadable" % (index, transfer_id))
                if stored == raw:
                    return {
                        "ok": True,
                        "transfer_id": transfer_id,
                        "index": index,
                        "bytes": len(raw),
                        "received_chunks": len(record["received"]),
                        "missing": self._missing(record),
                        "duplicate": True,
                    }
                raise ValueError("chunk %d for transfer %s differs from "
                                 "the stored chunk" % (index, transfer_id))
            # Write the part via a temp sibling + rename so a crash
            # mid-write never leaves a half-written part file.
            tmp_part = part.with_suffix(".tmp")
            try:
                with open(tmp_part, "wb") as handle:
                    handle.write(raw)
                os.replace(tmp_part, part)
            finally:
                try:
                    if tmp_part.exists():
                        tmp_part.unlink()
                except OSError:
                    pass
            record["received"].add(index)
            record["chunk_bytes"][index] = len(raw)
            return {
                "ok": True,
                "transfer_id": transfer_id,
                "index": index,
                "bytes": len(raw),
                "received_chunks": len(record["received"]),
                "missing": self._missing(record),
            }

    def finalize(self, transfer_id):
        """Reassemble, verify, and atomically place the file (SS1.4).

        Missing chunks raise ValueError naming every missing index —
        never a partial file. On a hash mismatch returns
        ``{"ok": false, ...}`` with both hashes; the target is never
        touched and the bad bytes are quarantined.
        """
        self._check_not_stopped()
        if not isinstance(transfer_id, str) or not transfer_id:
            raise ValueError("transfer_id must be a non-empty string")
        with self._registry_lock:
            record = self._get_session_locked(transfer_id)
            missing = self._missing(record)
            if missing:
                raise ValueError("transfer %s is missing chunks: %s"
                                 % (transfer_id,
                                    ", ".join(str(i) for i in missing)))
            if sum(record["chunk_bytes"].values()) != record["total_bytes"]:
                raise ValueError("transfer %s byte total %d does not match "
                                 "total_bytes %d"
                                 % (transfer_id,
                                    sum(record["chunk_bytes"].values()),
                                    record["total_bytes"]))
            target = record["path"]
            expected = record["sha256"]
            total_chunks = record["total_chunks"]

        temp = self._temp_path(target, transfer_id)
        try:
            digest = hashlib.sha256()
            written = 0
            with open(temp, "wb") as handle:
                for index in range(total_chunks):
                    part = self._part_path(transfer_id, index)
                    with open(part, "rb") as part_handle:
                        while True:
                            block = part_handle.read(_READ_BLOCK)
                            if not block:
                                break
                            handle.write(block)
                            digest.update(block)
                            written += len(block)
            actual = digest.hexdigest()
            if actual != expected:
                quarantined = self._quarantine(temp, transfer_id, target)
                self._emit_failed(transfer_id, target, written, actual,
                                  expected, total_chunks)
                self._drop_session(transfer_id)
                return {
                    "ok": False,
                    "error": "sha256 mismatch after reassembly: file "
                             "hashes to %s, expected %s"
                             % (actual, expected),
                    "path": str(target),
                    "bytes": written,
                    "sha256": actual,
                    "expected_sha256": expected,
                    "quarantined": quarantined,
                }
            os.replace(temp, target)
            final_hash = _sha256_file(target)
            if final_hash != expected:
                self._emit_failed(transfer_id, target, written, final_hash,
                                  expected, total_chunks)
                self._drop_session(transfer_id)
                return {
                    "ok": False,
                    "error": "sha256 mismatch after rename: file at target "
                             "hashes to %s, expected %s"
                             % (final_hash, expected),
                    "path": str(target),
                    "bytes": written,
                    "sha256": final_hash,
                    "expected_sha256": expected,
                    "quarantined": False,
                }
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass
        self._drop_session(transfer_id)
        self._emit_done(transfer_id, target, written, final_hash,
                        total_chunks)
        return {
            "ok": True,
            "path": str(target),
            "bytes": written,
            "sha256": final_hash,
            "chunks": total_chunks,
        }

    def status(self, transfer_id):
        """The transfer_status record for ``transfer_id`` (SS1.1).

        Read-only: works even while stopped. Unknown/expired id raises
        ValueError.
        """
        if not isinstance(transfer_id, str) or not transfer_id:
            raise ValueError("transfer_id must be a non-empty string")
        with self._registry_lock:
            record = self._get_session_locked(transfer_id)
            return {
                "ok": True,
                "transfer_id": transfer_id,
                "path": str(record["path"]),
                "total_bytes": record["total_bytes"],
                "total_chunks": record["total_chunks"],
                "received_chunks": len(record["received"]),
                "received": sorted(record["received"]),
                "missing": self._missing(record),
                "created_at": record["created_at"],
            }

    def cancel(self, transfer_id):
        """Abort the session and delete its part files (SS1.1).

        Cancelling always works, even while stopped (same precedent as
        run_cancel). Unknown id raises ValueError.
        """
        if not isinstance(transfer_id, str) or not transfer_id:
            raise ValueError("transfer_id must be a non-empty string")
        with self._registry_lock:
            record = self._get_session_locked(transfer_id)
            path = str(record["path"])
        self._drop_session(transfer_id)
        return {
            "ok": True,
            "transfer_id": transfer_id,
            "cancelled": True,
            "path": path,
        }

    def verify(self, path, sha256):
        """The transfer_verify record (SS1.1): hash the file at ``path``
        and compare against the caller-supplied ``sha256``.

        Read-only: works even while stopped. ``ok`` means the op
        itself succeeded (file read and hashed); ``match`` is the
        comparison. Missing file raises ValueError.
        """
        target = _validate_path(path, self._roots)
        expected = _validate_sha256(sha256)
        if not target.is_file():
            raise ValueError("not found (or outside allowed roots): %r"
                             % (path,))
        actual = _sha256_file(target)
        return {
            "ok": True,
            "path": str(target),
            "bytes": target.stat().st_size,
            "sha256": actual,
            "expected_sha256": expected,
            "match": actual == expected,
        }

    def read(self, path, max_bytes=MAX_READ_BYTES):
        """The transfer_read record (SS1.1): the exact bytes at ``path``
        as base64, for the VM to confirm content, not just a hash.

        ``sha256`` is the hash of the FULL file, ``bytes`` is the number
        of bytes returned, and ``total_bytes`` is the full file size;
        ``truncated`` is true when the file is larger than
        ``max_bytes`` (only the first ``max_bytes`` are returned).
        Read-only: works even while stopped.
        """
        target = _validate_path(path, self._roots)
        max_bytes = _validate_int(max_bytes, "max_bytes", 1, MAX_READ_BYTES)
        if not target.is_file():
            raise ValueError("not found (or outside allowed roots): %r"
                             % (path,))
        with open(target, "rb") as handle:
            data = handle.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        return {
            "ok": True,
            "path": str(target),
            "bytes": len(data),
            "total_bytes": target.stat().st_size,
            "sha256": _sha256_file(target),
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
            "truncated": truncated,
        }

    def dispatch(self, op, command):
        """One dispatch entry point for the service handle() branch
        (SS1.2). ``command`` is the op's args dict."""
        if not isinstance(command, dict):
            raise ValueError("command must be a dict of args")
        if op == "transfer_put":
            return self.put(
                _require(command, "path"),
                _require(command, "data"),
                _require(command, "sha256"),
                overwrite=bool(command.get("overwrite", False)),
            )
        if op == "transfer_init":
            return self.init(
                _require(command, "path"),
                _require(command, "total_bytes"),
                _require(command, "total_chunks"),
                _require(command, "sha256"),
                overwrite=bool(command.get("overwrite", False)),
            )
        if op == "transfer_chunk":
            return self.chunk(
                _require(command, "transfer_id"),
                _require(command, "index"),
                _require(command, "data"),
            )
        if op == "transfer_finalize":
            return self.finalize(_require(command, "transfer_id"))
        if op == "transfer_status":
            return self.status(_require(command, "transfer_id"))
        if op == "transfer_cancel":
            return self.cancel(_require(command, "transfer_id"))
        if op == "transfer_verify":
            return self.verify(
                _require(command, "path"),
                _require(command, "sha256"),
            )
        if op == "transfer_read":
            max_bytes = command.get("max_bytes", MAX_READ_BYTES)
            if max_bytes is None:
                max_bytes = MAX_READ_BYTES
            return self.read(_require(command, "path"),
                             max_bytes=max_bytes)
        raise ValueError("unknown transfer op: %r" % (op,))

    # ------------------------------------------------------------------
    # STOP gate (SS1.6)
    # ------------------------------------------------------------------

    def _check_not_stopped(self):
        """Refuse write ops while chat/agent work is stopped (SS1.6).

        The callable is wired by get_transfer() to the dispatcher
        state; None means the gate is open (direct unit use)."""
        if self._is_stopped is not None and self._is_stopped():
            raise RuntimeError("Transfer stopped: chat/agent work is stopped")

    # ------------------------------------------------------------------
    # Write-target checks (SS1.3)
    # ------------------------------------------------------------------

    def _check_write_target(self, target, overwrite):
        """Parent must exist; target must not be a directory; without
        overwrite=True an existing target is refused (SS1.3)."""
        if target.is_dir():
            raise ValueError("target is a directory: %r" % (str(target),))
        if not target.parent.is_dir():
            raise ValueError("parent directory does not exist: %r"
                             % (str(target.parent),))
        if target.exists() and not overwrite:
            raise ValueError("target already exists: %r; pass "
                             "overwrite=True" % (str(target),))

    # ------------------------------------------------------------------
    # Sessions (SS1.4)
    # ------------------------------------------------------------------

    def _parts_dir(self, transfer_id):
        return self._transfers_dir / transfer_id

    def _part_path(self, transfer_id, index):
        return self._parts_dir(transfer_id) / ("chunk-%04d.part" % index)

    def _temp_path(self, target, transfer_id):
        """Temp sibling in the destination dir (SS1.7): same filesystem,
        so the rename is atomic; dot-prefixed so it never collides
        with a real file name."""
        return target.parent / (".%s.deskhand-tmp-%s"
                                % (target.name, transfer_id))

    def _missing(self, record):
        """Sorted indexes not yet received. Caller holds _registry_lock."""
        return sorted(set(range(record["total_chunks"]))
                      - record["received"])

    def _get_session_locked(self, transfer_id):
        """The session record, or ValueError. Prunes it first if it has
        expired (SS1.4). Caller holds _registry_lock."""
        record = self._registry.get(transfer_id)
        if record is None:
            raise ValueError("unknown transfer_id: %r" % (transfer_id,))
        if time.monotonic() - record["created_mono"] > SESSION_MAX_AGE_S:
            self._registry.pop(transfer_id, None)
            self._remove_parts(transfer_id)
            raise ValueError("unknown or expired transfer_id: %r"
                             % (transfer_id,))
        return record

    def _drop_session(self, transfer_id):
        """Remove the session and its part files (SS1.4). Never raises
        out of a finalize/cancel path."""
        with self._registry_lock:
            self._registry.pop(transfer_id, None)
        self._remove_parts(transfer_id)

    def _remove_parts(self, transfer_id):
        try:
            shutil.rmtree(self._parts_dir(transfer_id),
                          ignore_errors=True)
        except Exception:
            logger.warning("transfer parts cleanup failed for %s",
                           transfer_id, exc_info=True)

    def _prune_locked(self):
        """Drop expired sessions (SS1.4). Caller holds _registry_lock."""
        now_mono = time.monotonic()
        expired = [tid for tid, record in self._registry.items()
                   if now_mono - record["created_mono"] > SESSION_MAX_AGE_S]
        for transfer_id in expired:
            self._registry.pop(transfer_id, None)
            self._remove_parts(transfer_id)

    def _prune_sweep(self):
        """Best-effort sweep of orphaned part dirs (SS1.4): a crashed
        service leaves part files behind; any parts dir whose mtime is
        older than the session max age is removed at construction. A
        live session's dir is touched on every chunk, so an idle dir
        older than the max age is already an expired session."""
        try:
            if not self._transfers_dir.is_dir():
                return
            cutoff = time.time() - SESSION_MAX_AGE_S
            for entry in self._transfers_dir.iterdir():
                if entry.name == "quarantine" or not entry.is_dir():
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry, ignore_errors=True)
                except OSError:
                    continue
        except Exception:
            logger.warning("transfer prune sweep failed", exc_info=True)

    # ------------------------------------------------------------------
    # Quarantine (SS1.7)
    # ------------------------------------------------------------------

    def _quarantine(self, temp, transfer_id, target):
        """Move a hash-mismatched temp file to the quarantine dir.

        Returns True when the bad bytes were preserved, False when the
        move failed and the temp was deleted instead. Quarantine keeps
        the evidence (was the sender's hash wrong, or did the bytes
        corrupt in transit?) instead of deleting it. The quarantine
        dir is never auto-pruned — one file per failed transfer.
        """
        dest = (self._quarantine_dir
                / ("%s-%s" % (transfer_id, target.name)))
        try:
            self._quarantine_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp), str(dest))
            return True
        except Exception:
            logger.warning("transfer quarantine failed for %s; deleting "
                           "the bad temp file instead",
                           transfer_id, exc_info=True)
            try:
                temp.unlink()
            except OSError:
                pass
            return False

    # ------------------------------------------------------------------
    # vm-events (SS1.9): metadata only, completed writes only.
    # ------------------------------------------------------------------

    def _emit(self, entry):
        """Emit one vm-event; a failing sink never propagates (SS1.9).

        The transfer already completed or failed; the feed is the
        view, the transfer is the work.
        """
        if self._emit_event is None:
            return
        try:
            self._emit_event(entry)
        except Exception:
            logger.warning("transfer event sink failed; continuing",
                           exc_info=True)

    def _emit_done(self, transfer_id, target, size, sha256, chunks):
        self._emit({
            "source": "transfer",
            "type": "transfer_done",
            "level": "INFO",
            "summary": "transfer wrote %s (%d bytes, %d chunks)"
                       % (target.name, size, chunks),
            "detail": {
                "transfer_id": transfer_id,
                "path": str(target),
                "bytes": size,
                "sha256": sha256,
                "chunks": chunks,
            },
        })

    def _emit_failed(self, transfer_id, target, size, actual, expected,
                     chunks):
        self._emit({
            "source": "transfer",
            "type": "transfer_failed",
            "level": "WARN",
            "summary": "transfer failed for %s: sha256 mismatch"
                       % target.name,
            "detail": {
                "transfer_id": transfer_id,
                "path": str(target),
                "bytes": size,
                "sha256": actual,
                "expected_sha256": expected,
                "chunks": chunks,
            },
        })


# ---------------------------------------------------------------------------
# Service accessor (SS1.2): one lazily-created Transfer per service.
# ---------------------------------------------------------------------------

def get_transfer(service):
    """Return the service's single Transfer, creating it on first use.

    Wired to ``service._append_vm_event`` and ``service.data``, with
    the STOP gate wired to the dispatcher state. The service branch
    does a lazy ``from deskhand_transfer import get_transfer`` so a
    broken transfer module cannot break service startup.

    The gate is fail-closed: if the dispatcher state cannot be read
    at all, writes are refused rather than allowed on an unknown
    state.
    """
    transfer = getattr(service, "_transfer", None)
    if transfer is None:
        def _service_stopped():
            try:
                return bool(service.dispatcher.admission_state()["stopped"])
            except Exception:
                return True

        transfer = Transfer(
            emit_event=getattr(service, "_append_vm_event", None),
            data_dir=getattr(service, "data", None),
            is_stopped=_service_stopped,
        )
        service._transfer = transfer
    return transfer
