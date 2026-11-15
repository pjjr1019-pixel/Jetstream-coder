"""Owned, synchronous chat dispatch. No listener, credentials or desktop actions.

Adapters must return the original identity with their reply. Production adapters
are deliberately not registered by importing this module.
"""
from __future__ import annotations

import os
import logging
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from deskhand_mailbox import Mailbox
from deskhand_stream import StreamSink
from deskhand_timing import TimingRecorder


@dataclass(frozen=True)
class Route:
    assistant: str
    model: str
    account: str
    effort: str

    @classmethod
    def of(cls, row):
        return cls(*(row[key] for key in ("assistant", "model", "account", "effort")))


@dataclass(frozen=True)
class Request:
    turn_id: str
    text: str
    route: Route
    agent_task_id: str | None = None


@dataclass(frozen=True)
class Reply:
    turn_id: str
    route: Route
    text: str


class StopConfirmed(Exception):
    """The adapter never started a turn, or observed backend interruption."""


# 100.025 S4 — per-adapter cancel contracts (code-verified, O6 fallback:
# no adapter-owner statement exists, so each contract states exactly what
# the code shows request_stop kills, or declares the adapter uncancellable).
# Never claim a kill capability that does not exist: no adapter kills a
# host exec / agent action already dispatched to the PC.
AGENT_CANCEL_CONTRACTS = {
    # Muse: stops only the wait for the browser to claim the job.
    "muse": "partial: stops only the wait for the browser to claim the job; a request already sent to Muse keeps running",
    # Codex: cooperative turn/interrupt RPC, 15 s confirm deadline.
    "codex": "partial: cooperative turn/interrupt RPC, 15 s confirm deadline, process not killed, server-side outcome unconfirmed",
    # Local (Ollama / OpenAI-compatible): single blocking HTTP call.
    "local": "uncancellable: single blocking HTTP call, no stop handle",
    # Laptop-agent fallback: no cancel endpoint on the agent.
    "fallback": "uncancellable: no cancel endpoint; a STOP mid-action is recorded and the next action is refused",
    # Fixture / test adapters only; never a production path.
    "fixture": "uncancellable (fixture-only)",
}

_ADAPTER_CONTRACT_BY_CLASS = {
    "MuseRelay": "muse",
    "CodexChat": "codex",
    "LocalChat": "local",
    "OpenAILocalChat": "local",
    "TaskAgentClient": "fallback",
}


def _cancel_contract(adapter):
    """Contract string for a running adapter, or None when undeclared.

    HostToolChat wraps a provider, so unwrap to the provider before
    mapping. A lambda / plain-function adapter is fixture-only UNLESS
    it carries a _cancel_contract_kind marker (the live local adapter
    is a lambda registered by ChatService._register_local and is tagged
    "local"; see AGENT_CANCEL_CONTRACTS). An adapter with request_stop
    but no known class has no declared contract (None) — that is
    honest, not cancellable.
    """
    seen = set()
    while adapter is not None and id(adapter) not in seen:
        seen.add(id(adapter))
        provider = getattr(adapter, "provider", None)
        if provider is None or provider is adapter:
            break
        adapter = provider
    if adapter is None:
        return None
    key = _ADAPTER_CONTRACT_BY_CLASS.get(type(adapter).__name__)
    if key is not None:
        return AGENT_CANCEL_CONTRACTS[key]
    # 100.025 fix: the live local adapter is a plain lambda, so class
    # name cannot tell it from a fixture lambda. The local lambda
    # carries a _cancel_contract_kind marker; an untagged lambda still
    # maps to the fixture contract below. The marker is checked after
    # the class-name lookup, so a mapped class stays authoritative.
    kind = getattr(adapter, "_cancel_contract_kind", None)
    if isinstance(kind, str) and kind in AGENT_CANCEL_CONTRACTS:
        return AGENT_CANCEL_CONTRACTS[kind]
    if callable(adapter) and not hasattr(adapter, "request_stop"):
        return AGENT_CANCEL_CONTRACTS["fixture"]
    if not callable(getattr(adapter, "request_stop", None)):
        return AGENT_CANCEL_CONTRACTS["fixture"]
    return None


class NotDispatched(RuntimeError):
    """The adapter confirmed that no external delivery was attempted."""


class DurableProviderError(RuntimeError):
    """A correlated provider returned a terminal error that must be saved."""


