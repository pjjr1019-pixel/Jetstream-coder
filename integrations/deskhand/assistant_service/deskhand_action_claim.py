"""Atomic action-claim store for the Deskhand laptop-agent fallback (v2).

GPT gap 5 (2026-11-08 verdict): task ID + journal alone do not
establish durable cross-route dedup or prevent replay after a crash,
and a task ID may identify multiple actions. This module is the v2
answer: a single enforcement point on the PC that binds one host-issued
task ID to exactly one action payload, atomically, durably, and
crash-safely.

Design
------
* One claim file per task ID, named by sha256("task:" + task_id).
  One task ID can therefore ever own one claim, hence one payload.
  A second, different payload under the same task ID is refused
  (``ClaimConflict``) -- the task is bound to the first payload it
  claimed. The same payload under a *different* task ID is a different
  claim: that is legitimate (e.g. opening Notepad in two separate
  turns), so there is deliberately NO payload index.
* ``action_id`` = sha256 of the canonical JSON of
  ``{"task_id": ..., "action": ...}`` (sort_keys, compact separators).
  The claim file stores the hash, the kind, and the task ID -- never
  the payload content itself (a files_write payload could hold
  anything; the store must not become a second copy of it).
* Claim creation is atomic: write the full record to a temp file,
  fsync, then ``os.link()`` it into place (atomic, fails when the
  target exists) + fsync of the containing directory. Exactly one
  concurrent claimant wins, in-process or cross-process, and readers
  only ever see complete files. A ``threading.Lock`` in the op
  module does NOT protect the runner process or the gateway, and this
  store does not pretend otherwise.
* States: ``claimed`` -> ``executed`` | ``failed`` | ``uncertain``.
  Terminal states are final, except for the two transitions the
  operator may make explicitly via ``resolve()`` (see below).
  - ``failed`` means dispatch PROVABLY never started (validation,
    gate refusal, dispatch-boundary refusal, health-check failure,
    config error, bad screenshot). A retry of the same payload is
    allowed: ``claim()`` re-claims a ``failed`` claim (history
    records the supersede).
  - ``uncertain`` means dispatch started and the outcome is unknown
    (agent timeout, transport error after send, 401, any other
    post-dispatch exception) OR the claim was left ``claimed`` by a
    crash. An ``uncertain`` claim NEVER auto-retries and NEVER
    auto-replays: a new attempt for the same task is refused until a
    human/operator resolves it with ``resolve()`` after checking
    independent evidence (agent request counter, agent log, receipt).
    A ``claimed`` claim older than ``CLAIM_STALE_SECS`` (B2-D H12) is
    the exception: no live dispatch can still hold it, so ``claim()``
    re-claims it for a retry and ``resolve()`` accepts it; a fresh
    ``claimed`` claim still refuses exactly as before.
  - ``executed`` is terminal: never re-executed, ever.
  - ``claimed`` with no terminal state on a later call is treated
    exactly like ``uncertain`` (in-flight or crashed -- never assume).
* Settle transitions use write-temp + ``os.replace`` + fsync, so a
  crash mid-settle leaves either the old state or the new state,
  never a torn file. Only the claim owner settles (the process that
  created the claim); ``resolve()`` is the sole exception and must
  only run when no dispatch for that task is in flight.
* This store is the FALLBACK route's store. It does NOT give
  cross-route dedup with the vendor gateway: the gateway
  (``executor.submit``) does not claim through this store -- there is
  no VM copy of the gateway and no evidence it could. Cross-route
  safety in v2 rests on pre-check-only routing (the caller never
  attempts both routes for one action), not on a shared store.
  Gateway-claims-through-this-store is vendor cooperation, listed as
  an open item in fallback-hook-design-v2.md.

Local-filesystem assumption: the store dir lives under the service
data dir (``%LOCALAPPDATA%\\Deskhand\\assistant`` on the PC), which
is a local disk. ``O_EXCL`` atomicity is a local-filesystem
guarantee; a network-backed store dir would weaken it.

Standard library only. No tokens, no secrets, no image data ever
written here.
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

CLAIM_VERSION = 1
CLAIM_STATES = ("claimed", "executed", "failed", "uncertain")
TERMINAL_STATES = ("executed", "failed", "uncertain")
# B2-D H12 (100.030): a claim left "claimed" by a crash used to brick its
# task ID forever (resolve() only accepted "uncertain"; claim()/settle()
# refused "claimed"). A claim older than this is stale and may be
# reclaimed/retried or operator-resolved. Bound: 3600 s (1 h). The
# longest legitimate hold on a fallback claim is one acting call under
# the 30 s agent timeout (TaskAgentClient DEFAULT_TIMEOUT) plus process
# overhead, so 1 h is ~100x any live dispatch and cannot fire while a
# dispatch is still in flight; a fresh "claimed" claim is still refused
# exactly as before. No timeout constant existed in this file to reuse.
CLAIM_STALE_SECS = 3600


class ClaimError(RuntimeError):
    """Base error for the claim store."""


class ClaimConflict(ClaimError):
    """The task ID is already bound to a different action payload."""


class ClaimStateError(ClaimError):
    """The claim is in a state that forbids the requested transition."""


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _claim_age_seconds(claim):
    """Age of a claim in seconds from its last update, or None when the
    timestamps are missing/unparseable (fail closed: age unproven, so
    the claim is NOT treated as stale)."""
    stamp = claim.get("ts_updated") or claim.get("ts_claimed")
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def _is_stale_claim(claim):
    """True only when the claim is provably older than CLAIM_STALE_SECS."""
    age = _claim_age_seconds(claim)
    return age is not None and age > CLAIM_STALE_SECS


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def payload_hash(action):
    """sha256 hex of the canonical action payload (no task ID)."""
    return hashlib.sha256(_canonical(action).encode("utf-8")).hexdigest()


def canonical_action_id(task_id, action):
    """sha256 hex binding task ID + full action payload."""
    return hashlib.sha256(
        _canonical({"task_id": task_id, "action": action})
        .encode("utf-8")).hexdigest()


def _claim_filename(task_id):
    return hashlib.sha256(
        ("task:" + task_id).encode("utf-8")).hexdigest() + ".json"


def _fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path):
    # Directory fsync is best-effort on Windows (opening a directory
    # for fsync is not supported there); the file fsync + atomic
    # rename still hold. Never let a dir-fsync failure break a claim.
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class ClaimStore:
    """Durable, atomic per-task action claims in ``store_dir``."""

    def __init__(self, store_dir):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id):
        return self.store_dir / _claim_filename(task_id)

    def read(self, task_id):
        """Return the claim dict for ``task_id``, or None."""
        try:
            raw = self._path(task_id).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            claim = json.loads(raw)
        except ValueError:
            return None
        return claim if isinstance(claim, dict) else None

    def claim(self, task_id, action, *, route, gateway_state=None,
              approval=None):
        """Atomically claim ``task_id`` for ``action``.

        Returns ``(claim, owned)``. ``owned`` True means this call
        created the claim (state ``claimed``) and owns the dispatch.
        ``owned`` False means a claim already existed:
          * same action_id, state ``failed`` -> re-claimed for a
            retry (state back to ``claimed``, history appended);
          * same action_id, state ``executed`` -> returns the claim;
            the caller must refuse (duplicate);
          * same action_id, state ``claimed`` or ``uncertain`` ->
            returns the claim; the caller must refuse (in-flight,
            crashed, or uncertain -- never assume, never replay).
            Exception (B2-D H12): a ``claimed`` claim older than
            ``CLAIM_STALE_SECS`` is re-claimed (``owned`` True) -- no
            live dispatch can still hold it, so the task recovers
            instead of bricking.
        Raises ``ClaimConflict`` when the task is already bound to a
        different action payload. Raises ``ValueError`` for a bad
        task ID / action.
        """
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(action, dict) or not isinstance(
                action.get("kind"), str) or not action.get("kind"):
            raise ValueError("action must be a dict with a kind")
        action_id = canonical_action_id(task_id, action)
        kind = action["kind"]
        path = self._path(task_id)
        now = _utcnow()
        record = {
            "version": CLAIM_VERSION,
            "action_id": action_id,
            "task_id": task_id,
            "kind": kind,
            "payload_hash": payload_hash(action),
            "route": route,
            "state": "claimed",
            "ts_claimed": now,
            "ts_updated": now,
            "gateway_state": gateway_state,
            "approval": approval,
            "history": [{"state": "claimed", "ts": now,
                         "detail": "claim created"}],
        }
        # Atomic create: write the full record to a temp file, fsync
        # it, then hard-link it into place. os.link() is atomic and
        # fails with FileExistsError when the target exists, so
        # exactly one concurrent claimant wins -- and readers only
        # ever see a COMPLETE claim file. (A plain O_EXCL create +
        # write has a hole: a concurrent reader can see the file
        # before the writer has finished it.)
        tmp = self.store_dir / (".%s.tmp-%d-%d"
                                 % (path.name, os.getpid(),
                                    threading.get_ident()))
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            existing = self.read(task_id)
            if existing is None:
                raise ClaimStateError(
                    "claim file for task exists but is unreadable; "
                    "refusing rather than overwriting")
            if existing.get("action_id") != action_id:
                raise ClaimConflict(
                    "task %s is already bound to a different action "
                    "(kind %r); refusing a second payload under one "
                    "task ID" % (task_id[:80],
                                 existing.get("kind")))
            state = existing.get("state")
            if state == "failed":
                # Proven non-execution: allow one retry of the SAME
                # payload. Re-claim by atomic replace.
                existing["state"] = "claimed"
                existing["ts_updated"] = now
                existing.setdefault("history", []).append(
                    {"state": "claimed", "ts": now,
                     "detail": "re-claim after failed (retry)"})
                self._replace(path, existing)
                return existing, True
            if state == "claimed" and _is_stale_claim(existing):
                # B2-D H12: crash-left claim older than CLAIM_STALE_SECS.
                # No dispatch can still hold it (see the constant's
                # note), so the task is recoverable instead of bricked:
                # re-claim for the SAME payload, with the stale age on
                # the record. A fresh "claimed" claim still refuses.
                age = _claim_age_seconds(existing)
                existing["state"] = "claimed"
                existing["ts_updated"] = now
                existing.setdefault("history", []).append(
                    {"state": "claimed", "ts": now,
                     "detail": "re-claim after stale claimed (age %.0fs; "
                               "original claim never settled)" % age})
                self._replace(path, existing)
                return existing, True
            # executed / claimed (fresh) / uncertain / unknown: not ours
            # to re-claim. Caller refuses based on state.
            return existing, False
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        _fsync_dir(self.store_dir)
        return record, True

    def settle(self, task_id, action_id, state, detail=None):
        """Move an owned ``claimed`` claim to a terminal state.

        ``state`` must be ``executed``, ``failed`` or ``uncertain``.
        Only a ``claimed`` claim settles; terminal claims never
        move (use ``resolve()`` for the explicit operator path).
        Raises ``ClaimStateError`` on a missing claim, an action_id
        mismatch, or a non-``claimed`` current state.
        """
        if state not in TERMINAL_STATES:
            raise ValueError("settle state must be a terminal state")
        path = self._path(task_id)
        claim = self.read(task_id)
        if claim is None:
            raise ClaimStateError("no claim for task; cannot settle")
        if claim.get("action_id") != action_id:
            raise ClaimStateError(
                "action_id mismatch; refusing to settle another action")
        if claim.get("state") != "claimed":
            raise ClaimStateError(
                "claim is %r, not claimed; terminal states are final"
                % (claim.get("state"),))
        now = _utcnow()
        claim["state"] = state
        claim["ts_updated"] = now
        claim.setdefault("history", []).append(
            {"state": state, "ts": now, "detail": detail or ""})
        self._replace(path, claim)
        return claim

    def resolve(self, task_id, action_id, resolution, *, resolved_by,
                detail=None):
        """Operator-only: move an ``uncertain`` claim to terminal.

        ``resolution`` is ``failed`` (independent evidence proves the
        action never ran -- e.g. the agent request counter did not
        increment) or ``executed`` (evidence proves it ran). Requires
        ``resolved_by`` (who resolved it, non-empty). Never called by
        the dispatch path; never automatic. Raises ``ClaimStateError``
        unless the claim is currently ``uncertain`` -- or ``claimed``
        and stale (B2-D H12: older than ``CLAIM_STALE_SECS``; a crash
        left it unsettled and no live dispatch can still hold it, so
        the operator may resolve it instead of the task bricking).
        A fresh ``claimed`` claim is still refused: it may be in flight.
        """
        if resolution not in ("failed", "executed"):
            raise ValueError("resolution must be failed or executed")
        if not isinstance(resolved_by, str) or not resolved_by.strip():
            raise ValueError("resolved_by is required")
        path = self._path(task_id)
        claim = self.read(task_id)
        if claim is None:
            raise ClaimStateError("no claim for task; cannot resolve")
        if claim.get("action_id") != action_id:
            raise ClaimStateError(
                "action_id mismatch; refusing to resolve another action")
        state = claim.get("state")
        if state == "claimed" and _is_stale_claim(claim):
            pass  # stale crash-left claim: operator resolution allowed
        elif state != "uncertain":
            raise ClaimStateError(
                "claim is %r, not uncertain (and not a stale claimed "
                "claim); only uncertain or stale-claimed claims can be "
                "resolved" % (state,))
        now = _utcnow()
        claim["state"] = resolution
        claim["ts_updated"] = now
        claim.setdefault("history", []).append(
            {"state": resolution, "ts": now,
             "detail": "operator resolution by %s: %s"
                       % (resolved_by, detail or "")})
        self._replace(path, claim)
        return claim

    def _replace(self, path, claim):
        """Atomic replace of a claim file (temp + rename + fsync)."""
        tmp = path.with_name(
            ".%s.tmp-%d" % (path.name, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(claim, ensure_ascii=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(self.store_dir)
