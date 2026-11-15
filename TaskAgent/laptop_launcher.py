#!/usr/bin/env python3
"""
laptop_launcher.py — Portable GUI launcher for laptop-agent v2.

One folder, everything together. Double-click LaptopLauncher.exe
(or run `python laptop_launcher.py`) and use the buttons — no
command window, no hunting for files in dist\\, no window that
closes on an error and takes the error message with it.

What it fixes (all seen already on Phil's PC)
---------------------------------------------
* The agent window closed on startup errors, so the error could
  never be read. The launcher captures the agent's output live
  into its Log tab, and the launcher window NEVER closes on an
  agent error — the error stays visible in the GUI.
* The EXE, the config and the launcher ended up split across
  folders (EXE in dist\\). Every path here is resolved from the
  launcher's own folder, never from the current working directory,
  and the agent is always started with an absolute --config path
  and the launcher folder as its working directory.
* Settings lived in a JSON file Phil had to edit by hand. The
  Settings tab edits agent_config.json with plain-language
  validation BEFORE anything starts (token empty / still
  CHANGE-ME / too short, host 0.0.0.0 or empty, bad ports).

Scope (same as laptop-agent v2 — TEST BOX ONLY)
------------------------------------------------
exec + files + screenshot only. No mouse/keyboard or window
control, no submit_task / chat. The master flag
remote_control_enabled defaults to FALSE and is enforced by the
agent on both transports; turning it ON in this launcher asks
for confirmation first. Closing the launcher stops the agent:
that is the kill-switch.

Tkinter only (Python standard library). No extra dependencies.
Works as a script (`python laptop_launcher.py`) and as a
PyInstaller windowed EXE (see launcher.spec / build_launcher.bat).

The token is NEVER shown in full anywhere except the masked
Settings field, NEVER written to any log, and NEVER included in
the "Copy status for Jett" text.
"""

import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_NAME = "Laptop Agent Launcher"
AGENT_SCRIPT = "laptop_agent_v2.py"
AGENT_EXE = "LaptopAgentV2.exe"
CONFIG_NAME = "agent_config.json"
EXAMPLE_NAME = "agent_config.example.json"
LOG_NAME = "agent.log"
DEFAULT_HTTP_PORT = 8899
DEFAULT_BRIDGE_PORT = 8765
MIN_TOKEN_LEN = 16

# Keys in the same order as agent_config.example.json, so a saved
# file stays readable next to the example.
DEFAULT_CONFIG = {
    "allowed_roots": [],
    "bridge_path": "/v1/bridge",
    "bridge_port": DEFAULT_BRIDGE_PORT,
    "host": "127.0.0.1",
    "log_file": LOG_NAME,
    "port": DEFAULT_HTTP_PORT,
    "remote_control_enabled": False,
    "timeout_secs": 120,
    "token": "CHANGE-ME",
}

_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


# ---------------------------------------------------------------- paths

