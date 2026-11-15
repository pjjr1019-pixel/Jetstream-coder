import json
import unittest
import uuid

from deskhand_tool_loop import ActionResult, Final, run_task
from deskhand_tool_protocol import parse_tool_request


def uid(): return str(uuid.uuid4())


class ToolLoopTests(unittest.TestCase):
    def setUp(self):
        self.task, self.turn = uid(), uid()

    def proposal(self, name="filesystem.roots", arguments=None):
        value = {"version": 1, "type": "deskhand.actions", "task_id": self.task,
                 "turn_id": self.turn, "request_id": uid(), "actions": [
                     {"action_id": uid(), "name": name, "arguments": {} if arguments is None else arguments}]}
        return parse_tool_request(json.dumps(value), task_id=self.task, turn_id=self.turn)

    def run_loop(self, adapter, execute, stopped=lambda: False):
        return run_task(task_id=self.task, turn_id=self.turn, adapter=adapter, execute=execute, stopped=stopped)

    def test_result_returns_to_same_task(self):
        proposal = self.proposal()
        received = []
        def model(results):
            received.append(results)
            return proposal if len(received) == 1 else Final("done")
        result = self.run_loop(model, lambda request, action: ActionResult(action.action_id, "completed", "result"))
        self.assertEqual(result.text, "done")
        self.assertEqual(received[1][0].output, "result")

    def test_failed_action_cannot_be_reported_as_completed_task(self):
        proposal = self.proposal()
        replies = iter([proposal, Final("Done")])
        result = self.run_loop(lambda _: next(replies), lambda request, action: ActionResult(action.action_id, "failed", "denied"))
        self.assertEqual(result.outcome, "failed")
        self.assertIn("did not complete successfully", result.text)

    def test_model_can_recover_after_failed_action(self):
        replies = iter([self.proposal(), self.proposal("window.list"), Final("Recovered")])
        statuses = iter(["failed", "completed"])
        result = self.run_loop(lambda _: next(replies), lambda request, action: ActionResult(action.action_id, next(statuses)))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.text, "Recovered")

    def test_uncertain_is_returned_to_model_and_allows_observation_only(self):
        first, observe = self.proposal(), self.proposal("window.list")
        calls, received = [], []
        def model(results):
            received.append(results)
            return first if len(received) == 1 else observe if len(received) == 2 else Final("Inspection complete")
        def execute(request, action):
            calls.append(action.name)
            return ActionResult(action.action_id, "uncertain" if len(calls) == 1 else "completed")
        result = self.run_loop(model, execute)
        self.assertEqual(received[1][0].status, "uncertain")
        self.assertIn("unconfirmed", result.text)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(calls, ["filesystem.roots", "window.list"])

    def test_uncertain_blocks_model_retry_even_with_new_ids(self):
        first = self.proposal("filesystem.mkdir", {"folder_token": "token", "name": "new"})
        retry = self.proposal("filesystem.mkdir", {"folder_token": "token", "name": "new"})
        calls, model_calls = [], []
        def model(results):
            model_calls.append(results)
            return first if len(model_calls) == 1 else retry
        result = self.run_loop(model, lambda request, action: calls.append(action.name) or ActionResult(action.action_id, "uncertain"))
        self.assertEqual(calls, ["filesystem.mkdir"])
        self.assertEqual(result.outcome, "failed")
        self.assertIn("unconfirmed", result.text)

    def test_uncertain_batch_never_dispatches_following_actions(self):
        value = {"version": 1, "type": "deskhand.actions", "task_id": self.task, "turn_id": self.turn,
                 "request_id": uid(), "actions": [
                     {"action_id": uid(), "name": "filesystem.mkdir", "arguments": {"folder_token": "token", "name": "one"}},
                     {"action_id": uid(), "name": "filesystem.mkdir", "arguments": {"folder_token": "token", "name": "two"}}]}
        proposal = parse_tool_request(json.dumps(value), task_id=self.task, turn_id=self.turn)
        calls = []
        result = self.run_loop(lambda _: proposal, lambda request, action: calls.append(action.name) or ActionResult(action.action_id, "uncertain"))
        self.assertEqual(calls, ["filesystem.mkdir"])
        self.assertEqual(result.outcome, "failed")

    def test_failed_or_rejected_batch_never_dispatches_following_actions(self):
        for status in ("failed", "rejected"):
            with self.subTest(status=status):
                value = {"version": 1, "type": "deskhand.actions", "task_id": self.task, "turn_id": self.turn,
                         "request_id": uid(), "actions": [
                             {"action_id": uid(), "name": "filesystem.mkdir", "arguments": {"folder_token": "token", "name": "one"}},
                             {"action_id": uid(), "name": "filesystem.mkdir", "arguments": {"folder_token": "token", "name": "two"}}]}
                proposal = parse_tool_request(json.dumps(value), task_id=self.task, turn_id=self.turn)
                calls = []
                result = self.run_loop(lambda _: proposal, lambda request, action: calls.append(action.action_id) or ActionResult(action.action_id, status))
                self.assertEqual(len(calls), 1)
                self.assertEqual(result.outcome, "failed")

    def test_duplicate_action_and_stopped_task_do_not_execute(self):
        proposal, calls = self.proposal(), []
        with self.assertRaises(ValueError):
            self.run_loop(lambda _: proposal, lambda r, a: calls.append(a.action_id) or ActionResult(a.action_id, "completed"))
        self.assertEqual(len(calls), 1)
        stopped = self.run_loop(lambda _: self.fail("model called"), lambda *_: None, lambda: True)
        self.assertEqual(stopped.text, "Task stopped.")
        self.assertEqual(stopped.outcome, "cancelled")

    def test_long_read_only_task_can_exceed_previous_sixteen_round_limit(self):
        calls = []
        def model(results):
            if len(calls) == 40:
                return Final("Forty observations complete")
            return self.proposal("window.list")
        def execute(request, action):
            calls.append(action.action_id)
            return ActionResult(action.action_id, "completed", "observed")
        result = self.run_loop(model, execute)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(len(calls), 40)
        self.assertEqual(len(set(calls)), 40)

    def test_stop_during_long_task_blocks_subsequent_actions(self):
        calls = []
        def execute(request, action):
            calls.append(action.action_id)
            return ActionResult(action.action_id, "completed")
        result = self.run_loop(lambda _: self.proposal("window.list"), execute,
                               stopped=lambda: len(calls) >= 20)
        self.assertEqual(result.outcome, "cancelled")
        self.assertEqual(len(calls), 20)

    def test_long_task_still_has_a_finite_budget(self):
        calls = []
        result = self.run_loop(lambda _: self.proposal("window.list"),
                               lambda request, action: calls.append(action.action_id) or ActionResult(action.action_id, "completed"))
        self.assertEqual(result.outcome, "failed")
        self.assertIn("action limit", result.text)
        self.assertEqual(len(calls), 64)


if __name__ == "__main__": unittest.main()
