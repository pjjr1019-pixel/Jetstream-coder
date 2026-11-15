"""Token-by-token chat streaming sink (Batch 2, v100.004).

Chat text ONLY. A stream sink carries partial model text from an adapter
to the mailbox/UI while a turn is dispatching. It never carries action
JSON, receipts, grants, STOP state, or credentials:

- Adapters feed it only model text (Ollama SSE deltas, Codex item/delta
  text, Muse observed-text growth). A chunk that parses as a Deskhand
  protocol JSON object is dropped, never forwarded — a streamed action
  would bypass the exact-JSON + grant + receipt path, so the sink refuses
  it by construction.
- The final reply is still delivered exactly once through the existing
  Reply -> Mailbox.finish path. Partials are display-only: a dropped
  connection, a STOP, or an uncertain finish leaves the turn in its
  existing terminal state (completed / cancelled / unknown) — a partial
  is never promoted to a reply and never replayed.
- Thread-safe: adapters call on_delta from their read loop while the
  dispatcher owns the turn. Sequence numbers let the UI apply deltas in
  order and ignore a stale re-delivery.
"""
from __future__ import annotations

import json
import threading
import time

# A streamed chat reply is display text, not a journal record. Cap it so a
# runaway model cannot grow the partial without bound; the final Reply
# (capped separately by the mailbox) is the durable record.
MAX_PARTIAL_CHARS = 100_000


def _looks_like_protocol_json(chunk: str) -> bool:
    """True when a chunk parses as a JSON object naming a Deskhand message.

    Only protocol-shaped objects are refused. Ordinary prose that happens
    to contain braces is not JSON-shaped and passes through untouched.
    """
    text = chunk.strip()
    if not text.startswith("{"):
        return False
    try:
        value = json.loads(text)
    except ValueError:
        return False
    if not isinstance(value, dict):
        return False
    return "type" in value and isinstance(value.get("type"), str) and (
        value["type"].startswith("deskhand.") or value["type"] in {"actions", "proposal"}
    )


class StreamSink:
    """Accumulates streamed chat text for one turn.

    ``on_delta`` is called with (text_so_far, seq) after each accepted
    chunk. ``first_byte_at`` is set on the first accepted chunk and is
    the per-hop timing input for "adapter first byte".
    """

    def __init__(self, turn_id: str, on_delta=None):
        self.turn_id = turn_id
        self.on_delta = on_delta
        self._lock = threading.Lock()
        self._text = ""
        self._seq = 0
        self._dropped_protocol_chunks = 0
        self.first_byte_at = None
        self.closed = False

    @property
    def text(self) -> str:
        with self._lock:
            return self._text

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def dropped_protocol_chunks(self) -> int:
        with self._lock:
            return self._dropped_protocol_chunks

    def feed(self, chunk) -> bool:
        """Append one model-text chunk. Returns True when accepted.

        Rejects non-string chunks, empty chunks, chunks after close, and
        protocol-JSON-shaped chunks (counted, never forwarded). Truncates
        at MAX_PARTIAL_CHARS instead of raising, so a long reply still
        streams its beginning.
        """
        if self.closed or not isinstance(chunk, str) or not chunk:
            return False
        if _looks_like_protocol_json(chunk):
            with self._lock:
                self._dropped_protocol_chunks += 1
            return False
        with self._lock:
            if self.closed:
                return False
            if self.first_byte_at is None:
                self.first_byte_at = time.monotonic()
            room = MAX_PARTIAL_CHARS - len(self._text)
            if room <= 0:
                return False
            self._text += chunk[:room]
            self._seq += 1
            text, seq = self._text, self._seq
        if self.on_delta is not None:
            try:
                self.on_delta(text, seq)
            except Exception:
                # A display callback must never break the adapter's read loop.
                pass
        return True

    def close(self) -> str:
        """Stop accepting chunks; return the accumulated text."""
        with self._lock:
            self.closed = True
            return self._text
