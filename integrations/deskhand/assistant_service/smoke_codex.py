"""Opt-in live Codex smoke; writes only its supplied Deskhand test data folder."""
import argparse
import json
import uuid
from pathlib import Path
from deskhand_codex import catalog, CodexChat
from deskhand_dispatch import Request, Route

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    args = parser.parse_args()
    models = catalog()
    chosen = next((r for r in models if r.get("default")), models[0])
    route = Route(*(chosen[k] for k in ("assistant", "model", "account", "effort")))
    adapter = CodexChat(route, args.data)
    reply = adapter(Request(str(uuid.uuid4()), "Reply with exactly DESKHAND_CODEX_OK. Do not use any tools.", route))
    print(json.dumps({"route": vars(reply.route), "reply": reply.text}))
    if reply.text.strip() != "DESKHAND_CODEX_OK":
        raise SystemExit("Unexpected live smoke reply")
