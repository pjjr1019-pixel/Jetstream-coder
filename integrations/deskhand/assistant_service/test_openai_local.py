import io
import json
import unittest
import uuid

from deskhand_dispatch import Route
from deskhand_local_tools import LocalToolAdapter
from deskhand_openai_local import OpenAILocalChat, OpenAILocalToolTransport
from deskhand_tool_loop import ActionResult, run_task


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class Client:
    def __init__(self, responses): self.responses, self.requests = list(responses), []
    def open(self, request, timeout):
        self.requests.append(json.loads(request.data))
        return Response(json.dumps(self.responses.pop(0)).encode())


def response(message, model="fixture"):
    return {"model": model, "choices": [{"message": message}]}


class OpenAILocalTests(unittest.TestCase):
    def test_empty_completion_gets_one_safe_generation_retry(self):
        chat = OpenAILocalChat(Route("local", "fixture", "", ""), "http://127.0.0.1:1234")
        chat.client = Client([response({"role": "assistant", "content": ""}), response({"role": "assistant", "content": "Ready"})])
        answer = OpenAILocalToolTransport(chat)([{"role": "user", "content": "test"}], [])
        self.assertEqual(answer["content"], "Ready")
        self.assertEqual(len(chat.client.requests), 2)

    def test_tool_round_trip_matches_ids_and_actual_result(self):
        route = Route("local", "fixture", "", "")
        chat = OpenAILocalChat(route, "http://127.0.0.1:1234")
        chat.client = Client([
            response({"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "desktop.list", "arguments": '{"folder_token":null}'}}]}),
            response({"role": "assistant", "content": "Found fixture"})])
        task, turn = str(uuid.uuid4()), str(uuid.uuid4())
        adapter = LocalToolAdapter(task_id=task, turn_id=turn, prompt="List files", transport=OpenAILocalToolTransport(chat))
        final = run_task(task_id=task, turn_id=turn, adapter=adapter,
            execute=lambda _, action: ActionResult(action.action_id, "completed", '{"data":{"items":["fixture.txt"]}}'), stopped=lambda: False)
        self.assertEqual(final.text, "Found fixture")
        sent = chat.client.requests[1]["messages"]
        self.assertEqual(sent[-1]["tool_call_id"], "call-1")
        self.assertIn("fixture.txt", sent[-1]["content"])
        self.assertTrue(chat.client.requests[0]["tools"])

    def test_rejects_remote_origins_and_ambiguous_arguments(self):
        route = Route("local", "fixture", "", "")
        with self.assertRaises(ValueError): OpenAILocalChat(route, "http://example.com")
        chat = OpenAILocalChat(route, "http://127.0.0.1:1234")
        chat.client = Client([response({"role": "assistant", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "desktop.list", "arguments": '{"folder_token":null,"folder_token":null}'}}]})])
        with self.assertRaises(ValueError): OpenAILocalToolTransport(chat)([{"role": "user", "content": "test"}], [])

    def test_no_model_substitution(self):
        chat = OpenAILocalChat(Route("local", "fixture", "", ""), "http://127.0.0.1:1234")
        chat.client = Client([response({"role": "assistant", "content": "wrong"}, model="different")])
        with self.assertRaises(ValueError): OpenAILocalToolTransport(chat)([{"role": "user", "content": "test"}], [])


if __name__ == "__main__": unittest.main()
