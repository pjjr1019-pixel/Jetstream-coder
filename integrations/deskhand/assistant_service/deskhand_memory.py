"""Approved inspectable desktop memory, v100.008 (Batch 3, smarts #10; F51).

The agent may read a small memory of Phil-approved facts about the
desktop (file paths, naming, habits). The desktop may also keep a
short-lived snapshot of the current session (window, app, open files).

Safety rules (the sensitive-facts rule, from the offline stand-in
NEVER_SPEAK list and MEMORY.md):
- Memory entries may NOT contain dad's health, the Jan 2026
  arrest/legal case, debts/collections, or family money conflicts.
  add_fact() refuses any fact matching those categories, and the
  refusal is logged, not silently skipped.
- Facts are added ONLY by Phil's approval (approved_by="phil" and
  approved=True). Nothing is auto-learned from a turn.
- Memory is inspectable: every fact carries its source and is
  listable/deletable by Phil. No hidden state.
- Session snapshots are ephemeral: they expire after 15 minutes,
  are never persisted to disk, and never carry credentials.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

__version__ = "100.008"

MAX_FACTS = 200
MAX_BYTES = 131072
SNAPSHOT_TTL_SECS = 900

DENIED_PATTERNS = (
    "dad", "father", "cancer", "lung", "tumor", "biopsy", "radiation",
    "arrest", "jail", "court", "attorney", "public defender", "trial",
    "bond", "debt", "collection", "afterpay", "overdue", "rent-a-center",
    "money fight", "owes",
)


def _denied(text):
    lowered = text.lower()
    return [word for word in DENIED_PATTERNS if word in lowered]


def add_fact(store_path, text, *, category, approved, approved_by, source=""):
    """Add a Phil-approved fact. Raises ValueError on a denied category,
    on missing approval, or on invalid fields. Returns the fact dict."""
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError("invalid fact text")
    if not isinstance(category, str) or not category.strip() or len(category) > 100:
        raise ValueError("invalid fact category")
    if approved is not True or approved_by != "phil":
        raise ValueError("facts require Phil's approval")
    hits = _denied(text)
    if hits:
        raise ValueError("fact refused: denied category (%s)" % ", ".join(sorted(set(hits))))
    facts = list_facts(store_path)
    fact = {"id": str(uuid.uuid4()), "text": text.strip(), "category": category,
            "approved_by": approved_by, "source": source,
            "added_at": time.time()}
    facts.append(fact)
    _save(store_path, facts)
    return fact


def _save(store_path, facts):
    if len(facts) > MAX_FACTS:
        raise ValueError("fact store is full")
    raw = json.dumps({"version": 1, "facts": facts}, ensure_ascii=True).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("fact store too large")
    path = Path(store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def list_facts(store_path):
    path = Path(store_path)
    if not path.exists():
        return []
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("fact store too large")
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("invalid fact store")
    return document.get("facts") or []


def search_facts(store_path, query):
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError("invalid search query")
    needle = query.strip().lower()
    return [fact for fact in list_facts(store_path)
            if needle in fact["text"].lower() or needle in fact["category"].lower()]


def delete_fact(store_path, fact_id):
    facts = list_facts(store_path)
    kept = [fact for fact in facts if fact["id"] != fact_id]
    if len(kept) == len(facts):
        raise ValueError("unknown fact id")
    _save(store_path, kept)
    return True


def build_snapshot(*, foreground_app, open_windows, recent_files, screenshot_token):
    """Build a per-session desktop snapshot. In-memory only — callers
    must not persist it. Expires 15 minutes after creation."""
    if not isinstance(foreground_app, str) or not foreground_app.strip():
        raise ValueError("invalid foreground_app")
    now = time.time()
    return {"foreground_app": foreground_app,
            "open_windows": list(open_windows or []),
            "recent_files": list(recent_files or []),
            "screenshot_token": screenshot_token,
            "created_at": now, "expires_at": now + SNAPSHOT_TTL_SECS}


def snapshot_expired(snapshot, now=None):
    now = time.time() if now is None else now
    return now >= snapshot["expires_at"]
