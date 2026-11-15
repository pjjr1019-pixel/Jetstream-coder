# Laptop Test Agent v2 — HTTP v1 + Jett's Bridge v1

Remote-control agent for a **throwaway test laptop**. Lets Jett run
shell commands, search/read/write files, and grab screenshots — over
the original v1 HTTP API **and** over Jett's own coded bridge
(PROTOCOL.md v1, WebSocket). Both transports call the same code, use
the same token, write to the same log, and obey the same master flag.

TEST BOX ONLY. Do not run on a machine you care about.

## What changed vs v1

| | v1 | v2 |
|---|---|---|
| HTTP API on 8899 (`/exec`, `/files/*`, `/screenshot`, `/health`) | yes | yes, unchanged |
| Bridge on `ws://<tailscale-ip>:8765/v1/bridge` | no | yes — PROTOCOL.md v1 |
| Auth | Bearer token per HTTP request | same token, also as first WebSocket message (`auth` → `auth_ok` / `auth_fail`) |
| Master flag `remote_control_enabled` | no | yes — default **false**, enforced server-side on both transports |
| Screenshot size rule | thumbnail 640 | same, plus PNG kept under 300 KiB (edge steps down until it fits) |
| Duplicate requests | could re-run | same message `id` returns the same result, never re-runs |
| Oversize messages | HTTP 1 MiB cap | plus bridge 5 MiB cap |
| Unknown bridge types | n/a | ignored per protocol; known-but-unsupported types (`submit_task`, `chat`, …) get an honest `error`, not silence |

The bridge is **not** a second agent. It is the v1 agent with a
second door that speaks Jett's bridge language, so Jett can use the
same client (`jett_harness_client.py`) it already uses for the MIND
harness. One implementation of `exec` / `files` / `screenshot`,
reached two ways.

## Setup (on the laptop)

```bat
:: 1. Install Python 3.10+ from python.org (check "Add to PATH")
:: 2. Install Tailscale from tailscale.com/download and sign in
::    (run `tailscale ip` to get the laptop's Tailscale IP)
:: 3. Copy these files to a folder, e.g. C:\agent\
:: 4. Configure:
copy agent_config.example.json agent_config.json
::    edit agent_config.json:
::      "token": a long random string (never reuse one from anywhere else)
::      "host": the laptop's Tailscale IP from step 2
::      "remote_control_enabled": true   <-- explicit opt-in, see below
:: 5. Install dependencies:
pip install websockets
::    Optional, for screenshots only:
pip install mss pillow
:: 6. Run:
python laptop_agent_v2.py
```

Keep it running while you want Jett to have access. Close the window
(or Ctrl+C) to revoke access instantly — both transports die together.

## The master flag — read this

`remote_control_enabled` defaults to **false** on purpose. With it
off, the agent still answers `/health`, `ping`, `get_status` and
`get_capabilities` (so Jett can see the flag is off and *why* an
action was refused), but every `exec` / `files.*` / screenshot —
HTTP **and** bridge — refuses with `403` / `error`:
"remote control is off (remote_control_enabled=false)".

Turning it on is a deliberate act in the config file, so a stray run
of a copied folder can never grant access by accident. There is no
remote way to flip it on: edit the config on the laptop and restart
the agent. There is also no lock-screen auto-off in this build — the
kill-switch is closing the window.

## How Jett connects

Over Tailscale only. Never expose ports 8899 / 8765 publicly.

```bash
# --- HTTP v1 (unchanged) ---
curl -H "Authorization: Bearer <token>" \
     -X POST http://<tailscale-ip>:8899/exec \
     -d '{"command":"whoami"}'

# --- Bridge v1 (new) ---
# WebSocket to ws://<tailscale-ip>:8765/v1/bridge
# 1st message: {"type": "auth", "token": "<token>"}
#   -> {"type": "auth_ok", "server": "laptop-agent-v2", "protocol": 1}
# Then envelope messages, e.g.:
#   {"id": "<uuid>", "type": "ping", "ts": 1792000000,
#    "in_reply_to": null, "payload": {}}
#   -> {"id": "<uuid>", "type": "pong", "ts": ...,
#       "in_reply_to": "<ping id>", "payload": {}}
#   {"id": "<uuid>", "type": "exec", "ts": ...,
#    "payload": {"command": "whoami"}}
#   -> {"type": "exec_result", "in_reply_to": "<id>",
#       "payload": {"stdout": "...", "stderr": "", "exit_code": 0}}
#   {"id": "<uuid>", "type": "files.search", "ts": ...,
#    "payload": {"root": "C:\\test", "pattern": "*.py"}}
#   {"id": "<uuid>", "type": "get_screenshot", "ts": ...,
#    "payload": {"max_edge": 640}}
```

Or point Jett's existing bridge client at it — it already speaks this
protocol (`jett_harness_client.py`, config `url` =
`ws://<tailscale-ip>:8765/v1/bridge`, same `token`).

## Notes

- One token for both transports; it is never written to `agent.log`.
- Every bridge action is logged with its message `id` and `type`.
- One bridge connection at a time: a newer authenticated connection
  closes the older one.
- Messages over 5 MiB are rejected; duplicate message `id`s replay
  the stored result instead of re-running (an `exec` never runs twice
  for one `id`).
- Ambiguous requests get an `error` reply, never a guess.
- `submit_task` and chat are NOT implemented — `get_capabilities`
  says so honestly, and those requests get an `error`.
- Optional `allowed_roots` in the config jails file operations to
  listed directories. Empty (default) = whole machine, per Phil's
  choice for the throwaway test box.
- `host` must be the Tailscale IP or `127.0.0.1`. `0.0.0.0` is
  refused at startup.
