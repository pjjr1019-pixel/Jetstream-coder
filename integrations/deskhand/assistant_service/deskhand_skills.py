"""Reusable skills / workflows, v100.008 (Batch 3, smarts #11; F50).

A skill is a reviewed, versioned, inspectable sequence of steps
(JSON file, Phil/GPT-reviewed). Running a skill is NOT a capability
grant: every step still needs its family's grant, every write still
needs phone approval where the host requires it, and a step's
verification evidence is checked before the next step runs.

Safety rules:
- Skills are data, never authority. A skill cannot widen a grant, add
  a capability, or skip a receipt.
- Token placeholders: skill steps carry "FROM_HOST" for folder/item
  tokens and "FROM_GROUNDING" for control tokens. Those are NOT real
  tokens — the host binds them at run time from its own listings /
  grounding observation before executing the step, and load_skill only
  checks the argument SHAPE. A skill file must never contain a real
  token, and a step whose token cannot be bound is blocked, never
  guessed.
- A skill may not contain destructive steps (delete/rmdir/command.run
  with destructive intent) unless flagged destructive=True AND run with
  phone_approved=True — and even then the host enforces the approval.
- A failed step stops the skill; the report says which step failed and
  why. No step is retried blindly (the tool loop never auto-retries).
- Skills live in a skills/ directory next to this module and are
  loaded by name only (no path traversal).
"""
from __future__ import annotations

import json
from pathlib import Path

from deskhand_evidence import receipt_is_verified
# Family per action name. Moved here from deskhand_plan.py (removed in
# 100.030, B2-D: the plan module had zero production callers); this
# module is its only remaining consumer (skill step validation +
# run_skill grant checks). deskhand_tools.py keeps its own smaller map
# for the new-tools subset only.
ACTION_FAMILY = {
    "desktop.list": "filesystem", "desktop.open": "filesystem",
    "filesystem.roots": "filesystem", "filesystem.next": "filesystem",
    "filesystem.read": "filesystem", "filesystem.read_chunk": "filesystem",
    "filesystem.mkdir": "filesystem", "filesystem.create": "filesystem",
    "filesystem.replace": "filesystem", "filesystem.copy": "filesystem",
    "filesystem.move": "filesystem", "filesystem.move_folder": "filesystem",
    "filesystem.launch": "filesystem", "filesystem.delete": "filesystem",
    "filesystem.rmdir": "filesystem", "filesystem.search": "filesystem",
    "window.list": "window", "window.observe": "window",
    "window.inspect": "window", "window.activate": "window",
    "window.screenshot": "window", "window.close": "window",
    "window.wait_for": "window",
    "screen.click": "pointer", "screen.double_click": "pointer",
    "screen.right_click": "pointer",
    "screen.move": "pointer", "screen.drag": "pointer",
    "screen.scroll": "pointer", "screenshot.monitor": "window",
    "control.invoke": "accessibility", "control.type": "accessibility",
    "control.key": "accessibility",
    "control.key_down": "accessibility", "control.key_up": "accessibility",
    "clipboard.get": "clipboard", "clipboard.set": "clipboard",
    "app.launch_by_name": "filesystem",
    "command.run": "command",
}
from deskhand_tools import NEW_TOOLS, parse_new_tool
from deskhand_tool_protocol import parse_tool_request

__version__ = "100.008"

DESTRUCTIVE = frozenset({"filesystem.delete", "filesystem.rmdir", "command.run"})
MAX_STEPS = 20


def skill_dir():
    return Path(__file__).resolve().parent / "skills"


