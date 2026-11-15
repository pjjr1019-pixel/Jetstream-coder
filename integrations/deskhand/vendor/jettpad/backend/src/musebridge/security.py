"""Private local pairing credential and short-lived operator sessions."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path


class OperatorCredential:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(secrets.token_urlsafe(48))
        self.token = path.read_text(encoding="ascii").strip()
        if len(self.token) < 32:
            raise ValueError("Local operator credential is invalid")

    def accepts(self, value: str) -> bool:
        return isinstance(value, str) and hmac.compare_digest(value, self.token)

    def issue_session(self, seconds=8*3600) -> str:
        body = f"{int(time.time())+seconds}.{secrets.token_hex(16)}"
        signature = hmac.new(self.token.encode(), body.encode(), hashlib.sha256).hexdigest()
        return body + "." + signature

    def accepts_session(self, value: str) -> bool:
        try:
            expiry, nonce, signature = value.split(".")
            if len(nonce) != 32 or int(expiry) < time.time():
                return False
            body = expiry + "." + nonce
            expected = hmac.new(self.token.encode(), body.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (ValueError, AttributeError):
            return False
