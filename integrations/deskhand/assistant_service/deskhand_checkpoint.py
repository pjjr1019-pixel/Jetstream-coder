"""Resumable plans / checkpoints, v100.008 (Batch 3, smarts #8; F49).

Persist a checkpoint after each verified step (goal, steps done with
their receipts, next step) so a task can resume after restart/reconnect
with a fresh grant — without replaying any step whose receipt was
uncertain.

Safety rules (core, non-negotiable):
- Uncertain steps are never saved as done and never replayed. save()
  refuses a step whose status is "uncertain"; resume() filters them
  defensively as well.
- Resume needs a FRESH task/turn + a FRESH grant + fresh phone approval
  for remaining writes. A checkpoint is state, not authority.
- Tokens from completed receipts are marked invalid on resume
  (tokens_valid=False): the host must re-list/re-validate before the
  next step uses them. Never trust a token across a restart.
- Checkpoints store receipts/IDs only. Any field whose key looks like a
  credential (token, secret, password, key, auth, bearer) is rejected
  on save — a checkpoint must never become a credential store.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

__version__ = "100.008"

MAX_BYTES = 131072
# B2-D M1: a task_id is used as a filename component, so it must be a
# single safe segment -- anything else ("/", "..", absolute paths) is
# rejected before it touches the filesystem. Host task IDs are UUIDs,
# which match; "task-1" style test IDs match too.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _checkpoint_path(data_dir, task_id):
    """Path for a task's checkpoint file. Validates the task_id first
    so a hostile/compromised caller cannot traverse out of data_dir
    (the old code interpolated task_id raw into the filename)."""
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        raise ValueError("invalid task_id for checkpoint filename")
    return Path(data_dir) / ("checkpoint-" + task_id + ".json")
CREDENTIAL_KEYS = ("token", "secret", "password", "passwd", "apikey", "api_key",
                   "auth", "bearer", "private", "credential")


def _check_no_credentials(value, where="checkpoint"):
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in CREDENTIAL_KEYS):
                raise ValueError("%s contains a credential-like field: %s" % (where, key))
            _check_no_credentials(item, where)
    elif isinstance(value, list):
        for item in value:
            _check_no_credentials(item, where)


def save(data_dir, task_id, *, goal, completed_steps, next_step, receipts=None):
    """Save a checkpoint. `completed_steps` is a list of {"step": int,
    "action": str, "status": "completed", "receipt_id": str}. Any step
    with status != "completed" raises ValueError. Returns the path."""
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("invalid task_id")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 200:
        raise ValueError("invalid goal")
    if not isinstance(completed_steps, list):
        raise ValueError("invalid completed_steps")
    for step in completed_steps:
        if not isinstance(step, dict) or step.get("status") != "completed":
            raise ValueError("only completed steps may be checkpointed; "
                             "uncertain steps are never saved as done")
    _check_no_credentials({"receipts": receipts or []})
    document = {"version": 1, "task_id": task_id, "goal": goal,
                "completed_steps": completed_steps, "next_step": next_step,
                "receipts": receipts or []}
    raw = json.dumps(document, ensure_ascii=True).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("checkpoint too large")
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = _checkpoint_path(directory, task_id)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return path


def load(data_dir, task_id):
    path = _checkpoint_path(data_dir, task_id)
    if not path.exists():
        return None
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("checkpoint file too large")
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("invalid checkpoint file")
    return document


def resume(data_dir, old_task_id, *, new_task_id, new_turn_id, grants):
    """Resume a checkpoint under a fresh task/turn. `grants` must be the
    fresh grant list (checked for shape only here — the host enforces
    them per step). Returns {"resumed": bool, "goal": str,
    "completed_steps": [...], "next_step": int, "tokens_valid": False,
    "requires": [...]}. Tokens are always invalid on resume."""
    document = load(data_dir, old_task_id)
    if document is None:
        return {"resumed": False, "goal": "", "completed_steps": [],
                "next_step": 1, "tokens_valid": False,
                "requires": ["no checkpoint found"]}
    if not isinstance(grants, list) or not grants:
        raise ValueError("resume requires a fresh grant list")
    completed = [step for step in document.get("completed_steps", [])
                 if isinstance(step, dict) and step.get("status") == "completed"]
    return {"resumed": True, "goal": document.get("goal", ""),
            "completed_steps": completed,
            "next_step": document.get("next_step", len(completed) + 1),
            "tokens_valid": False,
            "requires": ["fresh task %s / turn %s" % (new_task_id, new_turn_id),
                         "fresh grant", "fresh phone approval for remaining writes",
                         "host must re-list / re-validate tokens before use"]}
