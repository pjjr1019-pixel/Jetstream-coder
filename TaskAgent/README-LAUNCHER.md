# Laptop Agent v3 — Portable GUI Launcher

One folder, one window, everything together. This replaces the
old flow of double-clicking an EXE and hoping the black window
stayed open long enough to read the error.

## What it is

`laptop_launcher.py` (tkinter only — Python standard library, no
extra packages) is a window with four tabs — **Status, Settings,
Log, Instructions** — that runs laptop-agent v2 for you.

* **Status** — a green/red light for Running/Stopped, the HTTP and
  bridge URLs, the health-check result, and the remote-control
  flag. Buttons: Start, Stop, Test connection, Build EXE,
  Copy Tailscale IP, Copy status for Jett, Open folder.
* **Settings** — edits `agent_config.json` beside the launcher:
  token (masked, with Show/Hide and a Generate button), host
  (with Auto-detect), HTTP + bridge ports, command timeout,
  allowed folders, and the remote-control ON/OFF switch.
  Everything is validated in plain language *before* it is saved
  or the agent starts.
* **Log** — the agent's live output, captured here. If the agent
  stops itself, the reason stays visible. The launcher window
  **never closes on an agent error**. Buttons: Clear log,
  Open agent.log, Open config.
* **Instructions** — the full step-by-step and troubleshooting,
  built in, so the answers are in the window when something
  goes wrong.

## The two problems this fixes

1. **The window closed on error.** The old EXE exited on a bad
   config and took the error message with it. The launcher starts
   the agent as a captured subprocess: its output goes to the Log
   tab, and if it exits the Status tab says so with the exit code.
2. **Files ended up split across folders** (EXE in `dist\`).
   Every path in the launcher is resolved from the launcher's own
   folder — never from the current directory — and the agent is
   started with an absolute `--config` path and the launcher
   folder as its working directory. The build script copies both
   EXEs back to the folder root. Keep everything in one folder
   and it cannot split.

## Recommendations built in (Jett's)

* **One-click Start/Stop as the kill-switch.** Closing the
  launcher stops the agent too. No window, no access.
* **Token generator.** One click makes a 43-character random
  token. The token is masked in Settings, never written to any
  log, and never included in "Copy status for Jett".
* **Tailscale IP auto-detect.** One click runs `tailscale ip`
  and fills in the 100.x.x.x address.
* **Config validation before start.** Token empty / still
  CHANGE-ME / too short, host 0.0.0.0 or empty or not an IP,
  bad or duplicate ports, bad timeout — all refused with a
  plain-language message, before the agent ever starts.
* **Live log viewer.** The agent's own `agent.log` is in the
  same folder, and the Log tab shows output as it happens.
* **Token-free "Copy status for Jett".** One click copies the
  IP, ports, flag, running state and health result — never the
  token. The token still goes to Jett securely, separately.
* **Confirm before turning remote control ON.** The switch asks
  first, in plain words, what ON means. Turning it OFF needs no
  confirmation (safe direction).

## How to use it (short version)

1. Unzip this package to ONE folder, e.g. `C:\agent\`.
2. Install Python 3.10+ (tick "Add python.exe to PATH") and
   Tailscale (install + sign in).
3. Double-click `LaptopLauncher.exe` — or, before building,
   `python laptop_launcher.py` (same window, no build needed).
4. Settings: Generate a token, Auto-detect the host, turn
   remote control ON (confirm), Save settings.
5. Status: Start. The light turns green; the Log shows both
   `HTTP listening` and `BRIDGE listening` lines.
6. Copy status for Jett, paste it to Jett. Send the token
   securely (secure vault / secure page) — never in chat.
7. Stop any time with Stop, or by closing the launcher.

Full steps + troubleshooting are in the Instructions tab and in
`START-HERE.txt`.

## Building the EXEs

On the PC, double-click `build_launcher.bat`. It builds both
`LaptopAgentV2.exe` and `LaptopLauncher.exe` and copies both to
the folder root. No ready-made Windows EXE is in the zip — a
Windows EXE has to be built on Windows, and this is the one
command that does it. The launcher also has a "Build EXE" button
that runs the same script and streams the output into the Log tab.

You do **not** have to build anything to use the launcher:
`python laptop_launcher.py` opens the same window, and Start
runs the agent script directly if no agent EXE is present.

## What the agent does (unchanged from v2)

HTTP on 8899 + WebSocket bridge on 8765, same shared token,
`remote_control_enabled` default false, exec + files +
screenshot only. No mouse/keyboard, no window control, no
submit_task / chat. See `README.md` and
`laptop-agent-v3-TEST-REPORT.md`.

## Files in this package

* `laptop_launcher.py` — the GUI launcher (tkinter only)
* `launcher.spec` — PyInstaller spec (windowed) for the launcher
* `build_launcher.bat` — builds both EXEs, copies them here
* `laptop_agent_v2.py` — the agent, UNCHANGED from v2
* `laptop_agent_v2.spec`, `build_exe.bat` — agent-only build
  (kept working; `build_launcher.bat` is the new all-in-one)
* `run_agent.bat` — old console launcher (kept; the GUI
  replaces it for normal use)
* `agent_config.example.json`, `README.md`, `README-EXE.md`,
  `requirements-exe.txt`, `START-HERE.txt`
* `test_launcher.py` — the headless test suite (VM only)
* `README-LAUNCHER.md` — this file
