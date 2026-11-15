"""Smarts registry + service op handler, v100.008 (Batch 3).

Thin wiring layer: the service op handler `handle_smarts_op` dispatches
the read-only smarts ops. The wiring patch script
(apply_smarts_wiring_100005.py, in deskhand-port-work/) inserts ONE
branch into ChatService.handle that calls this function — no other
service edit is needed for Batch 3.

Ops (all pure/library; none executes a desktop action, none changes
grants, none touches STOP):
- smarts_status: version + module list (no state, no grant needed).
- grounding_fuse: fuse screenshot + UI tree into a grounding observation.
- index_search: search the app/file index (in-scope only).
- checkpoint_save / checkpoint_resume: resumable plan state.
- memory_list: list Phil-approved desktop-memory facts.
- skill_list: list available reviewed skills.

State lives on the ChatService instance (lazy attributes), never in
module globals — so a service restart drops it, as it should.
"""
from __future__ import annotations

__version__ = "100.008"

# B2-D (100.030): deskhand_plan, deskhand_recovery,
# deskhand_step_router and deskhand_benchmark were removed -- zero
# production callers (test-only library code with no host consumer).
MODULES = (
    "deskhand_grounding", "deskhand_channel",
    "deskhand_evidence", "deskhand_index",
    "deskhand_checkpoint", "deskhand_tools",
    "deskhand_memory", "deskhand_skills",
)
OPS = (
    "smarts_status", "grounding_fuse", "index_search",
    "checkpoint_save", "checkpoint_resume", "memory_list", "skill_list",
)


def smarts_status():
    return {"version": __version__, "modules": list(MODULES), "ops": list(OPS),
            "stop_semantics": "unchanged (F37 — Phil/Codex decision)",
            "move_retry": "disabled until the SS9.4 host diagnosis lands"}


def handle_smarts_op(service, command):
    """Dispatch one smarts op. `service` is the ChatService instance
    (duck-typed: needs `.data` for checkpoint/index/memory paths).
    Returns a JSON-able dict. Raises ValueError on a bad op/args."""
    op = command.get("op")
    if op == "smarts_status":
        return smarts_status()
    if op == "grounding_fuse":
        from deskhand_grounding import GroundingRegistry
        if not hasattr(service, "_grounding"):
            service._grounding = GroundingRegistry()
        return service._grounding.fuse(
            task_id=command["task_id"], turn_id=command["turn_id"],
            screenshot_token=command["screenshot_token"],
            tree=command["tree"])
    if op == "index_search":
        from deskhand_index import AppFileIndex
        if not hasattr(service, "_index"):
            service._index = AppFileIndex()
        return service._index.search(command["query"])
    if op == "checkpoint_save":
        from deskhand_checkpoint import save
        path = save(service.data, command["task_id"], goal=command["goal"],
                    completed_steps=command["completed_steps"],
                    next_step=command["next_step"],
                    receipts=command.get("receipts") or [])
        return {"saved": True, "path": str(path)}
    if op == "checkpoint_resume":
        from deskhand_checkpoint import resume
        return resume(service.data, command["task_id"],
                      new_task_id=command["new_task_id"],
                      new_turn_id=command["new_turn_id"],
                      grants=command["grants"])
    if op == "memory_list":
        from deskhand_memory import list_facts
        from pathlib import Path
        store = Path(service.data) / "desktop_memory.json"
        return {"facts": list_facts(store)}
    if op == "skill_list":
        from deskhand_skills import list_skills
        return {"skills": list_skills()}
    raise ValueError("Unknown smarts operation: " + str(op))
