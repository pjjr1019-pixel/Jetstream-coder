"""Shared execution-time authorization for local operations."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from .config import SECRET_PATH_PARTS


class PermissionDenied(ValueError):
    code = "permission_denied"


def _resolved(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise PermissionDenied("An absolute path is required")
    # strict=False resolves existing symlink parents and normalizes traversal.
    return candidate.resolve(strict=False)


def _contains(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


class PermissionManager:
    def __init__(self, config: dict[str, Any], config_path: Path):
        self.config = config
        self.config_path = config_path.resolve(strict=False)
        self.session_profile: str | None = None
        self.session_expires_at: float | None = None

    def set_session(self, profile: str | None, minutes: int = 60) -> None:
        if profile not in {None, "files_read", "files_edit", "assistant"}:
            raise ValueError("Unknown session profile")
        if type(minutes) is not int or not 1 <= minutes <= 480:
            raise ValueError("Session duration must be 1 to 480 minutes")
        self.session_profile = profile
        self.session_expires_at = time.time()+minutes*60 if profile is not None else None

    def check_session(self, permission: str) -> None:
        if self.session_expires_at is not None and time.time() >= self.session_expires_at:
            raise PermissionDenied("The local permission session has expired")
        allowed = {"files_read": {"read"}, "files_edit": {"read", "write"},
                   "assistant": {"read", "write", "exec", "desktop"}}
        if self.session_profile is not None and permission not in allowed[self.session_profile]:
            raise PermissionDenied("Operation is excluded by the local session profile")

    def authorize(self, value: str, permission: str) -> Path:
        self.check_session(permission)
        path = _resolved(value)
        if self._is_secret(path):
            raise PermissionDenied("blocked path")
        tier = self.config.get("tier", 0)
        if tier == 0:
            raise PermissionDenied("Bridge permissions are off")
        if tier == 2:
            return path
        scopes = self.config.get("scopes", {})
        for scope in scopes.values():
            root = _resolved(scope["path"])
            permissions = scope.get("permissions", [])
            if permission in permissions and _contains(root, path):
                return path
        raise PermissionDenied("outside approved scope")

    def _is_secret(self, path: Path) -> bool:
        normalized = str(path).casefold()
        config_path = str(self.config_path).casefold()
        if normalized == config_path or normalized.startswith(config_path+".") or _contains(self.config_path, path):
            return True
        parts = {part.casefold() for part in path.parts}
        if parts.intersection({"musebridge-recovery", "musebridge-uploads"}):
            return True
        name = path.name.casefold()
        if any(name == private or name.startswith(private+".") for private in SECRET_PATH_PARTS):
            return True
        categories = {
            "ssh_keys": {".ssh", ".gnupg"},
            "browser_profiles": {
                "user data", "cookies", "login data", "web data", "local state",
                "mozilla", "firefox", "chrome", "edge", "safari",
            },
            "credentials": {"credentials", "credential", "windows credentials"},
            "password_managers": {
                "password manager", "password-managers", "1password",
                "bitwarden", "keepass", "keepassxc", "lastpass", "vault",
            },
        }
        suffixes = {".pem", ".p12", ".pfx", ".key", ".kdbx", ".ppk"}
        if path.suffix.casefold() in suffixes or path.name.casefold() in {
            "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
        }:
            if self.config.get("secret_overrides", {}).get("ssh_keys") is not True:
                return True
        for category, markers in categories.items():
            if parts.intersection(markers) and self.config.get("secret_overrides", {}).get(category) is not True:
                return True
        return False

    def authorize_cwd(self, cwd: str) -> Path:
        target = self.authorize(cwd, "exec")
        if not target.is_dir():
            raise PermissionDenied("cwd must be an existing directory")
        return target

    def exec_scope_roots(self) -> list[Path]:
        tier = self.config.get("tier", 0)
        if tier == 2:
            root = Path(Path.home().anchor).resolve(strict=False)
            return [root] if root.is_absolute() else []
        if tier != 1:
            return []
        roots = []
        for scope in self.config.get("scopes", {}).values():
            if "exec" in scope.get("permissions", []):
                root = _resolved(scope["path"])
                if root.is_dir() and not self._is_secret(root):
                    roots.append(root)
        return roots