def base_dir():
    """Folder the launcher lives in — never the current directory.

    Frozen (PyInstaller onefile/windowed): the folder holding the
    launcher EXE. Script: the folder holding this .py file.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_path(base=None):
    return Path(base or base_dir()) / CONFIG_NAME


def example_path(base=None):
    return Path(base or base_dir()) / EXAMPLE_NAME


def agent_exe_path(base=None):
    return Path(base or base_dir()) / AGENT_EXE


def agent_script_path(base=None):
    return Path(base or base_dir()) / AGENT_SCRIPT


def log_path(base=None):
    return Path(base or base_dir()) / LOG_NAME


# ---------------------------------------------------------------- config

def load_config(base=None):
    """Load agent_config.json beside the launcher.

    Returns (config_dict, error_or_None). Missing file -> defaults
    plus an error saying so. Corrupt JSON -> defaults plus the
    parse error. Unknown keys are kept (forward-compatible).
    """
    path = config_path(base)
    cfg = dict(DEFAULT_CONFIG)
    cfg["allowed_roots"] = list(DEFAULT_CONFIG["allowed_roots"])
    if not path.exists():
        return cfg, f"config not found: {path.name} (not created yet)"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return cfg, f"config is not valid JSON: {exc}"
    if not isinstance(raw, dict):
        return cfg, "config is not a JSON object (expected { ... })"
    cfg.update(raw)
    return cfg, None


def save_config(cfg, base=None):
    """Write cfg to agent_config.json beside the launcher."""
    path = config_path(base)
    ordered = {key: cfg.get(key, DEFAULT_CONFIG[key])
               for key in DEFAULT_CONFIG}
    for key, value in cfg.items():  # keep any unknown keys too
        ordered.setdefault(key, value)
    path.write_text(json.dumps(ordered, indent=2) + "\n",
                    encoding="utf-8")
    return path


def parse_allowed_roots(text):
    """'C:\\a, C:\\b' or one-per-line -> list of paths."""
    if isinstance(text, list):
        return [str(item).strip() for item in text if str(item).strip()]
    parts = re.split(r"[,\n]+", str(text or ""))
    return [part.strip() for part in parts if part.strip()]


def _valid_ipv4(host):
    match = _IPV4_RE.match(str(host or "").strip())
    if not match:
        return False
    return all(0 <= int(group) <= 255 for group in match.groups())


def _port_error(value, name):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return f"{name} must be a whole number, got {value!r}"
    if not 1 <= port <= 65535:
        return f"{name} must be between 1 and 65535, got {port}"
    return None


def validate_config(cfg):
    """Plain-language errors. Empty list = good to save and start."""
    errors = []
    token = str(cfg.get("token", "") or "")
    if not token:
        errors.append("Token is empty — click Generate, or paste a long "
                      "random token.")
    elif token == "CHANGE-ME":
        errors.append("Token is still CHANGE-ME — click Generate to make "
                      "a real one. The agent refuses to start with the "
                      "placeholder.")
    elif len(token) < MIN_TOKEN_LEN:
        errors.append(f"Token is too short ({len(token)} characters) — "
                      f"use at least {MIN_TOKEN_LEN}. Click Generate.")
    host = str(cfg.get("host", "") or "").strip()
    if not host:
        errors.append("Host is empty — click Auto-detect, or type the "
                      "PC's Tailscale IP (a 100.x.x.x number).")
    elif host == "0.0.0.0":
        errors.append("Host 0.0.0.0 is refused — the agent never binds "
                      "all interfaces. Use the Tailscale IP or 127.0.0.1.")
    elif not _valid_ipv4(host):
        errors.append(f"Host {host!r} is not a valid IP address — use "
                      "the Tailscale IP (100.x.x.x) or 127.0.0.1.")
    for value, name in ((cfg.get("port"), "HTTP port"),
                        (cfg.get("bridge_port"), "Bridge port")):
        err = _port_error(value, name)
        if err:
            errors.append(err)
    try:
        http_port = int(str(cfg.get("port")).strip())
        bridge_port = int(str(cfg.get("bridge_port")).strip())
        if http_port == bridge_port:
            errors.append("HTTP port and bridge port must be different — "
                          f"both are {http_port}.")
    except (TypeError, ValueError):
        pass  # already reported above
    try:
        timeout = int(str(cfg.get("timeout_secs")).strip())
        if not 5 <= timeout <= 3600:
            errors.append("Command timeout must be between 5 and 3600 "
                          f"seconds, got {timeout}.")
    except (TypeError, ValueError):
        errors.append("Command timeout must be a whole number of seconds, "
                      f"got {cfg.get('timeout_secs')!r}.")
    return errors


def generate_token():
    """A long random token: 43 URL-safe characters (letters, digits,
    '-' and '_'). Never logged, never shown in full outside the
    masked Settings field."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------- network

def detect_tailscale_ip(timeout=5):
    """Run `tailscale ip` and return the first 100.x.x.x address.

    Returns None when Tailscale is not installed, not running, or
    gives no 100.x address — never raises.
    """
    for args in (["tailscale", "ip", "-4"], ["tailscale", "ip"]):
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            continue
        for line in (proc.stdout or "").splitlines():
            candidate = line.strip()
            if candidate.startswith("100.") and _valid_ipv4(candidate):
                return candidate
    return None


def check_health(host, port, timeout=5):
    """GET http://host:port/health (no token needed).

    Returns (ok, message). ok=True only on HTTP 200 with
    {"ok": true}. The message is plain language for the Status tab.
    """
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, f"health check got HTTP {exc.code} — the agent " \
                      "answered, but not with a healthy reply"
    except urllib.error.URLError as exc:
        return False, f"could not reach {url} ({exc.reason}) — is the " \
                      "agent running, and is the host/port right?"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"health check failed: {exc}"
    if isinstance(data, dict) and data.get("ok") is True:
        flag = data.get("remote_control_enabled")
        return True, "health OK — agent is answering" + (
            " (remote control ON)" if flag else
            " (remote control OFF — commands will be refused)")
    return False, f"health check got an unexpected reply: {data!r}"


