"""Model-neutral task loop. It does not supply a Windows executor or authority.

Adapters return either final text or a validated tool request. The host callback
must independently enforce task identity, permissions, STOP, approvals and
durable action reservation. No failed or uncertain call is automatically retried.
"""
from dataclasses import dataclass
import json

from deskhand_tool_protocol import ToolRequest

OBSERVATION_ACTIONS = frozenset({"window.list", "window.observe", "window.inspect", "window.screenshot", "desktop.list", "filesystem.roots", "filesystem.next", "filesystem.read", "filesystem.read_chunk"})


@dataclass(frozen=True)
class Final:
    text: str
    outcome: str = "completed"


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    status: str
    output: str = ""


def run_task(*, task_id, turn_id, adapter, execute, stopped, max_rounds=64):
    if type(max_rounds) is not int or not 1 <= max_rounds <= 64:
        raise ValueError("invalid task budget")
    results = ()
    seen_requests = set()
    seen_actions = set()
    uncertain = False
    last_failure = False
    for _ in range(max_rounds):
        if stopped():
            deliver = getattr(adapter, "deliver_terminal", None)
            if results and callable(deliver):
                deliver(results)
            return Final("Task stopped.", "cancelled")
        proposal = adapter(results)
        # A model may have been generating while STOP or grant expiry happened.
        if stopped():
            cancel = getattr(adapter, "deliver_terminal", None)
            if isinstance(proposal, ToolRequest) and len(proposal.actions) == 1 and callable(cancel):
                # execute() was never called: cancellation is known, not an
                # inferred outcome of a possibly dispatched operation.
                cancel((ActionResult(proposal.actions[0].action_id, "cancelled",
                    json.dumps({"error": "stopped", "data": {}})),))
            return Final("Task stopped.", "cancelled")
        if isinstance(proposal, Final):
            if not isinstance(proposal.text, str) or not proposal.text.strip():
                raise ValueError("empty model reply")
            if uncertain:
                return Final(proposal.text + "\n\nAn earlier action was unconfirmed; no further changes were attempted.", "failed")
            if last_failure:
                return Final(proposal.text + "\n\nThe last attempted action did not complete successfully.", "failed")
            return proposal
        if not isinstance(proposal, ToolRequest) or proposal.task_id != task_id or proposal.turn_id != turn_id:
            raise ValueError("task identity mismatch")
        if proposal.request_id in seen_requests or any(a.action_id in seen_actions for a in proposal.actions):
            raise ValueError("replayed model action")
        # After an unconfirmed side effect the model may inspect, but it may not
        # issue any additional mutations or retry the action automatically.
        if uncertain and any(action.name not in OBSERVATION_ACTIONS for action in proposal.actions):
            return Final("Task paused: an earlier action was unconfirmed, so no further changes were attempted.", "failed")
        seen_requests.add(proposal.request_id)
        seen_actions.update(a.action_id for a in proposal.actions)
        current = []
        for action in proposal.actions:
            if stopped():
                deliver = getattr(adapter, "deliver_terminal", None)
                if len(proposal.actions) == 1 and callable(deliver):
                    deliver((ActionResult(action.action_id, "cancelled",
                        json.dumps({"error": "stopped", "data": {}})),))
                return Final("Task stopped.", "cancelled")
            result = execute(proposal, action)
            if not isinstance(result, ActionResult) or result.action_id != action.action_id or result.status not in {
                    "completed", "failed", "rejected", "cancelled", "uncertain"}:
                raise ValueError("invalid host action result")
            current.append(result)
            if result.status == "cancelled":
                deliver = getattr(adapter, "deliver_terminal", None)
                if callable(deliver):
                    deliver(tuple(current))
                return Final("Task stopped; the last action was cancelled.", "cancelled")
            if result.status in {"failed", "rejected"} and len(proposal.actions) != 1:
                return Final("Task paused: an action in a batch failed or was rejected; remaining actions were not attempted.", "failed")
            if result.status == "uncertain":
                uncertain = True
                # Do not execute any remaining actions in the same proposal.
                if len(proposal.actions) != 1:
                    return Final("Task paused: an action in a batch was unconfirmed; remaining actions were not attempted.", "failed")
                break
        results = tuple(current)
        last_failure = any(result.status in {"failed", "rejected"} for result in current)
    return Final("Task paused after reaching its action limit.", "failed")
