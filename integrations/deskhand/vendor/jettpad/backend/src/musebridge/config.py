"""Configuration loading and fail-closed validation."""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "bind": "",
    "port": 8765,
    "token": "",
    "tier": 1,
    "scopes": {},
    "secret_overrides": {},
    "features": {
        "proactive": True,
        "screenshots": False,
        "desktop_control": False,
        "exec": False,
    },
    "desktop_apps": [],
    "rate_limits": {
        "proactive_count": 20,
        "proactive_window_secs": 600,
        "exec_count": 20,
        "exec_window_secs": 600,
        "desktop_per_second": 10,
        "screenshots_per_minute": 20,
    },
    "max_task_budget_secs": 600,
    "whole_drive_warning_accepted": False,
    "chat_timeout_secs": 600,
    "retain_chat_text": False,
    "desktop_app_paths": [],
    "require_frame_id": True,
    "execution_mode": "host",
    "peer_addresses": [],
    "peer_name": "Muse",
}

SECRET_PATH_PARTS = {
    "musebridge.config.json",
    "muse-bridge.json",
    "operator.secret", "runtime.json", "state.sqlite3", "audit.sqlite3",
    "musebridge.log",
    "state.sqlite3-wal", "state.sqlite3-shm", "audit.sqlite3-wal", "audit.sqlite3-shm",
}


def default_config() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def tailscale_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("bind must be a Tailscale interface IP address") from exc
    network = ipaddress.ip_network(
        "100.64.0.0/10" if address.version == 4 else "fd7a:115c:a1e0::/48"
    )
    if address not in network:
        raise ValueError("bind must be a Tailscale interface IP address")
    return str(address)


