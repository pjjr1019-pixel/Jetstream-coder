"""On-PC runner for the laptop-agent fallback op.

Invoked by the VM caller through the laptop agent's /exec (the VM
cannot call ChatService.handle() directly -- the service is a
JSON-lines process owned by the C# app). Shares the implementation
with the in-process ``task_agent_action`` op: same gates, same
journal, same dedup (see deskhand_task_agent_op.py).

Usage:
  python -u run_task_agent_fallback.py --data <service data dir>
      --command-file C:\\agent\\fallback_command.json
  --command-file may be replaced by --command '<json>' or a JSON
  document on stdin. The command is {"task_id": ..., "action":
  {"kind": ..., ...}, "gateway_state": {...}}.

Prints exactly one JSON line on stdout:
  {"ok": true, "result": {...}} on success, or
  {"ok": false, "error": "...", "error_kind": "..."} on refusal /
  failure, and exits 1. Nothing else is printed (no tokens, no
  image data). The bridge check here reads the bridge operator state
  on loopback (bridge_stop_allows); it fails closed when the bridge
  state is unreadable. STOP latch removed (Phil 2026-11-09): no STOP
  field is read.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deskhand_task_agent_op import (  # noqa: E402
    bridge_stop_allows,
    run_task_agent_action,
)


def _default_data_dir():
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return os.path.join(local, "Deskhand", "assistant")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=_default_data_dir())
    parser.add_argument("--command")
    parser.add_argument("--command-file")
    args = parser.parse_args()
    if not args.data:
        print(json.dumps({"ok": False, "error": "service data dir is required (--data)",
                          "error_kind": "ValueError"}))
        return 1
    if args.command_file:
        with open(args.command_file, "r", encoding="utf-8") as handle:
            command = json.load(handle)
    elif args.command:
        command = json.loads(args.command)
    else:
        command = json.load(sys.stdin)
    try:
        result = run_task_agent_action(
            args.data, command,
            host_allows=lambda: bridge_stop_allows(args.data))
    except Exception as error:  # refusal or execution failure
        print(json.dumps({"ok": False, "error": str(error)[:500],
                          "error_kind": type(error).__name__},
                         ensure_ascii=True))
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
