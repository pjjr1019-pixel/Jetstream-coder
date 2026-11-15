"""Command-line entry point for the standalone MuseBridge app."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import subprocess
import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .audit import AuditLog
from .bridge import MuseBridge
from .config import default_config, generate_token, load_config, save_config, tailscale_address
from .operator import OperatorServer
from .lifecycle import InstanceLock, local_request
from .redaction import redact_text
from .guards import DesktopGuards
from .runtime_paths import root as isolated_root


def _deskhand_default_listener():
    """Discover this PC's Tailscale address; the port is OS-assigned (0).

    B2-E (B-14): no throwaway probe socket. Binding a candidate port and
    closing it before the bridge binds it is a TOCTOU race -- another
    process can take the port in between. Port 0 lets the OS assign the
    port at bind time; the bridge records the real port in
    `listener_port`, which operator state and connection details report.
    """
    if os.name != 'nt':
        return None
    executable = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Tailscale' / 'tailscale.exe'
    if not executable.is_file():
        return None
    try:
        result = subprocess.run([str(executable), 'ip', '-4'], capture_output=True, text=True,
            timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
        address = tailscale_address(result.stdout.strip())
        return address, 0
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def default_home() -> Path:
    isolated = isolated_root()
    if isolated is not None:
        return isolated / "bridge"
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "MuseBridge"
    return Path.home() / ".local" / "share" / "MuseBridge"


def _open_pad_settings() -> None:
    script = Path(__file__).resolve().parents[3] / 'JettPad' / 'jett_pad.py'
    python = Path(sys.executable).with_name('pythonw.exe')
    if not script.is_file() or not python.is_file():
        raise RuntimeError('Jett Pad and its Windows Python runtime are required')
    subprocess.Popen([str(python),str(script),'--settings'],
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)


def _lock_windows_directory(path: Path) -> None:
    if os.name != "nt":
        return
    icacls = shutil.which("icacls.exe")
    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    if not icacls or not user:
        raise RuntimeError("Could not set private Windows permissions on MuseBridge data")
    principal = f"{domain}\\{user}" if domain else user
    result = subprocess.run(
        [icacls, str(path), "/inheritance:r", "/grant:r", f"{principal}:(OI)(CI)F", "/T", "/C"],
        capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("Could not restrict MuseBridge data folder permissions")


def _lock_windows_file(path: Path) -> None:
    if os.name != "nt":
        return
    icacls = shutil.which("icacls.exe")
    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    if not icacls or not user:
        raise RuntimeError("Could not set private Windows permissions on MuseBridge data")
    principal = f"{domain}\\{user}" if domain else user
    result = subprocess.run(
        [icacls, str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
        capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError(f"Could not restrict MuseBridge file permissions: {path.name}")


def _lock_windows_data(home: Path) -> None:
    if os.name=='nt' and isolated_root() is not None:
        from .private_acl import secure_tree
        secure_tree(home)
        return
    _lock_windows_directory(home)
    # The recursive directory rule uses inheritance flags. Regular files need
    # explicit file rights too, including nested provenance/transfer stores.
    for path in home.rglob('*'):
        if path.is_file():
            _lock_windows_file(path)


def _remove_legacy_operator_token(home: Path) -> None:
    legacy_token = home / "musebridge.admin-token"
    try:
        legacy_token.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError("Could not remove the obsolete local operator token") from exc


def initialize(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "musebridge.config.json"
    if config_path.exists():
        raise RuntimeError("MuseBridge is already initialized; refusing to overwrite local configuration")
    _remove_legacy_operator_token(home)
    _lock_windows_directory(home)
    config = default_config()
    if isolated_root() is not None:
        config['port'] = 8767
        listener = _deskhand_default_listener()
        if listener is not None:
            config['bind'], config['port'] = listener
        # DeskHand's user requested desktop control and screenshots on by
        # default. This only affects a new isolated profile; explicit later
        # revocations, STOP, application trust and session limits still apply.
        config['features']['desktop_control'] = True
        # B2-E (B-11): the install-time local gesture approving resume.
        config['desktop_resume_approved'] = True
        config['features']['screenshots'] = True
        # Reply files are the copied Pad protocol's transport, not general
        # access to the user's Documents folder. Grant only this private inbox.
        inbox = isolated_root() / 'documents' / 'jett-pad-inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        config['scopes']['deskhand_replies'] = {'path': str(inbox), 'permissions': ['read', 'write']}
    config["token"] = generate_token()
    save_config(config_path, config)
    AuditLog(home / "audit.sqlite3").record("app.initialize", str(home), True)
    _lock_windows_data(home)
    print("MuseBridge initialized.")
    print(f"Private configuration and Muse connection token: {config_path}")
    print("Use the console command to pair the private local controls.")


async def run(home: Path, open_window: bool) -> bool:
    isolated = isolated_root()
    if isolated is not None and home.resolve() != (isolated / "bridge").resolve():
        raise ValueError("Deskhand MuseBridge home must belong to its isolated runtime")
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "musebridge.config.json"
    if not config_path.exists():
        raise RuntimeError("Run `musebridge init` before starting the app")
    _lock_windows_data(home)
    _remove_legacy_operator_token(home)
    config = load_config(config_path)
    # STOP latch removed (Phil 2026-11-09): the isolated bridge no longer
    # latches STOP at startup and no longer requires a fresh local clear.
    # The latch is always off and is not part of the app.
    bridge = MuseBridge(config, config_path, home)
    # The local operator starts the built-in connection explicitly. Failure to
    # load the optional staged integration must not break the existing bridge.
    if isolated is None:
        try:
            app_root=Path(__file__).resolve().parents[3]
            if str(app_root) not in sys.path:sys.path.insert(0,str(app_root))
            embedded_dir=app_root/'embedded'
            if str(embedded_dir) not in sys.path:sys.path.insert(0,str(embedded_dir))
            from .embedded_setup import build_lifecycle
            bridge.embedded_lifecycle = build_lifecycle(bridge)
        except Exception:
            logging.getLogger("musebridge").exception("Built-in connection setup unavailable")
    operator = OperatorServer(bridge, config_path, port=0 if isolated is not None else 8766)
    await operator.start()
    loop = asyncio.get_running_loop()
    def intervention():
        phone=getattr(bridge,'phone_input',None)
        if phone and phone.active:
            phone.end('Local human takeover')
            loop.call_soon_threadsafe(lambda:bridge.desktop.revoke(reason='local_input_handover'))
        elif bridge.executor.input_active:
            bridge.executor.interrupt('local_input_handover')
            loop.call_soon_threadsafe(lambda:bridge.desktop.revoke(reason='local_input_handover'))
        elif bridge.desktop.enabled:
            loop.call_soon_threadsafe(bridge.desktop.intervene)
    def emergency():
        bridge.executor.interrupt('local_emergency_stop')
        loop.call_soon_threadsafe(lambda: asyncio.create_task(bridge.emergency_stop()))
    guards = DesktopGuards(emergency, intervention, bridge.remote_control.session_changed)
    try:
        await asyncio.to_thread(guards.start)
        bridge.remote_control.watch_probe=lambda:guards.session_watch_available and guards.available
        logging.getLogger("musebridge").info("Runtime started; Windows local controls registered=%s", guards.available)
        if config["enabled"] and config["tier"] > 0:
            await bridge.start()
        url = operator.origin + "/"
        print(f"MuseBridge operator console: {url}")
        print("Local controls require pairing. Run the launcher console command to open them.")
        print("Ctrl+C stops this process. Emergency stop: Ctrl+Alt+Shift+Esc.")
        (home/"runtime.json").write_text(json.dumps({"pid": os.getpid(), "operator_port": operator.port,
                                                    "started_at": time.time(), "version": "0.2.0"}), encoding="utf-8")
        if open_window:
            _open_pad_settings()
        await operator.shutdown_requested.wait()
    finally:
        lifecycle=getattr(bridge,'embedded_lifecycle',None)
        if lifecycle is not None:
            try:await lifecycle.stop_local()
            except Exception:logging.getLogger("musebridge").exception("Built-in connection cleanup unconfirmed")
        bridge.remote_control.watch_available=False
        bridge.remote_control.watch_probe=None
        bridge.remote_control.session_changed('session_monitor_stopping')
        await asyncio.to_thread(guards.stop)
        await bridge.stop()
        await operator.stop()
        (home/"runtime.json").unlink(missing_ok=True)
        logging.getLogger("musebridge").info("Runtime shutdown completed")
    return operator.restart_requested


def configure_logging(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)

    class Redact(logging.Filter):
        def filter(self, record):
            record.msg, record.args = redact_text(record.getMessage()), ()
            return True

    handler = RotatingFileHandler(home/"musebridge.log", maxBytes=2*1024*1024, backupCount=3, encoding="utf-8")
    handler.addFilter(Redact())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("musebridge")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def doctor(home: Path) -> dict:
    import importlib.metadata
    config = load_config(home/"musebridge.config.json")
    packages = {}
    for package in ("aiohttp", "websockets", "Pillow", "pyautogui", "pywinauto"):
        try: packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: packages[package] = "not installed"
    return {"version": "0.2.0", "python": sys.version.split()[0], "packages": packages, "configuration_valid": True,
            "bridge_enabled_in_config": config["enabled"], "execution_mode": config.get("execution_mode", "host"),
            "desktop_available": bridge_desktop_available(), "configuration_dir": str(home),
            "peer_ip_pinning": bool(config.get("peer_addresses")), "os_isolated_executor_available": False,
            "browser_dom_available": False, "tailnet_policy_managed_by_bridge": False}


def bridge_desktop_available():
    from .desktop import DesktopAccess
    return DesktopAccess.local_control_available()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="musebridge", description="Standalone, permissioned MuseBridge app")
    parser.add_argument("--home", type=Path, default=default_home(),
                        help="Private data directory (default: per-user application data)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create private configuration and bridge connection token")
    for command in ("status", "stop", "restart", "doctor", "console", "stop-all"):
        commands.add_parser(command, help=f"{command.replace('-', ' ').capitalize()} MuseBridge")
    serve_parser = commands.add_parser("run", help="Run the local operator console and optional bridge")
    serve_parser.add_argument(
        "--no-window", "--no-browser", dest="no_window", action="store_true",
        help="Do not open the local console window automatically",
    )
    args = parser.parse_args(argv)
    try:
        isolated = isolated_root()
        if isolated is not None and args.home.resolve() != (isolated / "bridge").resolve():
            raise ValueError("Deskhand MuseBridge home must belong to its isolated runtime")
        if isolated is not None and args.command == "console":
            raise ValueError("Open Muse settings from Deskhand")
        if args.command == "init":
            initialize(args.home)
        elif args.command == "status":
            state = local_request(args.home)
            print(json.dumps({key: state.get(key) for key in ("enabled", "connected", "diagnostics", "session")}, indent=2))
        elif args.command == "doctor":
            print(json.dumps(doctor(args.home), indent=2))
        elif args.command == "console":
            _open_pad_settings()
        elif args.command in {"stop", "restart", "stop-all"}:
            before = json.loads((args.home/"runtime.json").read_text()).get("started_at", 0) if args.command == "restart" else None
            local_request(args.home, {"stop-all": "stop_all", "restart": "restart", "stop": "shutdown"}[args.command])
            if args.command == "stop":
                for _ in range(100):
                    try: local_request(args.home, timeout=.2)
                    except RuntimeError: break
                    time.sleep(.1)
                else: raise RuntimeError("The process is still stopping; no duplicate instance was launched")
            if args.command == "restart":
                for _ in range(100):
                    try:
                        runtime = json.loads((args.home/"runtime.json").read_text())
                        if runtime.get("started_at", 0) > before:
                            local_request(args.home)
                            break
                    except (OSError, RuntimeError, ValueError): pass
                    time.sleep(.1)
                else: raise RuntimeError("The bridge did not finish restarting; inspect the existing shell and logs")
            print("Restarted in the existing shell." if args.command == "restart" else "Stopped.")
        else:
            with InstanceLock(args.home):
                configure_logging(args.home)
                try:
                    open_window = not args.no_window
                    while asyncio.run(run(args.home, open_window)):
                        open_window = False
                except (OSError, RuntimeError, ValueError):
                    logging.getLogger("musebridge").exception("Runtime failed")
                    raise
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"MuseBridge: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