def redact(text, token):
    """Hide the token if it ever appears in a log line. Only
    redacts tokens of 8+ characters, so short words are untouched."""
    token = str(token or "")
    if len(token) >= 8 and token in text:
        return text.replace(token, "[token hidden]")
    return text


def status_text_for_jett(cfg, running, health_ok, health_msg):
    """Token-free status block Phil can paste to Jett. Never
    includes the token, by construction — the token is not read
    here at all beyond being deliberately left out."""
    lines = [
        "Laptop Agent status (from the launcher):",
        f"  host: {cfg.get('host')}",
        f"  HTTP port: {cfg.get('port')}  (http://{cfg.get('host')}"
        f":{cfg.get('port')}/health)",
        f"  bridge: ws://{cfg.get('host')}:{cfg.get('bridge_port')}"
        f"{cfg.get('bridge_path', '/v1/bridge')}",
        f"  remote_control_enabled: {bool(cfg.get('remote_control_enabled'))}",
        f"  agent running: {running}",
        f"  health check: {'OK' if health_ok else 'not OK'} — {health_msg}",
        f"  token: set (not shown here — send it securely, never in chat)",
    ]
    return "\n".join(lines)

# ---------------------------------------------------------------- agent process

class AgentProcess:
    """Runs the agent as a captured subprocess.

    Prefers LaptopAgentV2.exe beside the launcher; otherwise runs
    `python laptop_agent_v2.py`. The agent's stdout/stderr are
    pumped into a queue the GUI drains — output is never lost when
    the agent exits, which is exactly the failure Phil already hit
    (a window that closed on error and took the error with it).
    """

    def __init__(self, base=None):
        self.base = Path(base or base_dir())
        self.proc = None
        self._queue = queue.Queue()
        self._reader = None
        self.exit_code = None

    def find_command(self):
        """Return (cmd_list, error_or_None) for starting the agent."""
        exe = self.base / AGENT_EXE
        if exe.exists():
            return [str(exe), "--config", str(self.base / CONFIG_NAME)], None
        script = self.base / AGENT_SCRIPT
        if script.exists():
            return [sys.executable, str(script), "--config",
                    str(self.base / CONFIG_NAME)], None
        return None, (
            f"Neither {AGENT_EXE} nor {AGENT_SCRIPT} is in this folder "
            f"({self.base}). Click Build EXE, or unzip the full package "
            "here again.")

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            return False, "Agent is already running."
        cmd, err = self.find_command()
        if err:
            return False, err
        kwargs = {"cwd": str(self.base), "stdout": subprocess.PIPE,
                  "stderr": subprocess.STDOUT, "text": True,
                  "bufsize": 1}
        if os.name == "nt":
            # Hide the agent's own console: its output goes to the
            # launcher's Log tab instead.
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            self.proc = None
            return False, f"Could not start the agent: {exc}"
        self.exit_code = None
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        return True, f"Agent started (PID {self.proc.pid})."

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self._queue.put(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(None)  # end-of-output marker

    def drain(self):
        """Lines produced since the last drain (never blocks)."""
        lines = []
        while True:
            try:
                line = self._queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            lines.append(line)
        return lines

    def state(self):
        """'stopped', 'running', or 'exited:<code>'."""
        if self.proc is None:
            return "stopped"
        code = self.proc.poll()
        if code is None:
            return "running"
        self.exit_code = code
        return f"exited:{code}"

    def stop(self, timeout=5):
        """Stop the agent. Closing the launcher calls this too —
        it is the kill-switch."""
        if self.proc is None:
            return "Agent was not running."
        if self.proc.poll() is not None:
            code = self.proc.poll()
            self.proc = None
            return f"Agent had already stopped (exit code {code})."
        self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=timeout)
        code = self.proc.poll()
        self.proc = None
        return f"Agent stopped (exit code {code})."