def validate_config(config: dict[str, Any], require_enabled: bool = False) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Configuration must be an object")
    # STOP latch removed (Phil 2026-11-09): legacy keys are discarded, never
    # persisted, so an old on-disk config still loads.
    config = {key: value for key, value in config.items()
              if key not in ('stop_latched', 'stop_reason')}
    if "desktop_resume_approved" in config and type(config["desktop_resume_approved"]) is not bool:
        raise ValueError("desktop_resume_approved must be boolean")
    merged = default_config()
    merged.update(config)
    for key in ("features", "rate_limits"):
        if not isinstance(merged[key], dict):
            raise ValueError(f"{key} must be an object")
    merged["features"] = {**DEFAULT_CONFIG["features"], **merged["features"]}
    provided_limits = merged["rate_limits"]
    merged["rate_limits"] = {**DEFAULT_CONFIG["rate_limits"], **provided_limits}
    if "exec_per_minute" in provided_limits:
        if "exec_count" not in provided_limits:
            merged["rate_limits"]["exec_count"] = provided_limits["exec_per_minute"]
        if "exec_window_secs" not in provided_limits:
            merged["rate_limits"]["exec_window_secs"] = 60
        merged["rate_limits"].pop("exec_per_minute", None)

    # B2-E (B-9): reject unknown keys instead of silently carrying them, so
    # a typo'd or stale setting cannot run with the intended value ignored.
    # Legacy STOP keys are stripped above, so they never trip this check.
    allowed_keys = set(DEFAULT_CONFIG) | {"desktop_resume_approved"}
    unknown = sorted(set(config) - allowed_keys)
    if unknown:
        raise ValueError(f"Unknown configuration key: {', '.join(unknown)}")
    unknown_features = sorted(set(merged["features"]) - set(DEFAULT_CONFIG["features"]))
    if unknown_features:
        raise ValueError(f"Unknown features key: {', '.join(unknown_features)}")
    unknown_limits = sorted(set(merged["rate_limits"]) - set(DEFAULT_CONFIG["rate_limits"]))
    if unknown_limits:
        raise ValueError(f"Unknown rate_limits key: {', '.join(unknown_limits)}")

    # B2-E (B-11): grandfather the persistent desktop-control approval that
    # existing installs already carry as features.desktop_control, so the
    # reconnect resume gate does not silently disable control on upgrade.
    # New approvals are recorded explicitly (operator toggle / init).
    if "desktop_resume_approved" not in merged:
        merged["desktop_resume_approved"] = bool(merged["features"].get("desktop_control"))

    if type(merged["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if not isinstance(merged["peer_name"], str) or not 1 <= len(merged["peer_name"]) <= 80:
        raise ValueError("peer_name must be 1 to 80 characters")
    peers = merged["peer_addresses"]
    if not isinstance(peers, list) or len(peers) > 8 or any(not isinstance(value, str) for value in peers):
        raise ValueError("peer_addresses must be a list of at most 8 tailnet addresses")
    merged["peer_addresses"] = [tailscale_address(value) for value in peers]
    for field in ("retain_chat_text", "require_frame_id"):
        if type(merged[field]) is not bool:
            raise ValueError(f"{field} must be boolean")
    if type(merged["chat_timeout_secs"]) is not int or not 30 <= merged["chat_timeout_secs"] <= 86400:
        raise ValueError("chat_timeout_secs must be between 30 and 86400")
    if merged["execution_mode"] not in {"host", "windows_sandbox"}:
        raise ValueError("Unknown execution_mode")
    if not isinstance(merged["desktop_app_paths"], list) or any(not isinstance(path, str) or not Path(path).is_absolute() for path in merged["desktop_app_paths"]):
        raise ValueError("desktop_app_paths must contain absolute executable paths")
    if type(merged["tier"]) is not int or merged["tier"] not in (0, 1, 2):
        raise ValueError("tier must be 0 (off), 1 (scoped), or 2 (whole drive)")
    if type(merged["port"]) is not int or not 0 <= merged["port"] <= 65535:
        raise ValueError("port must be between 0 (OS-assigned) and 65535")
    if type(merged["max_task_budget_secs"]) is not int or not 1 <= merged["max_task_budget_secs"] <= 600:
        raise ValueError("max_task_budget_secs must be between 1 and 600")
    for key, value in merged["features"].items():
        if type(value) is not bool:
            raise ValueError(f"features.{key} must be boolean")
    if not isinstance(merged["scopes"], dict):
        raise ValueError("scopes must be an object")
    if not isinstance(merged["secret_overrides"], dict):
        raise ValueError("secret_overrides must be an object")
    allowed_overrides = {"ssh_keys", "browser_profiles", "credentials", "password_managers"}
    if any(key not in allowed_overrides or type(value) is not bool
           for key, value in merged["secret_overrides"].items()):
        raise ValueError("secret_overrides must contain only known boolean categories")
    if not isinstance(merged["desktop_apps"], list) or any(
        not isinstance(app, str) or not app or "/" in app or "\\" in app for app in merged["desktop_apps"]
    ):
        raise ValueError("desktop_apps must contain executable names only")
    for key in (
        "proactive_count", "proactive_window_secs", "exec_count", "exec_window_secs",
        "desktop_per_second", "screenshots_per_minute",
    ):
        value = merged["rate_limits"].get(key)
        ceiling = 10000 if key.endswith("count") or key.endswith("minute") or key.endswith("second") else 86400
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"rate_limits.{key} must be a finite positive integer")
    token = merged["token"] or os.environ.get("MUSEBRIDGE_TOKEN", "")
    if not isinstance(token, str):
        raise ValueError("token must be a string")
    if merged["enabled"] or require_enabled:
        tailscale_address(merged["bind"])
        if len(token) < 32 or token.startswith("REPLACE"):
            raise ValueError("Set a random bridge token of at least 32 characters")
    for alias, scope in merged["scopes"].items():
        if not isinstance(alias, str) or not alias or len(alias) > 64:
            raise ValueError("Scope names must be non-empty and at most 64 characters")
        if not isinstance(scope, dict) or not isinstance(scope.get("path"), str):
            raise ValueError(f"Scope {alias} must include a path")
        if alias == "whole_drive":
            raise ValueError("whole_drive is a reserved scope alias")
        permissions = scope.get("permissions", [])
        if not isinstance(permissions, list) or any(p not in ("read", "write", "exec") for p in permissions):
            raise ValueError(f"Scope {alias} has invalid permissions")
        root = Path(scope["path"]).expanduser()
        if not root.is_absolute():
            raise ValueError(f"Scope {alias} path must be absolute")
        resolved_root = root.resolve(strict=False)
        if resolved_root == resolved_root.parent and merged["tier"] != 2:
            raise ValueError("A whole-drive root requires tier 2 and its explicit warning")
    if merged["tier"] == 2 and merged["whole_drive_warning_accepted"] is not True:
        raise ValueError("Whole-drive tier requires explicit whole_drive_warning_accepted: true")
    return merged


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read configuration: {exc}") from exc
    return validate_config(data)


def save_config(path: Path, config: dict[str, Any]) -> None:
    checked = validate_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(checked, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        if os.name != "nt":
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def generate_token() -> str:
    return secrets.token_urlsafe(48)
