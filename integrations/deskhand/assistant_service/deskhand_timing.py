"""Per-hop reply timing (Batch 2, v100.004) — measurement only.

Records monotonic timestamps for one turn: submitted (mailbox row
created), claimed (dispatcher took it), first_byte (first streamed
model text), adapter_done (adapter returned), finished (mailbox
finished). Durations are derived, never guessed.

Heartbeat / lease / reconnect constants are deliberately NOT changed
here. The report's item #17 is "measure first, then tune": this module
is the measurement. Any future constant change needs the named live
load tests (a slow bridge must not be killed just as it recovers).

Timing data is diagnostics, never authority: it is stored beside the
turn, surfaced in the runtime stream / timing op, and never used to
replay, re-route, or re-issue a turn.
"""
from __future__ import annotations

import time

HOPS = ("submitted", "claimed", "first_byte", "adapter_done", "finished")


class TimingRecorder:
    """Monotonic per-turn hop timestamps (process-local)."""

    def __init__(self):
        self._stamps = {}

    def mark(self, hop: str, at: float | None = None) -> None:
        if hop not in HOPS:
            raise ValueError("unknown timing hop: %r" % hop)
        if hop not in self._stamps:
            self._stamps[hop] = time.monotonic() if at is None else float(at)

    def as_dict(self) -> dict:
        stamps = dict(self._stamps)
        out = dict(stamps)
        if "submitted" in stamps and "finished" in stamps:
            out["total_secs"] = round(stamps["finished"] - stamps["submitted"], 3)
        if "claimed" in stamps and "first_byte" in stamps:
            out["time_to_first_byte_secs"] = round(stamps["first_byte"] - stamps["claimed"], 3)
        if "first_byte" in stamps and "adapter_done" in stamps:
            out["generation_secs"] = round(stamps["adapter_done"] - stamps["first_byte"], 3)
        return out
