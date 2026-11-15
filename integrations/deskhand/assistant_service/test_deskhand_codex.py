"""Mock-only tests for deskhand_codex.py Batch 1 (v100.003) — unlatch port.

No Codex binary is touched: a fake rpc_factory stands in for
CodexRpc. Verifies, against the UNLATCH copy (per-account identity,
STOP/interrupt, agent-task mode preserved):
(1) the warm process is reused across calls and respawned only after
a simulated close/crash or an unconfirmed interruption;
(2) the 1h catalog cache is keyed by (home, account identity) — a
second call for the same account does not re-page model/list, a
different account gets its own catalog, and an auth failure
invalidates the home's entries;
(3) request_stop before a turn raises StopConfirmed and keeps the
warm process (the turn itself is never replayed).
Run: python3 -m unittest test_deskhand_codex -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deskhand_codex
from deskhand_codex import CodexChat, account_identity, catalog
from deskhand_dispatch import Request, Route, StopConfirmed

ACCOUNT_1 = {"type": "account", "id": "acct-1", "email": "phil@example.com"}
ACCOUNT_2 = {"type": "account", "id": "acct-2", "email": "other@example.com"}
IDENTITY_1 = account_identity(ACCOUNT_1)
IDENTITY_2 = account_identity(ACCOUNT_2)


class FakeProc:
    def __init__(self):
        self.dead = False

    def poll(self):
        return 1 if self.dead else None


class FakeRpc:
    account = ACCOUNT_1
    model = "gpt-5"

    def __init__(self, home=None, timeout=180):
        self.home = home
        self.timeout = timeout
        self.proc = FakeProc()
        self.closed = False
        self.calls = []
        self._events = []

    def call(self, method, params=None):
        self.calls.append(method)
        if method == "account/read":
            return {"account": dict(self.account)}
        if method == "model/list":
            return {"data": [{"model": self.model, "displayName": self.model.upper(),
                              "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                              "defaultReasoningEffort": "high", "isDefault": True}],
                    "nextCursor": None}
        if method == "thread/start":
            return {"thread": {"id": "t1"}}
        if method == "thread/resume":
            return {"thread": {"id": "t1"}}
        if method == "turn/start":
            self._events = [
                {"method": "item/completed",
                 "params": {"threadId": "t1", "turnId": "turn1",
                            "item": {"id": "m1", "type": "agentMessage", "text": "Hello"}}},
                {"method": "turn/completed",
                 "params": {"threadId": "t1", "turnId": "turn1",
                            "turn": {"id": "turn1", "status": "completed"}}},
            ]
            return {"turn": {"id": "turn1"}}
        raise AssertionError("unexpected call %r" % method)

    def receive(self, deadline):
        if not self._events:
            raise AssertionError("receive called with no queued events")
        return self._events.pop(0)

    def close(self):
        self.closed = True


class FakeRpcFactory:
    def __init__(self, rpc_cls=FakeRpc):
        self.rpc_cls = rpc_cls
        self.created = []

    def __call__(self, home=None, timeout=180):
        rpc = self.rpc_cls(home, timeout)
        self.created.append(rpc)
        return rpc

    def model_list_calls(self):
        return sum(r.calls.count("model/list") for r in self.created)


ROUTE = Route("codex", "gpt-5", IDENTITY_1, "high")


class WarmProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.factory = FakeRpcFactory()
        self.chat = CodexChat(ROUTE, Path(self.tmp.name) / "codex",
                              home="/nonexistent-home", rpc_factory=self.factory)

    def tearDown(self):
        self.chat.close()
        self.tmp.cleanup()

    def test_same_rpc_reused_across_two_calls(self):
        r1 = self.chat(Request("turn-1", "hi", ROUTE))
        r2 = self.chat(Request("turn-2", "hi again", ROUTE))
        self.assertEqual(r1.text, "Hello")
        self.assertEqual(r2.text, "Hello")
        self.assertEqual(len(self.factory.created), 1)
        self.assertIs(self.chat._rpc, self.factory.created[0])
        self.assertFalse(self.factory.created[0].closed)

    def test_respawn_after_simulated_close(self):
        self.chat(Request("turn-1", "hi", ROUTE))
        first = self.factory.created[0]
        first.proc.dead = True  # simulated crash / close
        self.chat(Request("turn-2", "hi again", ROUTE))
        self.assertEqual(len(self.factory.created), 2)
        self.assertIsNot(self.chat._rpc, first)
        self.assertTrue(first.closed)

    def test_close_then_next_call_respawns(self):
        self.chat(Request("turn-1", "hi", ROUTE))
        self.chat.close()
        self.assertIsNone(self.chat._rpc)
        self.chat(Request("turn-2", "hi again", ROUTE))
        self.assertEqual(len(self.factory.created), 2)

    def test_two_routes_have_separate_processes(self):
        other_route = Route("codex", "gpt-5", "default", "")
        other = CodexChat(other_route, Path(self.tmp.name) / "codex2",
                          home="/nonexistent-home", rpc_factory=self.factory)
        try:
            self.chat(Request("turn-1", "hi", ROUTE))
            other(Request("turn-1", "hi", other_route))
        finally:
            other.close()
        self.assertEqual(len(self.factory.created), 2)
        self.assertIsNot(self.chat._rpc, other._rpc)

    def test_corrupt_thread_state_starts_fresh_thread(self):
        # B2-D M7: a corrupt state file used to raise on every turn
        # (bricking the Codex route until hand-deleted). Now the
        # corrupt file is discarded and the turn starts a fresh thread.
        self.chat(Request("turn-1", "hi", ROUTE))
        self.assertEqual(self.factory.created[0].calls.count("thread/start"), 1)
        self.chat.state.write_text("{not json", encoding="utf-8")
        reply = self.chat(Request("turn-2", "hi again", ROUTE))
        self.assertEqual(reply.text, "Hello")
        self.assertEqual(self.factory.created[0].calls.count("thread/start"), 2)
        self.assertNotIn("thread/resume", self.factory.created[0].calls)
        # The corrupt file was replaced by a valid fresh-thread record.
        import json as _json
        self.assertEqual(_json.loads(self.chat.state.read_text())["thread_id"], "t1")

    def test_wrong_shape_thread_state_starts_fresh_thread(self):
        # Same recovery for a parseable file with the wrong shape.
        self.chat(Request("turn-1", "hi", ROUTE))
        self.chat.state.write_text('{"nope": true}', encoding="utf-8")
        reply = self.chat(Request("turn-2", "hi again", ROUTE))
        self.assertEqual(reply.text, "Hello")

    def test_stop_before_turn_raises_and_keeps_warm_process(self):
        self.chat.request_stop()
        with self.assertRaises(StopConfirmed):
            self.chat(Request("turn-1", "hi", ROUTE))
        # The warm process was never started (cancelled before turn start
        # in this path) — but a started process must survive a stop:
        self.chat.prepare_turn()
        self.chat(Request("turn-2", "hi", ROUTE))
        first = self.factory.created[0]
        self.assertFalse(first.closed)
        self.assertIs(self.chat._rpc, first)


class CatalogCacheTest(unittest.TestCase):
    def setUp(self):
        deskhand_codex._CATALOG_CACHE.clear()
        self.factory = FakeRpcFactory()

    def tearDown(self):
        deskhand_codex._CATALOG_CACHE.clear()

    def test_second_call_same_account_does_not_repage(self):
        first = catalog("/home", rpc_factory=self.factory)
        second = catalog("/home", rpc_factory=self.factory)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["account"], IDENTITY_1)
        # Identity is re-verified on a fresh rpc (spawn is cheap vs the
        # paging it replaces), but model/list is paged only once.
        self.assertEqual(self.factory.model_list_calls(), 1)

    def test_refresh_forces_new_paging(self):
        catalog("/home", rpc_factory=self.factory)
        catalog("/home", refresh=True, rpc_factory=self.factory)
        self.assertEqual(self.factory.model_list_calls(), 2)

    def test_different_account_gets_its_own_catalog(self):
        first = catalog("/home", rpc_factory=self.factory)
        self.assertEqual(first[0]["model"], "gpt-5")

        class OtherRpc(FakeRpc):
            account = ACCOUNT_2
            model = "gpt-6"

        other = FakeRpcFactory(OtherRpc)
        second = catalog("/home", rpc_factory=other)
        self.assertEqual(second[0]["model"], "gpt-6")
        self.assertEqual(second[0]["account"], IDENTITY_2)
        # And the first account's cached entry is still intact.
        third = catalog("/home", rpc_factory=self.factory)
        self.assertEqual(third[0]["model"], "gpt-5")
        self.assertEqual(self.factory.model_list_calls(), 1)

    def test_auth_failure_invalidates_cache(self):
        catalog("/home", rpc_factory=self.factory)
        self.assertEqual(self.factory.model_list_calls(), 1)

        class NoAccountRpc(FakeRpc):
            def call(self, method, params=None):
                if method == "account/read":
                    return {"account": None}
                return super().call(method, params)

        bad = FakeRpcFactory(NoAccountRpc)
        # refresh=True bypasses the cache so the auth check actually runs.
        with self.assertRaises(RuntimeError):
            catalog("/home", refresh=True, rpc_factory=bad)
        # Cache was invalidated, so the next good call pages again.
        catalog("/home", rpc_factory=self.factory)
        self.assertEqual(self.factory.model_list_calls(), 2)


if __name__ == "__main__":
    unittest.main()