INSTRUCTIONS_TEXT = """\
Laptop Agent — how to get Jett connected
=========================================

This launcher runs laptop-agent v2 on this PC. When it is running
and switched ON, Jett can run commands, search/read/write files,
and take screenshots on THIS PC over Tailscale — nothing else.
No mouse/keyboard, no window control, no chat or task running.

Step 1 — Install what the PC needs (once)
------------------------------------------
* Python 3.10 or newer from python.org — tick "Add python.exe
  to PATH" during install.
* Tailscale from tailscale.com/download — install it and sign in.
  Then run `tailscale ip` in a command window: it prints a
  100.x.x.x number. That is this PC's Tailscale IP. The launcher
  can find it for you with the Auto-detect button in Settings.

Step 2 — Build the EXE (once)
------------------------------
Click "Build EXE" on the Status tab. It builds LaptopAgentV2.exe
and LaptopLauncher.exe and puts both in this folder. This takes
a minute or two and needs internet for pip.
You can skip this: the launcher also runs the Python script
directly (Start uses the EXE if it is here, otherwise the script).
Windows SmartScreen will warn on first run because the EXE is
unsigned — it was built by you, on this PC. Click "More info",
then "Run anyway". Never download an EXE for this from anywhere
else.

Step 3 — Settings
-----------------
Open the Settings tab:
* Token — click Generate. That is the password Jett uses. Keep
  it secret: never paste it in chat, never put it in a file that
  leaves this PC.
* Host — click Auto-detect (or type the 100.x.x.x Tailscale IP).
* Ports — leave HTTP 8899 and bridge 8765 unless something else
  already uses them.
* Remote control — leave OFF until you are ready. Turning it ON
  means Jett can run commands and read/write files on this PC.
  The launcher asks you to confirm, and the agent itself also
  refuses everything while the flag is off.
* Click Save settings. The launcher checks everything first and
  tells you in plain words if something is wrong.

Step 4 — Start
--------------
Click Start on the Status tab. The light turns green and the Log
tab shows both lines:
  HTTP listening on <tailscale-ip>:8899
  BRIDGE listening on ws://<tailscale-ip>:8765/v1/bridge
If it stops straight away, the reason stays in the Log tab —
read it, fix it, Start again. The launcher never closes on an
agent error.

Step 5 — Tell Jett
------------------
Click "Copy status for Jett" and paste it to Jett. That text has
the IP, ports, flag and running state — never the token.
Send the token separately, securely (secure vault / secure page).
Never paste the token in chat.

Step 6 — Stop any time
----------------------
Click Stop, or just close this launcher. Closing it stops the
agent too. That is the kill-switch: no window, no access.

Troubleshooting — the failures already seen
--------------------------------------------
* "The window closes and I can't read the error"
  Fixed by this launcher: run the agent from here, not by
  double-clicking the EXE. Errors stay in the Log tab.
* "LaptopAgentV2.exe is not in this folder"
  The files were split across folders (the EXE was in dist\\).
  Keep everything in ONE folder. Build EXE in this launcher
  copies both EXEs back here automatically.
* "config not found"
  agent_config.json is not beside the launcher. Click Save
  settings once — the launcher creates it here.
* "set a real token in the config first" / token still CHANGE-ME
  Click Generate in Settings, then Save settings, then Start.
* Health check says remote control OFF / commands refused (403)
  Remote control is OFF. Turn it ON in Settings, Save, Stop,
  then Start again. The agent only reads the flag when it starts.
* "host 0.0.0.0 is refused"
  The host must be the Tailscale IP or 127.0.0.1 — never 0.0.0.0.
* SmartScreen / "Windows protected your PC"
  Normal for a self-built unsigned EXE. "More info" -> "Run anyway".
* "port already in use" / address in use
  Something is already listening on 8899 or 8765 — maybe the
  agent is already running (check the Status light), or another
  program uses that port. Stop the other copy, or change the
  ports in Settings (both must stay different from each other).
* Tailscale IP keeps changing / Auto-detect finds nothing
  Tailscale is not running or not signed in. Open Tailscale,
  sign in, wait for Connected, then Auto-detect again.
* Screenshots fail with "pip install mss pillow"
  Screenshots need those two packages. Click Build EXE again
  (it installs them), or run: pip install mss pillow
* Nothing answers on 8899 from Jett's side
  The agent must be running AND the host must be the Tailscale
  IP (not 127.0.0.1 — Jett cannot reach 127.0.0.1 on your PC).
  Check the Status tab: green light, host is 100.x.x.x.

What this agent does NOT do
----------------------------
No mouse/keyboard or window control. No submit_task / chat.
No lock-screen auto-off — closing the launcher is the off switch.
It never phones home anywhere except Tailscale: the HTTP port
8899 and bridge port 8765 listen on the Tailscale IP only.
Never expose those ports to the public internet.
"""

