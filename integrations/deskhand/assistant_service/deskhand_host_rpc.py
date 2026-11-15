"""Bounded request/reply channel to the owning Windows process.

Transport only: Windows must independently bind calls to an admitted task and
apply all permissions, STOP and durable action accounting. No call is retried.
"""
import threading
import uuid
from concurrent.futures import Future, TimeoutError


class HostRpc:
    def __init__(self, emit, timeout=30, action_timeout=None):
        if not 0 < timeout <= 120:
            raise ValueError("invalid host timeout")
        if action_timeout is not None and not 0 < action_timeout <= 180:
            raise ValueError("invalid action timeout")
        self.emit, self.timeout, self.action_timeout = emit, timeout, action_timeout or timeout
        self.lock = threading.Lock()
        self.pending = {}
        self.closed = False

    def call(self, operation, payload):
        if operation not in {"task.begin", "task.check", "action.execute", "task.end", "health.snapshot", "fixture.reserve", "fixture.ack", "fixture.audit", "fixture.verify", "fixture.pending", "fixture.recover", "fixture.recover_ack"} or not isinstance(payload, dict):
            raise ValueError("invalid host request")
        request_id = str(uuid.uuid4())
        future = Future()
        with self.lock:
            if self.closed:
                raise RuntimeError("host connection closed")
            if len(self.pending) >= 8:
                raise RuntimeError("host connection busy")
            self.pending[request_id] = future
        try:
            self.emit({"host_call": request_id, "operation": operation, "payload": payload})
            try:
                return future.result(self.action_timeout if operation == "action.execute" else self.timeout)
            except TimeoutError:
                raise TimeoutError("Host did not confirm action; outcome is uncertain") from None
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def receive(self, request_id, result):
        with self.lock:
            future = self.pending.get(request_id)
            if future is None or future.done():
                return False
            future.set_result(result)
            return True

    def close(self):
        with self.lock:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("host connection closed; outcome is uncertain"))
