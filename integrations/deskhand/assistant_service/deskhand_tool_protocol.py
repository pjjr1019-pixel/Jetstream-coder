"""Validated model-neutral action proposals; parsing never executes an action.

The authenticated Windows host remains responsible for task identity, grants,
STOP, approvals, durable reservation, execution, and reporting results.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass

MAX_BYTES = 32768
MAX_ACTIONS = 4

# D3 (100.030): the exact v1 key/chord allowlist. Every entry named
# explicitly; a chord not on this list is rejected, never executed.
# Must stay in lockstep with InputInjector.cs (Keys +
# AssistantShortcuts) and the AssistantAccessibility supported-key
# check — see tests/test_desktop_control.py::TestKeyLockstep.
_SUPPORTED_KEYS = frozenset({
    "enter", "backspace", "tab", "esc", "delete",
    "left", "up", "right", "down", "home", "end", "pageup", "pagedown",
    "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+s", "ctrl+f",
    "ctrl+shift+o", "alt+tab", "shift+tab",
    "ctrl+shift+left", "ctrl+shift+right", "ctrl+home", "ctrl+end",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _uuid(value):
    try:
        parsed = uuid.UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid UUIDv4") from None
    return value


def _string(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError("invalid string")
    return value


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("invalid fields")


@dataclass(frozen=True)
class ToolAction:
    action_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ToolRequest:
    task_id: str
    turn_id: str
    request_id: str
    actions: tuple[ToolAction, ...]


def parse_tool_request(raw: str, *, task_id: str, turn_id: str) -> ToolRequest:
    """Parse an entire model message; require exact active task and turn IDs.

    Accepted names define a first interoperable subset. Later action families
    must be explicitly added alongside host implementation and tests.
    """
    if not isinstance(raw, str):
        raise ValueError("invalid envelope")
    try:
        if len(raw.encode("utf-8", errors="strict")) > MAX_BYTES:
            raise ValueError("envelope too large")
        obj = json.loads(raw, object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid constant")))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("invalid JSON") from None
    _fields(obj, {"version", "type", "task_id", "turn_id", "request_id", "actions"})
    if type(obj["version"]) is not int or obj["version"] != 1 or obj["type"] != "deskhand.actions":
        raise ValueError("unsupported protocol")
    if _uuid(obj["task_id"]) != _uuid(task_id) or _uuid(obj["turn_id"]) != _uuid(turn_id):
        raise ValueError("wrong active task")
    request_id = _uuid(obj["request_id"])
    actions = obj["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise ValueError("invalid action count")
    parsed = []
    seen = set()
    for action in actions:
        _fields(action, {"action_id", "name", "arguments"})
        action_id = _uuid(action["action_id"])
        if action_id in seen:
            raise ValueError("duplicate action ID")
        seen.add(action_id)
        name, arguments = action["name"], action["arguments"]
        if not isinstance(name, str):
            raise ValueError("invalid action name")
        if name == "desktop.list":
            _fields(arguments, {"folder_token"})
            if arguments["folder_token"] is not None:
                _string(arguments["folder_token"], 64)
        elif name in {"desktop.open", "filesystem.read"}:
            _fields(arguments, {"item_token"})
            _string(arguments["item_token"], 64)
        elif name == "filesystem.read_chunk":
            _fields(arguments, {"item_token", "offset"})
            _string(arguments["item_token"], 64)
            if type(arguments["offset"]) is not int or not 0 <= arguments["offset"] <= 9223372036854775807:
                raise ValueError("invalid file offset")
        elif name == "filesystem.next":
            _fields(arguments, {"folder_token", "cursor"})
            _string(arguments["folder_token"], 64)
            _string(arguments["cursor"], 64)
        elif name == "filesystem.mkdir":
            _fields(arguments, {"folder_token", "name"})
            _string(arguments["folder_token"], 64)
            _string(arguments["name"], 200)
        elif name == "filesystem.create":
            _fields(arguments, {"folder_token", "name", "content"})
            _string(arguments["folder_token"], 64)
            _string(arguments["name"], 200)
            if not isinstance(arguments["content"], str) or len(arguments["content"].encode("utf-8")) > 32768:
                raise ValueError("invalid content")
        elif name == "filesystem.replace":
            _fields(arguments, {"item_token", "purpose", "content"})
            _string(arguments["item_token"], 64)
            _string(arguments["purpose"], 500)
            if not isinstance(arguments["content"], str) or len(arguments["content"].encode("utf-8")) > 4096:
                raise ValueError("invalid content")
        elif name in {"filesystem.copy", "filesystem.move", "filesystem.move_folder"}:
            _fields(arguments, {"item_token", "folder_token", "name", "purpose"})
            _string(arguments["item_token"], 64)
            _string(arguments["folder_token"], 64)
            _string(arguments["name"], 200)
            _string(arguments["purpose"], 500)
        elif name in {"filesystem.launch", "filesystem.delete", "filesystem.rmdir"}:
            _fields(arguments, {"item_token", "purpose"})
            _string(arguments["item_token"], 64)
            _string(arguments["purpose"], 500)
        elif name == "command.run":
            _fields(arguments, {"command", "purpose"})
            _string(arguments["command"], 4000)
            _string(arguments["purpose"], 500)
        elif name in {"window.activate", "window.inspect", "window.screenshot"}:
            _fields(arguments, {"window_token"})
            _string(arguments["window_token"], 64)
        elif name == "window.wait_for":
            targets = [key for key in ("window_token", "text", "control_name") if key in arguments]
            if set(arguments) != {"timeout_secs", *targets} or len(targets) != 1:
                raise ValueError("invalid wait_for fields")
            if "window_token" in arguments:
                _string(arguments["window_token"], 64)
            if "text" in arguments:
                _string(arguments["text"], 500)
            if "control_name" in arguments:
                _string(arguments["control_name"], 200)
            if type(arguments["timeout_secs"]) is not int or not 1 <= arguments["timeout_secs"] <= 30:
                raise ValueError("wait_for timeout must be 1..30 seconds")
        elif name == "window.close":
            _fields(arguments, {"window_token", "purpose"})
            _string(arguments["window_token"], 64)
            _string(arguments["purpose"], 500)
        elif name in {"screen.click", "screen.double_click", "screen.right_click", "screen.move", "screen.drag", "screen.scroll"}:
            _fields(arguments, {"screenshot_token", "x", "y", "purpose", "steps"} if name == "screen.scroll" else {"screenshot_token", "x", "y", "to_x", "to_y", "purpose"} if name == "screen.drag" else {"screenshot_token", "x", "y", "purpose"})
            _string(arguments["screenshot_token"], 64)
            _string(arguments["purpose"], 500)
            if any(type(arguments[k]) not in (int, float) or not math.isfinite(arguments[k]) or not 0 <= arguments[k] <= 1 for k in (("x", "y", "to_x", "to_y") if name == "screen.drag" else ("x", "y"))):
                raise ValueError("invalid click coordinates")
            if name == "screen.scroll" and (type(arguments["steps"]) is not int or arguments["steps"] not in {-3, -2, -1, 1, 2, 3}):
                raise ValueError("invalid scroll steps")
        elif name == "screenshot.monitor":
            # PARSER-ONLY (100.030): accepted and validated here; host
            # execution is deferred (no C# selector in this version).
            _fields(arguments, {"monitor_index"})
            if type(arguments["monitor_index"]) is not int or not 0 <= arguments["monitor_index"] <= 15:
                raise ValueError("monitor_index must be 0..15 (0 = primary)")
        elif name in {"control.invoke", "control.type", "control.key"}:
            _fields(arguments, {"control_token", "purpose", "text"} if name == "control.type" else {"control_token", "purpose", "key"} if name == "control.key" else {"control_token", "purpose"})
            _string(arguments["control_token"], 64)
            _string(arguments["purpose"], 500)
            if name == "control.type" and (not isinstance(arguments["text"], str) or len(arguments["text"]) > 512):
                raise ValueError("invalid text")
            if name == "control.key" and (not isinstance(arguments["key"], str) or arguments["key"] not in _SUPPORTED_KEYS):
                raise ValueError("unsupported key")
        elif name in {"control.key_down", "control.key_up"}:
            # D3 set, one key each. They act on the focused window, so
            # they take screenshot_token, NOT control_token.
            _fields(arguments, {"screenshot_token", "purpose", "key"})
            _string(arguments["screenshot_token"], 64)
            _string(arguments["purpose"], 500)
            if not isinstance(arguments["key"], str) or arguments["key"] not in _SUPPORTED_KEYS:
                raise ValueError("unsupported key")
        elif name in {"filesystem.roots", "window.list", "window.observe"}:
            _fields(arguments, set())
        else:
            raise ValueError("unsupported action")
        parsed.append(ToolAction(action_id, name, json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)))
    return ToolRequest(task_id, turn_id, request_id, tuple(parsed))
