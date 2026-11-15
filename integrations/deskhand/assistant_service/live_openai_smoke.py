"""Optional live model smoke test with simulated host data only."""
import json
import sys
import uuid
from deskhand_dispatch import Route
from deskhand_local_tools import LocalToolAdapter
from deskhand_openai_local import OpenAILocalChat, OpenAILocalToolTransport
from deskhand_tool_loop import ActionResult, run_task

model = sys.argv[1]
task, turn = str(uuid.uuid4()), str(uuid.uuid4())
chat = OpenAILocalChat(Route("local", model, "", ""), "http://127.0.0.1:11434", timeout=180)
adapter = LocalToolAdapter(task_id=task, turn_id=turn, prompt="Use filesystem.roots and report the name returned.", transport=OpenAILocalToolTransport(chat))
calls = []
def execute(_, action):
    calls.append(action.name)
    return ActionResult(action.action_id, "completed", json.dumps({"data": {"name": "DeskhandFixtureDrive"}}))
result = run_task(task_id=task, turn_id=turn, adapter=adapter, execute=execute, stopped=lambda: False)
print("actions:", calls, "outcome:", result.outcome, "reply:", result.text)
if "filesystem.roots" not in calls or "DeskhandFixtureDrive" not in result.text:
    raise SystemExit("Model did not complete the tool round trip")