def load_skill(name):
    """Load a skill by name (no path traversal). Validates the schema
    and every step's action through the tool parsers. Returns the skill
    dict."""
    if not isinstance(name, str) or not name.strip() or "/" in name or "\\" in name \
            or ".." in name or not name.endswith(".json"):
        raise ValueError("invalid skill name")
    path = skill_dir() / name
    if not path.exists():
        raise ValueError("unknown skill: " + name)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {
            "version", "name", "description", "steps", "destructive"}:
        raise ValueError("invalid skill file")
    if document["version"] != 1 or not document["name"] or not document["description"]:
        raise ValueError("invalid skill metadata")
    if not isinstance(document["steps"], list) or not 1 <= len(document["steps"]) <= MAX_STEPS:
        raise ValueError("invalid skill steps")
    if document["destructive"] is not True and document["destructive"] is not False:
        raise ValueError("invalid destructive flag")
    for index, step in enumerate(document["steps"], start=1):
        if not isinstance(step, dict) or set(step) != {"step", "action", "verification"}:
            raise ValueError("invalid skill step")
        if step["step"] != index:
            raise ValueError("skill steps must be numbered 1..N in order")
        action = step["action"]
        if not isinstance(action, dict) or set(action) != {"name", "arguments"}:
            raise ValueError("invalid skill step action")
        if action["name"] in NEW_TOOLS:
            parse_new_tool(action["name"], action["arguments"])
        elif action["name"] in ACTION_FAMILY:
            synthetic = json.dumps({"version": 1, "type": "deskhand.actions",
                                    "task_id": "11111111-1111-4111-8111-111111111111",
                                    "turn_id": "11111111-1111-4111-8111-111111111111",
                                    "request_id": "11111111-1111-4111-8111-111111111111",
                                    "actions": [{"action_id": "11111111-1111-4111-8111-111111111111",
                                                 "name": action["name"],
                                                 "arguments": action["arguments"]}]})
            parse_tool_request(synthetic, task_id="11111111-1111-4111-8111-111111111111",
                               turn_id="11111111-1111-4111-8111-111111111111")
        else:
            raise ValueError("unsupported skill action: " + action["name"])
        if action["name"] in DESTRUCTIVE and not document["destructive"]:
            raise ValueError("destructive step in a skill not flagged destructive")
        if not isinstance(step["verification"], str) or not step["verification"]:
            raise ValueError("invalid skill verification")
    return document


def list_skills():
    directory = skill_dir()
    if not directory.exists():
        return []
    return sorted(path.name for path in directory.glob("*.json"))


def run_skill(skill, grants, executor, *, phone_approved=False):
    """Run a loaded skill. `executor(action)` returns a receipt dict.
    `grants` is the fresh grant list (list of {"capability": ...}).
    Returns {"skill": name, "outcome": "completed"|"blocked"|"failed",
    "completed_steps": [...], "failed_step": int|None, "reason": str}.
    Stops on the first failure; a step whose receipt is not verified
    counts as failed (verification evidence missing = uncertain)."""
    if skill.get("destructive") and not phone_approved:
        return {"skill": skill["name"], "outcome": "blocked",
                "completed_steps": [], "failed_step": None,
                "reason": "destructive skill needs phone approval"}
    families = {grant.get("capability") for grant in grants if isinstance(grant, dict)}
    completed = []
    for step in skill["steps"]:
        name = step["action"]["name"]
        family = ACTION_FAMILY.get(name)
        if family is None or family not in families:
            return {"skill": skill["name"], "outcome": "blocked",
                    "completed_steps": completed, "failed_step": step["step"],
                    "reason": "no %s grant for step %d (%s)" % (family, step["step"], name)}
        receipt = executor(step["action"])
        if not isinstance(receipt, dict) or receipt.get("status") != "completed" \
                or not receipt_is_verified(receipt):
            return {"skill": skill["name"], "outcome": "failed",
                    "completed_steps": completed, "failed_step": step["step"],
                    "reason": "step %d (%s) did not complete with verified evidence: %s"
                              % (step["step"], name, (receipt or {}).get("status"))}
        completed.append(step["step"])
    return {"skill": skill["name"], "outcome": "completed",
            "completed_steps": completed, "failed_step": None, "reason": ""}
