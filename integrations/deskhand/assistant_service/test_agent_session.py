import unittest
import uuid

from deskhand_agent_session import run_agent_session
from deskhand_local_tools import LocalToolAdapter


class AgentSessionTests(unittest.TestCase):
    def test_stop_while_model_is_responding_prevents_action(self):
        task, turn = str(uuid.uuid4()), str(uuid.uuid4())
        class Host:
            active = True
            actions = 0
            def call(self, op, payload):
                if op == "task.check": return {"active": self.active}
                self.actions += 1
                raise AssertionError("action started after STOP")
        host = Host()
        def model(*_):
            host.active = False
            return {"role": "assistant", "tool_calls": [{"function": {"name": "filesystem.roots", "arguments": {}}}]}
        result = run_agent_session(task_id=task, turn_id=turn, adapter=LocalToolAdapter(task_id=task, turn_id=turn, prompt="test", transport=model), host=host, stopped=lambda: False)
        self.assertEqual(result.outcome, "cancelled")
        self.assertEqual(host.actions, 0)

    def test_missing_host_authority_fails_closed(self):
        class Host:
            def call(self, op, payload): raise TimeoutError("unavailable")
        calls = []
        result = run_agent_session(task_id=str(uuid.uuid4()), turn_id=str(uuid.uuid4()), adapter=lambda _: calls.append(1), host=Host(), stopped=lambda: False)
        self.assertEqual(result.outcome, "cancelled")
        self.assertEqual(calls, [])

    def test_local_tool_round_trip_to_host_and_back(self):
        task, turn = str(uuid.uuid4()), str(uuid.uuid4())
        messages = []
        def model(history, _tools):
            messages.append(history)
            if len(messages) == 1:
                return {"role": "assistant", "tool_calls": [{"function": {"name": "desktop.list", "arguments": {"folder_token": None}}}]}
            return {"role": "assistant", "content": "Found the file."}
        class Host:
            def call(self, op, payload):
                if op == "task.check": return {"active": True}
                self.payload = payload
                return {"actionId": payload["action_id"], "status": "completed", "data": {"items": ["fixture.txt"]}}
        host = Host()
        adapter = LocalToolAdapter(task_id=task, turn_id=turn, prompt="list", transport=model)
        result = run_agent_session(task_id=task, turn_id=turn, adapter=adapter, host=host, stopped=lambda: False)
        self.assertEqual(result.text, "Found the file.")
        self.assertEqual(host.payload["task_id"], task)
        self.assertIn("fixture.txt", messages[-1][-1]["content"])

    def test_revoked_host_stops_without_another_model_call(self):
        task, turn = str(uuid.uuid4()), str(uuid.uuid4())
        calls = []
        def model(*_):
            calls.append(1)
            return {"role": "assistant", "tool_calls": [{"function": {"name": "filesystem.roots", "arguments": {}}}]}
        class Host:
            def call(self, op, payload):
                if op == "task.check": return {"active": True}
                return {"actionId": payload["action_id"], "status": "rejected", "error": "authorization_required"}
        result = run_agent_session(task_id=task, turn_id=turn, adapter=LocalToolAdapter(task_id=task, turn_id=turn, prompt="test", transport=model), host=Host(), stopped=lambda: False)
        self.assertIn("cancelled", result.text)
        self.assertEqual(len(calls), 1)
