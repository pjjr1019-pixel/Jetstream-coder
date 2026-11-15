"""Tiny task-difficulty router (Batch 2, v100.004).

Pure function, no I/O, no model calls: classify a chat text as "fast"
(safe for the fastest available route — local Ollama or a low-effort
Codex model) or "smart" (needs reasoning, tools, or multi-step computer
use — high-effort Codex or Muse).

Rules, in order:
- "fast" only for short, self-contained intents: greetings/ping, yes/no,
  status questions, and single-app opens ("open notepad"). Anything
  mentioning a file path, a multi-step sequence ("then", "and then",
  numbered steps), code, research, comparison, or a question needing
  reasoning is "smart".
- Uncertain -> "smart" (route UP on uncertainty; a mis-routed hard task
  gets a worse answer, a mis-routed easy task only costs latency).
- The router never changes grants, authority, or STOP state. It only
  suggests which already-configured route to use. The caller keeps the
  explicit UI override ("always use smart model") and never re-runs a
  completed turn to escalate it.
"""
from __future__ import annotations

import re

_FAST_PATTERNS = (
    re.compile(r"^(hi|hello|hey|ping|pong|yes|no|ok|okay|thanks|thank you)[.!?]*$", re.I),
    re.compile(r"^(what('s| is) (the )?(time|date|status)|are you (there|online|ready)|status)[?]*$", re.I),
    re.compile(r"^open (notepad|calculator|calc|chrome|edge|paint|settings)[.!?]*$", re.I),
    re.compile(r"^(show|what is) (the )?(version|changelog)[?]*$", re.I),
)

_SMART_MARKERS = (
    "then", "and then", "step", "first,", "second,", "1.", "2.",
    "write", "code", "script", "fix", "debug", "research", "compare",
    "versus", "vs.", "why", "how do i", "how to", "explain", "summar",
    "find", "search", "download", "install", "move", "delete", "copy",
    "edit", "create a", "make a", "build", "plan", "\\", "/", ".txt",
    ".pdf", ".docx", "c:\\", "documents", "desktop",
)


# B2-D M2: effort is a reasoning level, not a string to sort
# alphabetically ("high" < "low" < "medium" alphabetically, so the old
# sort picked the HIGHEST effort for the "lowest-effort" fast path).
# Rank it explicitly. Unknown/missing effort ranks as medium (1) for
# smart (neutral) and last (99) for fast (never prefer the unknown).
EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}


def _effort_rank(route, unknown):
    return EFFORT_RANK.get(route.get("effort") or "", unknown)


def classify(text: str) -> dict:
    """Return {"kind": "fast"|"smart", "reason": str}.

    Non-string / empty input is "smart": never route a malformed request
    to the fast path by default.
    """
    if not isinstance(text, str) or not text.strip():
        return {"kind": "smart", "reason": "empty or invalid text routes up"}
    stripped = text.strip()
    lowered = stripped.lower()
    if len(stripped) <= 120:
        for pattern in _FAST_PATTERNS:
            if pattern.match(stripped):
                return {"kind": "fast", "reason": "short trivial intent: %r" % stripped[:60]}
    for marker in _SMART_MARKERS:
        if marker in lowered:
            return {"kind": "smart", "reason": "needs reasoning/tools: contains %r" % marker}
    if len(stripped) > 120:
        return {"kind": "smart", "reason": "longer than a trivial intent"}
    return {"kind": "fast", "reason": "short question with no reasoning marker"}


def suggest_route(text: str, routes: list, always_smart: bool = False) -> dict:
    """Pick a route dict from a catalog list, or {"route": None, ...}.

    "fast" prefers a local route, then the lowest-effort Codex route;
    "smart" prefers the default/highest-effort Codex route, then Muse,
    then local. Returns the reason alongside, so the UI can show why a
    route was picked. Never invents a route: no match -> route None.
    """
    decision = classify(text)
    if always_smart:
        decision = {"kind": "smart", "reason": "always-smart override: " + decision["reason"]}
    if not routes:
        return {"route": None, "kind": decision["kind"], "reason": decision["reason"]}
    if decision["kind"] == "fast":
        for route in routes:
            if route.get("assistant") == "local":
                return {"route": route, "kind": "fast", "reason": decision["reason"]}
        codex = [r for r in routes if r.get("assistant") == "codex"]
        if codex:
            codex.sort(key=lambda r: _effort_rank(r, 99))
            return {"route": codex[0], "kind": "fast", "reason": decision["reason"] + "; lowest-effort Codex"}
    else:
        codex = [r for r in routes if r.get("assistant") == "codex"]
        if codex:
            codex.sort(key=lambda r: (not r.get("default", False), -_effort_rank(r, 1)))
            return {"route": codex[0], "kind": "smart", "reason": decision["reason"] + "; default Codex"}
        for route in routes:
            if route.get("assistant") == "muse":
                return {"route": route, "kind": "smart", "reason": decision["reason"] + "; Muse"}
        for route in routes:
            if route.get("assistant") == "local":
                return {"route": route, "kind": "smart", "reason": decision["reason"] + "; local fallback"}
    return {"route": routes[0], "kind": decision["kind"], "reason": decision["reason"] + "; first available"}
