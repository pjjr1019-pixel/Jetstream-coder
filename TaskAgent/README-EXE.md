# Laptop Agent v2 — Windows EXE build

Yes, you can run it as an EXE you double-click. One honest limit
first: **I can't hand you a ready-made Windows EXE from my Linux VM.**
PyInstaller doesn't cross-compile — a Windows EXE has to be built
on Windows. So this kit builds it on your laptop, once, with one
double-click. After that you just double-click the EXE.

## What you get

- `LaptopAgentV2.exe` — single file, console window. Same agent as
  `laptop_agent_v2.py`: HTTP on 8899 + bridge on
  `ws://<tailscale-ip>:8765/v1/bridge`, same token, same log, same
  master flag. No Python needed to *run* it, only to *build* it.
- Closing the EXE window (or Ctrl+C) kills both transports instantly.
- The EXE is **unsigned** because you built it yourself on your own
  laptop. Windows SmartScreen will warn on first run — that's normal
  for a self-built EXE. Click "More info" then "Run anyway".
  Don't download an EXE for this from anywhere else.

## Build it (once, on the throwaway laptop)

1. Install Python 3.10+ from python.org — tick **"Add python.exe
   to PATH"**. Install Tailscale and sign in (`tailscale ip` gives
   you the laptop's Tailscale IP).
2. Unzip this package to a folder, e.g. `C:\agent\`.
3. Double-click `build_exe.bat`. It installs PyInstaller +
   websockets (+ mss/pillow for screenshots) and builds
   `dist\LaptopAgentV2.exe`. Takes a minute or two.
4. Copy `dist\LaptopAgentV2.exe` into your run folder
   (e.g. `C:\agent\`) — **with** `agent_config.json` beside it.
   The EXE looks for `agent_config.json` next to itself.

## Configure, then run

Copy `agent_config.example.json` to `agent_config.json` and edit it:

- `"token"`: a long random string (never reuse one from anywhere else)
- `"host"`: the laptop's Tailscale IP from `tailscale ip`
- `"remote_control_enabled": true` — default is false on purpose;
  without this, every control op refuses.

Then double-click `LaptopAgentV2.exe`. You should see:

```
HTTP listening on <tailscale-ip>:8899
BRIDGE listening on ws://<tailscale-ip>:8765/v1/bridge
```

If it exits immediately, read the console — the usual causes are
"config not found" (agent_config.json isn't beside the EXE),
"set a real token" (token is still CHANGE-ME), or "host 0.0.0.0
is refused" (host must be the Tailscale IP or 127.0.0.1).

## After it runs

Send Jett the Tailscale IP — and the token **securely**
(secure vault / secure page, never pasted in chat). Jett then
verifies live: `/health`, bridge `auth_ok`, `ping`→`pong`,
`get_status`/`get_capabilities`, one bridge `exec`, one
files search/read/write round-trip, and one real screenshot.

## If you don't want to build

You don't have to. `python laptop_agent_v2.py` (the script in this
same zip) does exactly the same thing — the EXE is just a packaged
version of it for double-click running. The Python route needs
Python installed; the EXE route needs it only once, to build.

## What the EXE does NOT do

- No mouse/keyboard or window control — exec + files + screenshot only.
- No access to your main PC — throwaway laptop only.
- No lock-screen auto-off — closing the window is the kill-switch.
- No `submit_task` / chat — `get_capabilities` says so honestly.

## Files in this kit

- `laptop_agent_v2.py`, `README.md`, `agent_config.example.json` — v2, unchanged
- `laptop_agent_v2.spec` — PyInstaller spec (onefile, console)
- `build_exe.bat` — one-click Windows build
- `requirements-exe.txt` — websockets, mss, pillow
- `README-EXE.md` — this file

Tested in the VM: the spec parses, the script compiles, and the
.bat was checked by reading (no Windows machine in the VM to run
the actual PyInstaller build on — first real EXE build is step 3
above, on the laptop).
