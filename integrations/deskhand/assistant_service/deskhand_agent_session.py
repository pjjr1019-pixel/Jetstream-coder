"""Connect model-neutral proposals to the host-owned action authority.

The caller supplies a host-admitted task ID and an adapter bound to its model
and turn. This module never creates a grant or interprets ordinary prose.
"""
import json

from deskhand_tool_loop import ActionResult, run_task


def run_agent_session(*, task_id, turn_id, adapter, host, stopped):
    def no_authority():
        if stopped():
            return True
        try:
            response = host.call("task.check", {"task_id": task_id, "turn_id": turn_id})
            return not isinstance(response, dict) or response.get("active") is not True
        except (RuntimeError, TimeoutError):
            # Loss of host authority must never be interpreted as permission.
            return True
    def execute(proposal, action):
        if proposal.task_id != task_id or proposal.turn_id != turn_id:
            raise ValueError("task identity mismatch")
        result = host.call("action.execute", {
            "task_id": task_id, "turn_id": turn_id, "action_id": action.action_id,
            "name": action.name, "arguments": json.loads(action.arguments_json),
        })
        if not isinstance(result, dict) or result.get("actionId") != action.action_id:
            raise ValueError("host action identity mismatch")
        status = result.get("status")
        if status not in {"completed", "failed", "rejected", "cancelled", "uncertain"}:
            raise ValueError("invalid host status")
        # Grant/STOP denial ends the task; do not ask the model to try again.
        if status == "rejected" and result.get("error") == "authorization_required":
            status = "cancelled"
        return ActionResult(action.action_id, status, json.dumps({"error": result.get("error"), "data": result.get("data")}, ensure_ascii=True))

    return run_task(task_id=task_id, turn_id=turn_id, adapter=adapter, execute=execute, stopped=no_authority)
