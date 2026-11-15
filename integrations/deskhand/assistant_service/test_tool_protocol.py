import json
import unittest
import uuid

from deskhand_tool_protocol import parse_tool_request


def identifier(): return str(uuid.uuid4())


class ToolProtocolTests(unittest.TestCase):
    def setUp(self):
        self.task, self.turn = identifier(), identifier()
        self.value = {"version": 1, "type": "deskhand.actions", "task_id": self.task,
                      "turn_id": self.turn, "request_id": identifier(), "actions": [
                          {"action_id": identifier(), "name": "desktop.list", "arguments": {"folder_token": None}}]}

    def parse(self, value=None):
        return parse_tool_request(json.dumps(self.value if value is None else value), task_id=self.task, turn_id=self.turn)

    def test_valid_is_only_a_proposal(self):
        parsed = self.parse()
        self.assertEqual(parsed.task_id, self.task)
        self.assertEqual(parsed.actions[0].name, "desktop.list")

    def test_binding_and_schema_are_strict(self):
        self.value["turn_id"] = identifier()
        with self.assertRaises(ValueError): self.parse()
        self.value["turn_id"] = self.turn
        self.value["extra"] = True
        with self.assertRaises(ValueError): self.parse()

    def test_duplicate_fields_actions_and_unknown_action_rejected(self):
        raw = json.dumps(self.value).replace('"version": 1', '"version": 1, "version": 1')
        with self.assertRaises(ValueError): parse_tool_request(raw, task_id=self.task, turn_id=self.turn)
        self.value["actions"].append(dict(self.value["actions"][0]))
        with self.assertRaises(ValueError): self.parse()
        self.value["actions"] = [{"action_id": identifier(), "name": "shell.run", "arguments": {}}]
        with self.assertRaises(ValueError): self.parse()

    def test_prose_and_oversize_are_not_actions(self):
        for raw in ("open calculator", " " * 32769, '{"version": NaN}'):
            with self.assertRaises(ValueError): parse_tool_request(raw, task_id=self.task, turn_id=self.turn)

    def test_shortcuts_are_explicit_and_strictly_validated(self):
        self.value["actions"] = [{"action_id": identifier(), "name": "control.key", "arguments": {"control_token": "token", "purpose": "Select text", "key": "ctrl+a"}}]
        self.assertEqual(self.parse().actions[0].name, "control.key")
        for invalid in ("alt+f4", "CTRL+A", ["ctrl+a"], None):
            self.value["actions"][0]["arguments"]["key"] = invalid
            with self.subTest(key=invalid), self.assertRaises(ValueError): self.parse()

    def test_scroll_steps_are_bounded(self):
        self.value["actions"] = [{"action_id": identifier(), "name": "screen.scroll", "arguments": {"screenshot_token": "token", "x": 0.5, "y": 0.5, "steps": -2, "purpose": "Scroll"}}]
        self.assertEqual(self.parse().actions[0].name, "screen.scroll")
        for invalid in (0, -4, 4, 1.5, True, "1"):
            self.value["actions"][0]["arguments"]["steps"] = invalid
            with self.subTest(steps=invalid), self.assertRaises(ValueError): self.parse()

    def test_double_click_requires_fresh_token_and_normalized_coordinates(self):
        self.value["actions"] = [{"action_id": identifier(), "name": "screen.double_click", "arguments": {"screenshot_token": "token", "x": 0.5, "y": 0.5, "purpose": "Open fixture"}}]
        self.assertEqual(self.parse().actions[0].name, "screen.double_click")
        for invalid in (-0.1, 1.1, True, "0.5"):
            self.value["actions"][0]["arguments"]["x"] = invalid
            with self.subTest(x=invalid), self.assertRaises(ValueError): self.parse()

    def test_drag_validates_both_endpoints(self):
        self.value["actions"] = [{"action_id": identifier(), "name": "screen.drag", "arguments": {"screenshot_token": "token", "x": 0.1, "y": 0.2, "to_x": 0.8, "to_y": 0.9, "purpose": "Drag fixture"}}]
        self.assertEqual(self.parse().actions[0].name, "screen.drag")
        for invalid in (-0.1, 1.1, True, "0.5"):
            self.value["actions"][0]["arguments"]["to_x"] = invalid
            with self.subTest(to_x=invalid), self.assertRaises(ValueError): self.parse()

    def test_folder_creation_schema_is_exact(self):
        self.value["actions"] = [{"action_id": identifier(), "name": "filesystem.mkdir", "arguments": {"folder_token": "token", "name": "New folder"}}]
        self.assertEqual(self.parse().actions[0].name, "filesystem.mkdir")
        self.value["actions"][0]["arguments"]["extra"] = True
        with self.assertRaises(ValueError): self.parse()

    def test_chunked_read_rejects_invalid_offsets(self):
        self.value["actions"] = [{"action_id": identifier(), "name": "filesystem.read_chunk", "arguments": {"item_token": "token", "offset": 0}}]
        self.assertEqual(self.parse().actions[0].name, "filesystem.read_chunk")
        for invalid in (-1, True, 1.5, 9223372036854775808):
            self.value["actions"][0]["arguments"]["offset"] = invalid
            with self.subTest(offset=invalid), self.assertRaises(ValueError): self.parse()

    def test_command_and_file_change_schemas_are_strict(self):
        for name, arguments in (
            ("command.run", {"command": "echo fixture", "purpose": "test"}),
            ("filesystem.delete", {"item_token": "token", "purpose": "test"}),
            ("filesystem.rmdir", {"item_token": "token", "purpose": "test"}),
            ("filesystem.move_folder", {"item_token": "source", "folder_token": "target", "name": "new", "purpose": "test"}),
            ("filesystem.copy", {"item_token": "source", "folder_token": "target", "name": "new", "purpose": "test"}),
            ("filesystem.replace", {"item_token": "token", "purpose": "test", "content": "updated"}),
        ):
            with self.subTest(name=name):
                self.value["actions"] = [{"action_id": identifier(), "name": name, "arguments": arguments}]
                self.assertEqual(self.parse().actions[0].name, name)
                self.value["actions"][0]["arguments"] = {**arguments, "extra": True}
                with self.assertRaises(ValueError): self.parse()
        self.value["actions"] = [{"action_id": identifier(), "name": "filesystem.replace", "arguments": {"item_token": "token", "purpose": "test", "content": "x" * 4097}}]
        with self.assertRaises(ValueError): self.parse()
        self.value["actions"][0]["arguments"]["content"] = "é" * 2048
        self.assertEqual(self.parse().actions[0].name, "filesystem.replace")
        self.value["actions"][0]["arguments"]["content"] = "é" * 2049
        with self.assertRaises(ValueError): self.parse()


if __name__ == "__main__": unittest.main()