class QueueOwner(AbstractContextManager):
    """OS-held lock shared by all dispatchers for this exact database path."""
    def __init__(self, database: Path):
        self.path = Path(database).resolve().with_suffix(".owner.lock")
        self.file = None

    def __enter__(self):
        if self.file is not None:
            raise RuntimeError("owner already entered")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            stream.close()
            raise
        self.file = stream
        return self

    def __exit__(self, *exc):
        if self.file is not None:
            # Closing the handle also releases the lock after a process crash.
            self.file.close()
            self.file = None


class Dispatcher(AbstractContextManager):
    def __init__(self, mailbox: Mailbox, adapters: dict[Route, Callable[[Request], Reply]]):
        self.mailbox = mailbox
        self.adapters = dict(adapters)
        self.owner = QueueOwner(mailbox.path)
        self.gate = threading.Lock()
        self.admission_gate = threading.Lock()
        self.running = None
        self.dispatch_started_at = None
        self.dispatch_agent_task_id = None
        self.idle = threading.Event()
        self.idle.set()
        self.admission_epoch = 0
        self.active = False
        self.stopped = True
        # Batch 2, v100.004: per-route gates for parallel read-only
        # turns, and an in-flight counter shared by the serial and
        # parallel paths so the idle event (and the Muse force-restart
        # guard that reads it) stays honest while any turn runs.
        self._route_gates = {}
        self._route_gate_lock = threading.Lock()
        self._in_flight = 0
        self._route_running = {}
        self.timings = {}

    def __enter__(self):
        with self.gate:
            if self.active:
                raise RuntimeError("dispatcher already entered")
            self.owner.__enter__()
            try:
                self.mailbox.recover_interrupted()
                stopped, epoch = self.mailbox.admission_state()
                if stopped:
                    # Recover stops persisted by older builds that used two
                    # transactions, including a crash between those writes.
                    self.mailbox.persist_stop_and_cancel(epoch)
            except BaseException:
                self.owner.__exit__()
                raise
            self.active = True
            self.stopped = stopped
            self.admission_epoch = epoch
        return self

    def stop(self):
        # Stops admissions; this chat-only dispatcher cannot cancel external work.
        # It must never be used as the desktop STOP authority.
        with self.admission_gate:
            self.stopped = True
            self.admission_epoch += 1

    def shutdown(self):
        """Stop admissions and request interruption without cancelling queued turns."""
        with self.admission_gate:
            self.stopped = True
            self.admission_epoch += 1
            active = []
            if self.running is not None:
                active.append(self.running[1])
            active.extend(info["adapter"] for info in self._route_running.values())
            seen = set()
            for adapter in active:
                if id(adapter) in seen:
                    continue
                seen.add(id(adapter))
                interrupt = getattr(adapter, "request_stop", None)
                if callable(interrupt):
                    try:
                        interrupt()
                    except Exception:
                        logging.exception("Adapter shutdown interrupt failed")

    def admission_ticket(self):
        with self.admission_gate:
            if not self.active or self.stopped:
                raise RuntimeError("Assistant admissions are stopped")
            return self.admission_epoch

    def submit(self, *args, admission_ticket=None, before_admit=None, **kwargs):
        """Admit a chat turn only while admissions are open."""
        with self.admission_gate:
            if not self.active or self.stopped:
                raise RuntimeError("Assistant admissions are stopped")
            if admission_ticket is not None and admission_ticket != self.admission_epoch:
                raise RuntimeError("Submission was cancelled by a stop request")
            if before_admit is not None:
                before_admit()
            return self.mailbox.submit(*args, **kwargs)

    def cancel_unstarted(self):
        """Stop admissions and cancel queued turns, without claiming to interrupt active work."""
        with self.admission_gate:
            self.stopped = True
            self.admission_epoch += 1
            return self.mailbox.persist_stop_and_cancel(self.admission_epoch)

    def request_agent_stop(self):
        """Interrupt active computer work without stopping ordinary chat admission.

        Cancels queued agent turns FIRST (agent-filtered; chat turns and
        admissions untouched), then asks the running agent turn's adapter
        to stop. The interrupt call is guarded: an adapter whose
        request_stop raises reports interrupt_requested False instead of
        raising the op. The returned cancel_contract is the running
        adapter's entry in AGENT_CANCEL_CONTRACTS (None when nothing is
        running, or when the adapter declares no contract). The running
        turn is never force-marked here; its outcome is recorded once,
        by the dispatching thread.
        Returns {"interrupt_requested": bool, "queued_cancelled": [turn_id, ...],
                 "cancel_contract": str|None, "running_turn_id": str|None}.
        """
        with self.admission_gate:
            queued_cancelled = self.mailbox.cancel_queued_agent_turns()
            running = self.running
            running_turn_id = running[0] if running is not None else None
            contract = _cancel_contract(running[1]) if running is not None else None
            if running is None:
                return {"interrupt_requested": False, "queued_cancelled": queued_cancelled,
                        "cancel_contract": None, "running_turn_id": None}
            row = self.mailbox.get(running[0])
            if row is None or row.get('agent_task_id') is None:
                return {"interrupt_requested": False, "queued_cancelled": queued_cancelled,
                        "cancel_contract": contract, "running_turn_id": running_turn_id}
            interrupt = getattr(running[1], 'request_stop', None)
            if callable(interrupt):
                try:
                    interrupt()
                except Exception:
                    return {"interrupt_requested": False, "queued_cancelled": queued_cancelled,
                            "cancel_contract": contract, "running_turn_id": running_turn_id}
                return {"interrupt_requested": True, "queued_cancelled": queued_cancelled,
                        "cancel_contract": contract, "running_turn_id": running_turn_id}
            return {"interrupt_requested": False, "queued_cancelled": queued_cancelled,
                    "cancel_contract": contract, "running_turn_id": running_turn_id}

    def request_chat_stop(self):
        """Stop chat admissions and request interruption; this is not desktop STOP."""
        with self.admission_gate:
            self.stopped = True
            self.admission_epoch += 1
            running = self.running
            requested = False
            error = None
            if running is not None:
                interrupt = getattr(running[1], "request_stop", None)
                if interrupt is not None:
                    try:
                        interrupt()
                        requested = True
                    except Exception as exc:
                        error = str(exc)[:500]
            # Interrupt first, as in the original executor STOP path; a slow or
            # failing journal must not delay the active stop request.
            try:
                cancelled = self.mailbox.persist_stop_and_cancel(self.admission_epoch)
            except Exception as exc:
                cancelled = []
                error = ((error + "; ") if error else "") + "Could not persist stop and cancel queued chat: " + str(exc)[:300]
            return {"queued_cancelled": cancelled, "active_turn": running[0] if running else None,
                    "interrupt_requested": requested, "error": error, "admission_epoch": self.admission_epoch}

    def admission_state(self):
        with self.admission_gate:
            return {"stopped": self.stopped, "epoch": self.admission_epoch}

    def wait_for_idle(self, timeout):
        return self.idle.wait(timeout)

    def resume_chat(self):
        """Resume chat admissions only after the caller performs local authorization."""
        with self.admission_gate:
            if not self.active:
                raise RuntimeError("Assistant service is not active")
            if self.running is not None:
                raise RuntimeError("Assistant work is still active; cannot resume")
            new_epoch = self.admission_epoch + 1
            self.mailbox.set_admission_state(False, new_epoch)
            self.admission_epoch = new_epoch
            self.stopped = False

    def _route_gate(self, route):
        """One lock per route, so read-only turns on different routes
        run in parallel while turns on the same route stay serial."""
        with self._route_gate_lock:
            gate = self._route_gates.get(route)
            if gate is None:
                gate = threading.Lock()
                self._route_gates[route] = gate
            return gate

    def _record_timing(self, turn_id, timing):
        """Keep the last timings in memory and beside the turn row.

        Best-effort: timing must never break a turn's outcome.
        """
        self.timings[turn_id] = timing.as_dict()
        while len(self.timings) > 50:
            self.timings.pop(next(iter(self.timings)))
        try:
            self.mailbox.record_timing(turn_id, timing.as_dict())
        except Exception:
            pass

    def _new_sink(self, request, timing):
        """Stream sink for one turn: partials to the mailbox, first-byte
        into the timing record. Chat text only — the sink itself
        refuses protocol-JSON-shaped chunks."""
        sink = StreamSink(request.turn_id)

        def publish(text, seq):
            if sink.first_byte_at is not None:
                timing.mark("first_byte", at=sink.first_byte_at)
            try:
                self.mailbox.publish_partial(request.turn_id, text, seq)
            except Exception:
                pass

        sink.on_delta = publish
        return sink

    def _run_adapter(self, request, adapter, timing, sink):
        """Run one claimed turn through its adapter (shared by the
        serial and per-route paths). Prefers adapter.stream() when the
        adapter offers it, so the phone renders the reply as it
        arrives; otherwise calls the adapter as before. The final
        reply is still delivered exactly once through Mailbox.finish.
        StopConfirmed -> cancelled, NotDispatched -> failed with the
        error, anything else -> uncertain (delivery may have
        happened; never replayed)."""
        try:
            if adapter is None:
                self.mailbox.finish(request.turn_id, error="Selected route is not configured")
            else:
                stream = getattr(adapter, "stream", None)
                if callable(stream):
                    reply = stream(request, on_delta=sink.feed)
                else:
                    reply = adapter(request)
                timing.mark("adapter_done")
                if not isinstance(reply, Reply) or reply.turn_id != request.turn_id or reply.route != request.route:
                    raise ValueError("reply identity does not match dispatched request")
                if not isinstance(reply.text, str) or not reply.text.strip():
                    raise ValueError("empty reply")
                self.mailbox.finish(request.turn_id, reply=reply.text)
                timing.mark("finished")
                self._record_timing(request.turn_id, timing)
                saved = getattr(adapter, "reply_saved", None)
                if saved is not None:
                    try:
                        saved(request)
                    except Exception:
                        # The Deskhand reply is already durable. Do not
                        # replay or change it if relay cleanup fails.
                        logging.exception("Assistant reply receipt cleanup failed")
        except DurableProviderError as error:
            if request.agent_task_id is not None:
                # A host-tool exchange has a separate identity from the
                # parent turn; do not acknowledge it under the parent ID.
                self.mailbox.uncertain(request.turn_id, str(error)[:1000])
                raise
            self.mailbox.finish(request.turn_id, error=str(error)[:1000])
            saved = getattr(adapter, "reply_saved", None)
            if saved is not None:
                try:
                    saved(request)
                except Exception:
                    logging.exception("Assistant error receipt cleanup failed")
        except StopConfirmed:
            self.mailbox.cancel_active(request.turn_id)
        except NotDispatched as error:
            self.mailbox.finish(request.turn_id, error=str(error)[:1000])
        except BaseException as error:
            self.mailbox.uncertain(request.turn_id, str(error)[:1000])
            raise

    def dispatch_one(self):
        with self.gate:
            if not self.active:
                raise RuntimeError("exclusive queue ownership required")
            with self.admission_gate:
                if self.stopped:
                    return None
                row = self.mailbox.claim()
                if row is None:
                    return None
                request = Request(row["id"], row["text"], Route.of(row), row.get("agent_task_id"))
                adapter = self.adapters.get(request.route)
                prepare = getattr(adapter, "prepare_turn", None)
                if prepare is not None:
                    try:
                        prepare()
                    except Exception as error:
                        self.mailbox.uncertain(request.turn_id, "Could not prepare assistant: " + str(error)[:500])
                        raise
                self._in_flight += 1
                self.idle.clear()
                self.running = (request.turn_id, adapter)
                self.dispatch_started_at = time.monotonic()
                self.dispatch_agent_task_id = request.agent_task_id
            timing = TimingRecorder()
            timing.mark("claimed")
            sink = self._new_sink(request, timing)
            try:
                self._run_adapter(request, adapter, timing, sink)
            finally:
                with self.admission_gate:
                    self.running = None
                    self.dispatch_started_at = None
                    self.dispatch_agent_task_id = None
                    self._in_flight = max(0, self._in_flight - 1)
                    if self._in_flight == 0:
                        self.idle.set()
            return self.mailbox.get(request.turn_id)

    def dispatch_one_for_route(self, route, read_only=True):
        """Dispatch one turn for a single route (Batch 2, v100.004).

        read_only=True (default): only independent read-only chat
        turns run here, in parallel across routes, one at a time per
        route. Anything that can mutate the PC must use the serial
        dispatch_one() path — call with read_only=False to fall back
        to it. Agent-task turns (agent_task_id set) are never
        parallelised: they go through dispatch_one() too.
        """
        if not read_only:
            return self.dispatch_one()
        if not self.active:
            raise RuntimeError("exclusive queue ownership required")
        gate = self._route_gate(route)
        with gate:
            with self.admission_gate:
                if self.stopped:
                    return None
                row = self.mailbox.claim_for_route(route)
                if row is None:
                    return None
                request = Request(row["id"], row["text"], Route.of(row), row.get("agent_task_id"))
                if request.agent_task_id is not None:
                    # Agent turns may propose host actions: serial only.
                    # Put the row back to queued so dispatch_one() picks
                    # it up in order.
                    self.mailbox.requeue(request.turn_id)
                    return None
                adapter = self.adapters.get(request.route)
                prepare = getattr(adapter, "prepare_turn", None)
                if prepare is not None:
                    try:
                        prepare()
                    except Exception as error:
                        self.mailbox.uncertain(request.turn_id, "Could not prepare assistant: " + str(error)[:500])
                        raise
                started_at = time.monotonic()
                self._route_running[request.turn_id] = {
                    "route": request.route, "adapter": adapter,
                    "started_at": started_at, "agent_task_id": None}
                self._in_flight += 1
                self.idle.clear()
            timing = TimingRecorder()
            timing.mark("claimed")
            sink = self._new_sink(request, timing)
            try:
                self._run_adapter(request, adapter, timing, sink)
            finally:
                with self.admission_gate:
                    self._route_running.pop(request.turn_id, None)
                    self._in_flight = max(0, self._in_flight - 1)
                    if self._in_flight == 0:
                        self.idle.set()
            return self.mailbox.get(request.turn_id)

    def running_turn_info(self):
        """Snapshot of the active turn, if any: id, age, agent flag, interrupt support.

        Read-only; never touches the journal. The age is measured from the
        claim, so a turn stuck waiting on a dead reply path keeps ageing
        until its adapter returns or is interrupted.
        """
        with self.admission_gate:
            if self.running is not None and self.dispatch_started_at is not None:
                adapter = self.running[1]
                return {"turn_id": self.running[0],
                        "age_secs": time.monotonic() - self.dispatch_started_at,
                        "agent_task_id": self.dispatch_agent_task_id,
                        "interruptible": callable(getattr(adapter, "request_stop", None))}
            # Batch 2: a parallel read-only turn is visible to the
            # watchdog too (oldest one, if several are running).
            if self._route_running:
                turn_id, info = min(self._route_running.items(), key=lambda kv: kv[1]["started_at"])
                return {"turn_id": turn_id,
                        "age_secs": time.monotonic() - info["started_at"],
                        "agent_task_id": info.get("agent_task_id"),
                        "interruptible": callable(getattr(info["adapter"], "request_stop", None))}
            return None

    def interrupt_stale(self, timeout_secs, *, include_agent_turns=False):
        """Interrupt a turn that has been dispatching longer than timeout_secs.

        Interrupt-only: this never writes the turn row itself. The owning
        dispatch thread records the outcome through the existing paths
        (cancelled when the adapter confirms nothing started, unknown when
        delivery may have begun), so a stale turn is never replayed and its
        journal entry is written exactly once, by the dispatcher thread.
        Agent turns are skipped unless include_agent_turns is set: host
        actions may legitimately run long, and interrupting one mid-action
        needs an explicit caller decision, not a watchdog default.
        Returns a small dict describing what (if anything) was done.
        """
        with self.admission_gate:
            if self.running is not None and self.dispatch_started_at is not None:
                turn_id, adapter = self.running
                age = time.monotonic() - self.dispatch_started_at
                agent_task_id = self.dispatch_agent_task_id
            elif self._route_running:
                # Batch 2: same stale check for a parallel read-only turn.
                turn_id, info = min(self._route_running.items(), key=lambda kv: kv[1]["started_at"])
                adapter = info["adapter"]
                age = time.monotonic() - info["started_at"]
                agent_task_id = info.get("agent_task_id")
            else:
                return {"stale": False}
        if age <= timeout_secs:
            return {"stale": False, "turn_id": turn_id, "age_secs": age}
        if agent_task_id is not None and not include_agent_turns:
            return {"stale": True, "turn_id": turn_id, "age_secs": age,
                    "skipped": "agent_turn", "interrupt_requested": False}
        interrupt = getattr(adapter, "request_stop", None)
        if not callable(interrupt):
            return {"stale": True, "turn_id": turn_id, "age_secs": age,
                    "interrupt_requested": False, "error": "adapter cannot be interrupted"}
        try:
            interrupt()
        except Exception as exc:
            return {"stale": True, "turn_id": turn_id, "age_secs": age,
                    "interrupt_requested": False, "error": str(exc)[:300]}
        return {"stale": True, "turn_id": turn_id, "age_secs": age,
                "interrupt_requested": True}

    def __exit__(self, *exc):
        self.stop()
        with self.gate:
            self.active = False
            self.owner.__exit__(*exc)