# ---------------------------------------------------------------- GUI (tkinter only)

import tkinter as tk
from tkinter import messagebox, ttk


class LauncherApp:
    """The launcher window. The window never closes on an agent
    error — agent output is captured into the Log tab instead."""

    def __init__(self, root):
        self.root = root
        self.base = base_dir()
        self.cfg, load_err = load_config(self.base)
        self.agent = AgentProcess(self.base)
        self.health_ok = False
        self.health_msg = "not checked yet"
        self._poll_count = 0
        self._build_queue = queue.Queue()
        self._building = False
        self._flag_guard = False
        self.root.title(f"{APP_NAME}  —  laptop-agent v2")
        self.root.geometry("760x560")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build_widgets()
        self._load_settings_into_form()
        if load_err:
            self._log(f"[launcher] {load_err}")
            self._log("[launcher] Click Save settings to create "
                      "agent_config.json here.")
        self._refresh_status()
        self.root.after(200, self._poll)

    # -- widget construction -------------------------------------

    def _build_widgets(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_status = ttk.Frame(self.notebook)
        self.tab_settings = ttk.Frame(self.notebook)
        self.tab_log = ttk.Frame(self.notebook)
        self.tab_help = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_status, text="Status")
        self.notebook.add(self.tab_settings, text="Settings")
        self.notebook.add(self.tab_log, text="Log")
        self.notebook.add(self.tab_help, text="Instructions")

        # ---- Status tab ----
        self.status_dot = tk.Label(self.tab_status, text="●",
                                   font=("Segoe UI", 28), fg="red")
        self.status_dot.pack(anchor="w", padx=10, pady=(10, 0))
        self.status_label = tk.Label(
            self.tab_status, text="Stopped",
            font=("Segoe UI", 14, "bold"))
        self.status_label.pack(anchor="w", padx=10)
        self.urls_label = tk.Label(self.tab_status, text="",
                                   font=("Segoe UI", 10), justify="left")
        self.urls_label.pack(anchor="w", padx=10, pady=(4, 0))
        self.health_label = tk.Label(self.tab_status,
                                     text="Health: not checked yet",
                                     font=("Segoe UI", 10), justify="left",
                                     wraplength=700)
        self.health_label.pack(anchor="w", padx=10, pady=(4, 0))
        self.flag_label = tk.Label(self.tab_status, text="",
                                   font=("Segoe UI", 10, "bold"),
                                   justify="left")
        self.flag_label.pack(anchor="w", padx=10, pady=(4, 0))

        btns = ttk.Frame(self.tab_status)
        btns.pack(fill="x", padx=10, pady=12)
        self.start_btn = ttk.Button(btns, text="▶  Start",
                                    command=self.on_start)
        self.start_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ttk.Button(btns, text="■  Stop",
                                   command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="Test connection",
                   command=self.on_test).pack(side="left", padx=6)
        ttk.Button(btns, text="Build EXE",
                   command=self.on_build_exe).pack(side="left", padx=6)

        btns2 = ttk.Frame(self.tab_status)
        btns2.pack(fill="x", padx=10, pady=(0, 12))
        ttk.Button(btns2, text="Copy Tailscale IP",
                   command=self.on_copy_ip).pack(side="left", padx=(0, 6))
        ttk.Button(btns2, text="Copy status for Jett",
                   command=self.on_copy_status).pack(side="left", padx=6)
        ttk.Button(btns2, text="Open folder",
                   command=self.on_open_folder).pack(side="left", padx=6)
        self.status_note = tk.Label(
            self.tab_status,
            text="Stop (or closing this window) stops the agent — "
                 "that is the kill-switch.\nThe token is never shown in "
                 "the status text.",
            font=("Segoe UI", 9), justify="left", fg="gray")
        self.status_note.pack(anchor="w", padx=10)

        # ---- Settings tab ----
        form = ttk.Frame(self.tab_settings)
        form.pack(fill="both", expand=True, padx=12, pady=12)
        row = 0
        ttk.Label(form, text="Token (Jett's password — keep it secret):"
                  ).grid(row=row, column=0, sticky="w", pady=4)
        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(form, textvariable=self.token_var,
                                     show="*", width=46)
        self.token_entry.grid(row=row, column=1, sticky="we", pady=4)
        ttk.Button(form, text="Generate",
                   command=self.on_generate_token).grid(
                       row=row, column=2, padx=6, pady=4)
        row += 1
        self.show_token_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Show token",
                        variable=self.show_token_var,
                        command=self.on_toggle_show_token).grid(
                            row=row, column=1, sticky="w")
        row += 1
        ttk.Label(form, text="Host (Tailscale IP):").grid(
            row=row, column=0, sticky="w", pady=4)
        self.host_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.host_var, width=46).grid(
            row=row, column=1, sticky="we", pady=4)
        ttk.Button(form, text="Auto-detect",
                   command=self.on_autodetect).grid(row=row, column=2,
                                                   padx=6, pady=4)
        row += 1
        ttk.Label(form, text="HTTP port:").grid(row=row, column=0,
                                               sticky="w", pady=4)
        self.http_port_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.http_port_var,
                  width=12).grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        ttk.Label(form, text="Bridge port:").grid(row=row, column=0,
                                                 sticky="w", pady=4)
        self.bridge_port_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.bridge_port_var,
                  width=12).grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        ttk.Label(form, text="Command timeout (seconds):").grid(
            row=row, column=0, sticky="w", pady=4)
        self.timeout_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.timeout_var, width=12).grid(
            row=row, column=1, sticky="w", pady=4)
        row += 1
        ttk.Label(form, text="Allowed folders (optional, comma separated;\n"
                             "blank = whole PC):").grid(
                                 row=row, column=0, sticky="w", pady=4)
        self.roots_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.roots_var, width=46).grid(
            row=row, column=1, sticky="we", pady=4)
        row += 1
        self.flag_var = tk.BooleanVar(value=False)
        self.flag_check = ttk.Checkbutton(
            form, text="Remote control ON — Jett can run commands and "
                       "read/write files on this PC",
            variable=self.flag_var)
        self.flag_check.grid(row=row, column=0, columnspan=3,
                             sticky="w", pady=8)
        self.flag_var.trace_add("write", self._on_flag_toggle)
        row += 1
        ttk.Button(form, text="Save settings",
                   command=self.on_save).grid(row=row, column=1,
                                              sticky="w", pady=4)
        row += 1
        self.settings_msg = tk.Label(form, text="", wraplength=650,
                                     justify="left", fg="green")
        self.settings_msg.grid(row=row, column=0, columnspan=3,
                               sticky="w", pady=4)
        form.columnconfigure(1, weight=1)

        # ---- Log tab ----
        logbar = ttk.Frame(self.tab_log)
        logbar.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Button(logbar, text="Clear log",
                   command=self.on_clear_log).pack(side="left", padx=(0, 6))
        ttk.Button(logbar, text="Open agent.log",
                   command=self.on_open_log).pack(side="left", padx=6)
        ttk.Button(logbar, text="Open config",
                   command=self.on_open_config).pack(side="left", padx=6)
        self.log_text = tk.Text(self.tab_log, wrap="word",
                                font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_text.config(state="disabled")

        # ---- Instructions tab ----
        help_text = tk.Text(self.tab_help, wrap="word",
                            font=("Segoe UI", 10))
        help_text.pack(fill="both", expand=True, padx=8, pady=8)
        help_text.insert("1.0", INSTRUCTIONS_TEXT)
        help_text.config(state="disabled")

    # -- settings form -------------------------------------------

    def _load_settings_into_form(self):
        self.token_var.set(str(self.cfg.get("token", "")))
        self.host_var.set(str(self.cfg.get("host", "")))
        self.http_port_var.set(str(self.cfg.get("port", "")))
        self.bridge_port_var.set(str(self.cfg.get("bridge_port", "")))
        self.timeout_var.set(str(self.cfg.get("timeout_secs", "")))
        roots = self.cfg.get("allowed_roots") or []
        self.roots_var.set(", ".join(str(r) for r in roots))
        self._flag_guard = True
        self.flag_var.set(bool(self.cfg.get("remote_control_enabled")))
        self._flag_guard = False

    def _gather_settings(self):
        cfg = dict(self.cfg)
        cfg["token"] = self.token_var.get().strip()
        cfg["host"] = self.host_var.get().strip()
        cfg["port"] = self.http_port_var.get().strip()
        cfg["bridge_port"] = self.bridge_port_var.get().strip()
        cfg["timeout_secs"] = self.timeout_var.get().strip()
        cfg["allowed_roots"] = parse_allowed_roots(self.roots_var.get())
        cfg["remote_control_enabled"] = bool(self.flag_var.get())
        return cfg

    def _on_flag_toggle(self, *_args):
        if self._flag_guard:
            return
        if self.flag_var.get():
            ok = messagebox.askyesno(
                "Turn remote control ON?",
                "Turning remote control ON means Jett will be able to\n"
                "run commands and read/write files on THIS PC\n"
                "(over Tailscale, with your token).\n\n"
                "Only turn it on for a PC you are happy for Jett to\n"
                "work on like this.\n\nTurn it on?")
            if not ok:
                self._flag_guard = True
                self.flag_var.set(False)
                self._flag_guard = False

    def on_toggle_show_token(self):
        self.token_entry.config(
            show="" if self.show_token_var.get() else "*")

    def on_generate_token(self):
        token = generate_token()
        self.token_var.set(token)
        self.settings_msg.config(
            text="New token generated. Click Save settings — and "
                 "remember to send it to Jett securely, never in chat.",
            fg="green")

    def on_autodetect(self):
        ip = detect_tailscale_ip()
        if ip:
            self.host_var.set(ip)
            self.settings_msg.config(
                text=f"Tailscale IP detected: {ip}", fg="green")
        else:
            self.settings_msg.config(
                text="Could not find a Tailscale IP — is Tailscale "
                     "installed, running and signed in? Type the "
                     "100.x.x.x address by hand if you know it.",
                fg="red")

    def on_save(self):
        cfg = self._gather_settings()
        errors = validate_config(cfg)
        if errors:
            self.settings_msg.config(
                text="Not saved:\n• " + "\n• ".join(errors), fg="red")
            return
        # Normalise numbers now that validation passed.
        cfg["port"] = int(str(cfg["port"]).strip())
        cfg["bridge_port"] = int(str(cfg["bridge_port"]).strip())
        cfg["timeout_secs"] = int(str(cfg["timeout_secs"]).strip())
        try:
            save_config(cfg, self.base)
        except OSError as exc:
            self.settings_msg.config(
                text=f"Could not save agent_config.json: {exc}", fg="red")
            return
        self.cfg = cfg
        self.settings_msg.config(
            text="Settings saved to agent_config.json beside the "
                 "launcher.", fg="green")
        self._log("[launcher] Settings saved.")
        self._refresh_status()

    # -- status --------------------------------------------------

    def _refresh_status(self):
        state = self.agent.state()
        running = state == "running"
        if state == "running":
            self.status_dot.config(fg="green")
            self.status_label.config(text="●  Running")
        elif state.startswith("exited:"):
            self.status_dot.config(fg="orange")
            self.status_label.config(
                text=f"●  Agent stopped itself (exit code "
                     f"{state.split(':', 1)[1]}) — see the Log tab")
        else:
            self.status_dot.config(fg="red")
            self.status_label.config(text="●  Stopped")
        host = self.cfg.get("host")
        self.urls_label.config(
            text=f"HTTP: http://{host}:{self.cfg.get('port')}/health\n"
                 f"Bridge: ws://{host}:{self.cfg.get('bridge_port')}"
                 f"{self.cfg.get('bridge_path', '/v1/bridge')}")
        flag = bool(self.cfg.get("remote_control_enabled"))
        self.flag_label.config(
            text="Remote control: ON — Jett can run commands/files" if flag
            else "Remote control: OFF — commands will be refused",
            fg="darkred" if flag else "gray")
        self.health_label.config(
            text=f"Health: {self.health_msg}")
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")

    def _poll(self):
        # Drain agent output into the Log tab (redacted).
        for line in self.agent.drain():
            self._log(redact(line, str(self.cfg.get("token", ""))))
        while True:
            try:
                line = self._build_queue.get_nowait()
            except queue.Empty:
                break
            self._log(line)
        self._poll_count += 1
        if self.agent.state() == "running" and self._poll_count % 10 == 0:
            ok, msg = check_health(str(self.cfg.get("host")),
                                   int(self.cfg.get("port", 8899)))
            self.health_ok, self.health_msg = ok, msg
        self._refresh_status()
        self.root.after(200, self._poll)

    def _log(self, text):
        self.log_text.config(state="normal")
        self.log_text.insert("end", str(text) + "\n")
        lines = self.log_text.get("1.0", "end").splitlines()
        if len(lines) > 3000:
            self.log_text.delete("1.0", f"{len(lines) - 3000}.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # -- buttons -------------------------------------------------

    def on_start(self):
        errors = validate_config(self.cfg)
        if errors:
            messagebox.showerror(
                "Settings need fixing first",
                "The saved settings are not valid yet:\n• " +
                "\n• ".join(errors) +
                "\n\nFix them on the Settings tab and Save, then Start.")
            self.notebook.select(self.tab_settings)
            return
        ok, msg = self.agent.start()
        self._log(f"[launcher] {msg}")
        if not ok:
            messagebox.showerror("Could not start the agent", msg)
        self._refresh_status()

    def on_stop(self):
        msg = self.agent.stop()
        self._log(f"[launcher] {msg}")
        self.health_ok, self.health_msg = False, "agent stopped"
        self._refresh_status()

    def on_test(self):
        host = str(self.cfg.get("host"))
        try:
            port = int(str(self.cfg.get("port")).strip())
        except (TypeError, ValueError):
            messagebox.showerror("Test connection",
                                 "HTTP port is not a number — fix it in "
                                 "Settings first.")
            return
        ok, msg = check_health(host, port)
        self.health_ok, self.health_msg = ok, msg
        self._log(f"[launcher] Test connection: {msg}")
        if ok:
            messagebox.showinfo("Test connection", msg)
        else:
            messagebox.showwarning("Test connection", msg)
        self._refresh_status()

    def on_build_exe(self):
        if self._building:
            return
        bat = self.base / "build_launcher.bat"
        if not bat.exists():
            bat = self.base / "build_exe.bat"
        if not bat.exists():
            messagebox.showerror(
                "Build EXE",
                "No build_launcher.bat or build_exe.bat in this folder.\n"
                "Unzip the full package here again.")
            return
        self._building = True
        self._log(f"[launcher] Building EXE ({bat.name}) — this takes a "
                  "minute or two, output follows...")
        self.notebook.select(self.tab_log)

        def run_build():
            try:
                proc = subprocess.Popen(
                    ["cmd", "/c", str(bat)], cwd=str(self.base),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
                for line in proc.stdout:
                    self._build_queue.put(line.rstrip("\n"))
                proc.wait()
                self._build_queue.put(
                    f"[launcher] Build finished (exit code "
                    f"{proc.returncode}).")
            except OSError as exc:
                self._build_queue.put(f"[launcher] Build failed: {exc}")
            finally:
                self._building = False

        threading.Thread(target=run_build, daemon=True).start()

    def on_copy_ip(self):
        ip = str(self.cfg.get("host", "") or "").strip()
        if not ip:
            messagebox.showinfo("Copy Tailscale IP",
                                "No host is set yet — use Auto-detect in "
                                "Settings first.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(ip)
        self._log(f"[launcher] Copied IP {ip} to clipboard.")

    def on_copy_status(self):
        running = self.agent.state() == "running"
        text = status_text_for_jett(self.cfg, running, self.health_ok,
                                    self.health_msg)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._log("[launcher] Copied token-free status for Jett.")

    def _open_path(self, path):
        try:
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606 — local path only
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Open", f"Could not open {path}: {exc}")

    def on_open_folder(self):
        self._open_path(self.base)

    def on_open_log(self):
        path = log_path(self.base)
        if not path.exists():
            messagebox.showinfo("Open agent.log",
                                "agent.log does not exist yet — start the "
                                "agent first.")
            return
        self._open_path(path)

    def on_open_config(self):
        path = config_path(self.base)
        if not path.exists():
            messagebox.showinfo(
                "Open config",
                "agent_config.json does not exist yet — click Save "
                "settings first.")
            return
        self._open_path(path)

    def on_clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def on_close(self):
        if self.agent.state() == "running":
            self._log("[launcher] Closing — stopping the agent first.")
            self.agent.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
