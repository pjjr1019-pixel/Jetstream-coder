"""Fused screenshot + UI-tree grounding, v100.008 (Batch 3, smarts #1).

One observation = one fresh screenshot + the UI Automation tree for the same
window, fused into a single payload with short-lived, task-scoped element
IDs. The AI addresses controls by ID; the host resolves ID -> coordinates
at execution time, so small layout shifts do not break grounding.

Safety rules (from the compact protocol + handoff SS5-SS7):
- Element IDs are UUIDv4, minted per observation, valid for ONE task only.
  They expire with the screenshot frame (15s TTL) and are never reused
  across tasks, never persisted, never trusted after expiry.
- resolve() re-validates that the element still exists in the live
  observation; a stale/expired/foreign ID is an error, never a guess.
- A failed resolution is reported as uncertain by the caller; it is never
  retried with guessed coordinates and never replayed.
- This module captures nothing and executes nothing. The host supplies the
  screenshot token and the tree; this module only fuses and registries them.
"""
from __future__ import annotations

import time
import uuid

__version__ = "100.008"

FRAME_TTL_SECS = 15
MAX_ELEMENTS = 256
ELEMENT_KINDS = frozenset({
    "button", "edit", "text", "checkbox", "radio", "combo", "list",
    "list_item", "menu", "menu_item", "tab", "link", "image", "window",
    "pane", "scrollbar", "slider", "tree_item", "unknown",
})


def _uuid4():
    return str(uuid.uuid4())


def _bbox(value):
    if not isinstance(value, dict) or set(value) != {"x_min", "y_min", "x_max", "y_max"}:
        raise ValueError("invalid bbox")
    for key in ("x_min", "y_min", "x_max", "y_max"):
        if type(value[key]) not in (int, float) or not 0 <= value[key] <= 1:
            raise ValueError("invalid bbox coordinate")
    if not value["x_min"] < value["x_max"] or not value["y_min"] < value["y_max"]:
        raise ValueError("invalid bbox range")
    return dict(value)


def _element(value):
    if not isinstance(value, dict) or set(value) != {"name", "kind", "bbox"}:
        raise ValueError("invalid element")
    if not isinstance(value["name"], str) or not value["name"].strip() or len(value["name"]) > 200:
        raise ValueError("invalid element name")
    if value["kind"] not in ELEMENT_KINDS:
        raise ValueError("unsupported element kind")
    return {"name": value["name"], "kind": value["kind"], "bbox": _bbox(value["bbox"])}


class GroundingRegistry:
    """One live observation per task. Registering a new observation for a
    task replaces the old one (mirrors AssistantScreenSession, which clears
    on Remember): there is exactly one fresh frame per task at a time."""

    def __init__(self):
        self._observations = {}

    def register(self, *, task_id, turn_id, screenshot_token, elements, now=None):
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("invalid task_id")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("invalid turn_id")
        if not isinstance(screenshot_token, str) or not screenshot_token.strip() or len(screenshot_token) > 64:
            raise ValueError("invalid screenshot_token")
        if not isinstance(elements, list) or not elements or len(elements) > MAX_ELEMENTS:
            raise ValueError("invalid element list")
        now = time.time() if now is None else now
        fused = []
        for raw in elements:
            item = _element(raw)
            bbox = item["bbox"]
            fused.append({
                "element_id": _uuid4(),
                "name": item["name"],
                "kind": item["kind"],
                "bbox": bbox,
                "center": {
                    "x": (bbox["x_min"] + bbox["x_max"]) / 2,
                    "y": (bbox["y_min"] + bbox["y_max"]) / 2,
                },
            })
        observation = {
            "task_id": task_id,
            "turn_id": turn_id,
            "screenshot_token": screenshot_token,
            "elements": fused,
            "element_count": len(fused),
            "expires_at": now + FRAME_TTL_SECS,
        }
        self._observations[task_id] = observation
        return observation

    def fuse(self, *, task_id, turn_id, screenshot_token, tree, now=None):
        """Fuse a host-supplied UI tree (list of element dicts) with a fresh
        screenshot token into one observation. `tree` is host-observed data,
        never model-asserted: the caller must pass the tree it actually got
        from window.inspect in this task."""
        return self.register(task_id=task_id, turn_id=turn_id,
                             screenshot_token=screenshot_token, elements=tree, now=now)

    def resolve(self, *, task_id, screenshot_token, element_id, now=None):
        """Resolve an element ID to normalized (x, y) centre coordinates.
        Raises ValueError on unknown task, wrong screenshot token, unknown
        element, or expired frame. The caller must re-observe on failure;
        it must not guess coordinates."""
        now = time.time() if now is None else now
        observation = self._observations.get(task_id)
        if observation is None:
            raise ValueError("no grounding observation for this task")
        if observation["screenshot_token"] != screenshot_token:
            raise ValueError("screenshot token does not match this observation")
        if now >= observation["expires_at"]:
            del self._observations[task_id]
            raise ValueError("grounding observation expired; re-observe with a fresh frame")
        for element in observation["elements"]:
            if element["element_id"] == element_id:
                return {"x": element["center"]["x"], "y": element["center"]["y"],
                        "name": element["name"], "kind": element["kind"]}
        raise ValueError("unknown element id for this task")

    def clear(self, task_id):
        self._observations.pop(task_id, None)
