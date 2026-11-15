"""Optional local-only model smoke test; no real desktop actions are performed."""
import sys
import uuid

from deskhand_agent_session import run_agent_session
from deskhand_dispatch import Route
from deskhand_local import LocalChat
from deskhand_local_tools import LocalToolAdapter, LocalToolTransport


def main():
    model = sys.argv[1]
    chat = LocalChat(Route("local", model, "", ""), "http://127.0.0.1:11434", timeout=180)
    task, turn = str(uuid.uuid4()), str(uuid.uuid4())
    class FixtureHost:
        actions = []
        def call(self, operation, payload):
            if operation == "task.check":
                return {"active": True}
            if operation != "action.execute":
                raise ValueError("unexpected operation")
            self.actions.append(payload["name"])
            return {"actionId": payload["action_id"], "status": "completed", "data": {"items": [{"name": "deskhand-fixture.txt"}]}}
    host = FixtureHost()
    adapter = LocalToolAdapter(task_id=task, turn_id=turn,
        prompt="Use the desktop.list tool with a null folder_token, then report the names it returns.",
        transport=LocalToolTransport(chat))
    result = run_agent_session(task_id=task, turn_id=turn, adapter=adapter, host=host, stopped=lambda: False)
    print("actions:", host.actions)
    print("outcome:", result.outcome)
    print("reply:", result.text)
    if "desktop.list" not in host.actions or "deskhand-fixture.txt" not in result.text:
        raise SystemExit("Model did not complete the tool round trip")


if __name__ == "__main__":
    main()
