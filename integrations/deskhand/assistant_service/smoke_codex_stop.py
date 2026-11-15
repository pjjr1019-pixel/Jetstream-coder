"""Opt-in live interruption check using a separate Deskhand test conversation."""
import argparse
import threading
import uuid
from pathlib import Path

from deskhand_codex import CodexChat
from deskhand_dispatch import Request, Route, StopConfirmed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--delay", type=float, default=5)
    args = parser.parse_args()
    route = Route("codex", args.model, "default", "low")
    adapter = CodexChat(route, args.data)
    timer = threading.Timer(args.delay, adapter.request_stop)
    timer.start()
    try:
        try:
            adapter(Request(str(uuid.uuid4()),
                "For a cancellation test, write a detailed essay about the history of mathematics. Do not use tools.", route))
        except StopConfirmed as result:
            print("PASS: " + str(result))
        else:
            raise SystemExit("UNCONFIRMED: Codex completed before interruption was confirmed")
    finally:
        timer.cancel()
