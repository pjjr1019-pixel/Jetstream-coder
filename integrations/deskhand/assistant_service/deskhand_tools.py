"""Missing computer-use tools, v100.008 (Batch 3, smarts #9).

Validation + grant families for the tools the audit found missing.
`screen.scroll` already exists in deskhand_tool_protocol and is NOT
duplicated here — it is listed only so the family map is complete.

New tools:
- window.wait_for — host-side poll for a control/window/text, bounded
  30s max. The AI must not busy-poll in chat; the host polls.
- clipboard.get / clipboard.set — clipboard is SENSITIVE and gets its
  own grant family ("clipboard"). Contents are never journalled: use
  redacted_summary() for any log line (length only).
- filesystem.search — indexed search (see deskhand_index); query only,
  no contents in results.
- app.launch_by_name — name -> CIM launch via the host-owned app map.
  Unknown names error; never fall back to a guessed exe path.
- screenshot.monitor — pick which monitor to capture (0 = primary,
  the default). The C# side is ScreenCapturer.Bounds (primary-only;
  BoundsFor() was removed in 100.030).

Parsing never executes anything. The host implements each tool and
enforces its family's grant + STOP + approvals.
"""
from __future__ import annotations

__version__ = "100.008"

NEW_TOOLS = frozenset({"window.wait_for", "clipboard.get", "clipboard.set",
                       "filesystem.search", "app.launch_by_name",
                       "screenshot.monitor"})

ACTION_FAMILY = {
    "window.wait_for": "window",
    "clipboard.get": "clipboard",
    "clipboard.set": "clipboard",
    "filesystem.search": "filesystem",
    "app.launch_by_name": "filesystem",
    "screenshot.monitor": "window",
    "screen.scroll": "pointer",
}


def _string(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid string")
    return value


def parse_new_tool(name, arguments):
    """Validate one new-tool call. Returns the normalized arguments dict.
    Raises ValueError on any defect. Exact fields, no extras."""
    if name not in NEW_TOOLS:
        raise ValueError("not a new Batch 3 tool")
    if not isinstance(arguments, dict):
        raise ValueError("invalid arguments")
    if name == "window.wait_for":
        if set(arguments) != {"timeout_secs"} | ({k for k in arguments if k in {"window_token", "text", "control_name"}}):
            raise ValueError("invalid wait_for fields")
        # 100.030: exactly ONE target, matching the single parser in
        # deskhand_tool_protocol.py (do not maintain two validators).
        if sum(key in arguments for key in ("window_token", "text", "control_name")) != 1:
            raise ValueError("wait_for needs exactly one of window_token, text, or control_name")
        for key in ("window_token", "control_name"):
            if key in arguments:
                _string(arguments[key], 64 if key == "window_token" else 200)
        if "text" in arguments:
            _string(arguments["text"], 500)
        timeout = arguments["timeout_secs"]
        if type(timeout) is not int or not 1 <= timeout <= 30:
            raise ValueError("wait_for timeout must be 1..30 seconds")
    elif name == "clipboard.get":
        if set(arguments) != set():
            raise ValueError("clipboard.get takes no arguments")
    elif name == "clipboard.set":
        if set(arguments) != {"text"}:
            raise ValueError("invalid clipboard.set fields")
        if not isinstance(arguments["text"], str) or len(arguments["text"]) > 4096:
            raise ValueError("invalid clipboard text")
    elif name == "filesystem.search":
        if set(arguments) not in ({"query"}, {"query", "folder_token"}):
            raise ValueError("invalid filesystem.search fields")
        _string(arguments["query"], 200)
        if "folder_token" in arguments:
            _string(arguments["folder_token"], 64)
    elif name == "app.launch_by_name":
        if set(arguments) != {"name", "purpose"}:
            raise ValueError("invalid app.launch_by_name fields")
        _string(arguments["name"], 200)
        _string(arguments["purpose"], 500)
    elif name == "screenshot.monitor":
        if set(arguments) != {"monitor_index"}:
            raise ValueError("invalid screenshot.monitor fields")
        if type(arguments["monitor_index"]) is not int or not 0 <= arguments["monitor_index"] <= 15:
            raise ValueError("monitor_index must be 0..15 (0 = primary)")
    return dict(arguments)


def redacted_summary(name, arguments):
    """Log-safe summary. Clipboard contents are NEVER included — length
    only. Everything else is safe to log verbatim."""
    if name == "clipboard.set":
        text = arguments.get("text", "")
        return {"tool": name, "text_length": len(text)}
    if name == "clipboard.get":
        return {"tool": name}
    return {"tool": name, "arguments": dict(arguments)}
