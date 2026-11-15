"""Best-effort secret-pattern detection for content leaving the local machine."""

from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}", re.I),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"password|passwd|private[_-]?key|secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9_./+=:-]{8,}",
        re.I,
    ),
)


def contains_sensitive(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def redact_text(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value
