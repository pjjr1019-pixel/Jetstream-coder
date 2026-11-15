"""Optional local task-pipeline adapter.

The standalone app deliberately does not treat an instruction as shell input.
An embedding host may register a task handler that applies its own review and
approval workflow; without one, task submissions are explicitly refused.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
import hashlib
from typing import Any, Awaitable, Callable

from .redaction import contains_sensitive

LOG = logging.getLogger("musebridge.tasks")

TaskHandler = Callable[[str, str, int], Awaitable[dict[str, Any]]]
MAX_RETAINED_TASKS = 1000


class TaskPipeline:
    def __init__(self, handler: TaskHandler | None = None, store=None):
        if handler is not None and not asyncio.iscoroutinefunction(handler):
            raise TypeError("Task pipeline handlers must be asynchronous and cancellation-aware")
        self.handler = handler
        self.store = store
        self.tasks: dict[str, dict[str, Any]] = store.load_tasks() if store else {}
        self.by_request: dict[str, str] = {row["request_id"]: identifier for identifier, row in self.tasks.items()}
        self.workers: dict[str, asyncio.Task] = {}

    async def submit(self, request_id: str, task: str, scope: str, budget_secs: int) -> dict[str, Any]:
        if self.handler is None:
            raise ValueError("Task pipeline is not configured in this standalone build")
        fingerprint = hashlib.sha256(f"{task}\0{scope}\0{budget_secs}".encode()).hexdigest()
        if request_id in self.by_request:
            task_id = self.by_request[request_id]
            record = self.tasks[task_id]
            if record["fingerprint"] != fingerprint:
                raise ValueError("Request identity reused")
            return {"task_id": task_id}
        for identifier, record in list(self.tasks.items()):
            worker = self.workers.get(identifier)
            if worker is not None and worker.done():
                self.workers.pop(identifier, None)
            if record["state"] in {"complete", "failed", "cancelled"} and (len(self.tasks) >= MAX_RETAINED_TASKS or record["created"] < time.time()-86400):
                self.tasks.pop(identifier)
                self.by_request.pop(record["request_id"], None)
        if sum(record["state"] in ("accepted", "running") for record in self.tasks.values()) >= 32:
            raise ValueError("Task queue is full")
        task_id = str(uuid.uuid4())
        record = {"task_id": task_id, "request_id": request_id, "fingerprint": fingerprint,
                  "state": "accepted", "created": time.time(), "result": None}
        self.tasks[task_id] = record
        self.by_request[request_id] = task_id
        if self.store: await asyncio.to_thread(self.store.save_task, record)
        self.workers[task_id] = asyncio.create_task(self._run(task_id, task, scope, budget_secs))
        return {"task_id": task_id}

    async def _run(self, task_id: str, task: str, scope: str, budget_secs: int) -> None:
        record = self.tasks[task_id]
        record["state"] = "running"
        try:
            result = await asyncio.wait_for(
                self.handler(task, scope, budget_secs), timeout=budget_secs
            )
            result_text = result.get("result", "") if isinstance(result, dict) else None
            evidence = result.get("evidence") if isinstance(result, dict) else None
            if (not isinstance(result, dict)
                    or type(result.get("ok")) is not bool
                    or not isinstance(result_text, str)
                    or (evidence is not None and not isinstance(evidence, str))
                    or (result.get("ok") and (not isinstance(evidence, str) or not evidence.strip()))):
                raise ValueError("Task pipeline returned no verifiable evidence")
            if len(evidence or "") > 100000 or len(result_text) > 24000:
                raise ValueError("Task output exceeded the output limit")
            if contains_sensitive(result_text) or (evidence and contains_sensitive(evidence)):
                raise ValueError("Task result may contain sensitive data")
            record.update(state="complete", result={
                "task_id": task_id, "ok": bool(result.get("ok")),
                "result": result_text,
                "evidence": evidence,
            })
        except asyncio.CancelledError:
            record.update(state="cancelled", result={
                "task_id": task_id, "ok": False, "cancelled": True,
                "result": "Task cancelled",
            })
            raise
        except Exception as exc:
            LOG.error("Task pipeline failed (%s)", type(exc).__name__)
            record.update(state="failed", result={
                "task_id": task_id, "ok": False, "result": "Task failed; inspect local logs.",
            })
        finally:
            if self.store: await asyncio.to_thread(self.store.save_task, record)

    async def cancel(self, task_id: str) -> bool:
        task = self.workers.get(task_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            pass
        return self.tasks[task_id]["state"] == "cancelled"

    async def close(self) -> None:
        pending = [task for task in self.workers.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2)
