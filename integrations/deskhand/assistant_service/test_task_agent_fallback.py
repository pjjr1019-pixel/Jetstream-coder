"""Mock tests for deskhand_task_agent_op.py + the handle() branch.

Runs entirely in the VM. No PC, no real token, no network beyond
loopback (one fake bridge-operator server for bridge_stop_allows).
The fake client never touches a real agent.

Run: python3 test_task_agent_fallback.py
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deskhand_task_agent import TaskAgentUnavailable  # noqa: E402
from deskhand_task_agent_op import (  # noqa: E402
    TaskAgentFallbackRefused,
    bridge_stop_allows,
    read_deskhand_grant,
    run_task_agent_action,
)

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
    except Exception as error:
        FAILED.append(name)
        print("FAIL %s: %s: %s" % (name, type(error).__name__, error))
    else:
        PASSED.append(name)
        print("PASS %s" % name)


class FakeClient:
    """TaskAgentClient stand-in: records run_action calls."""

    def __init__(self, fail_with=None, result=None):
        self.calls = []
        self.fail_with = fail_with
        self.result = result

    def run_action(self, action):
        self.calls.append(action)
        if self.fail_with is not None:
            raise self.fail_with
        if self.result is not None:
            return self.result
        kind = action.get("kind")
        if kind in ("open_app", "open_notepad"):
            return {"ok": True, "kind": kind,
                    "result": {"ok": True, "exe": "notepad.exe", "pid": 4242,
                               "title": "Untitled - Notepad"}}
        if kind == "screenshot":
            return {"ok": True, "kind": kind,
                    "result": {"format": "png", "width": 640, "height": 360,
                               "data_b64": "iVBORw0KGgo=",
                               "png_base64": "iVBORw0KGgo=", "bytes": 1234}}
        if kind == "exec":
            return {"ok": True, "kind": kind,
                    "result": {"stdout": "SECRET-OUTPUT", "stderr": "",
                               "exit_code": 0}}
        return {"ok": True, "kind": kind, "result": {"ok": True}}


def allow():
    return True


def make_hooks(client, grant=True, stop_ok=True):
    def host_allows():
        if not stop_ok:
            raise RuntimeError("Host STOP is set; agent fallback refused")
    return {"host_allows": host_allows,
            "grant_reader": lambda d: grant,
            "client_factory": lambda grant_on: client}


def journal_entries(data_dir):
    path = os.path.join(data_dir, "task_agent_fallback.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main():
    # 1. success: open_notepad through the op
    def t_success():
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            out = run_task_agent_action(
                folder, {"task_id": "task-1",
                         "action": {"kind": "open_notepad"},
                         "gateway_state": {"action_routes_available": False}},
                **make_hooks(client))
            assert out["ok"] is True and out["route"] == "task_agent_fallback"
            assert out["task_id"] == "task-1" and out["kind"] == "open_notepad"
            assert out["result"]["result"]["pid"] == 4242
            assert len(client.calls) == 1
            rows = journal_entries(folder)
            assert len(rows) == 1 and rows[0]["outcome"] == "executed"
            assert rows[0]["task_id"] == "task-1"
            assert rows[0]["route"] == "task_agent_fallback"
            assert rows[0]["gateway_state"]["action_routes_available"] is False
            assert rows[0]["detail"]["pid"] == 4242
    check("success open_notepad + journal", t_success)

    # 2. grant off -> refused, client never called, journal refused
    def t_grant_off():
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-2",
                             "action": {"kind": "open_notepad"}},
                    **make_hooks(client, grant=False))
                raise AssertionError("expected refusal")
            except TaskAgentFallbackRefused as error:
                assert "Full PC access is off" in str(error)
            assert client.calls == []
            rows = journal_entries(folder)
            assert len(rows) == 1 and rows[0]["outcome"] == "refused"
    check("grant off -> refused, no agent call", t_grant_off)

    # 3. STOP set -> refused, client never called
    def t_stop():
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-3",
                             "action": {"kind": "open_notepad"}},
                    **make_hooks(client, stop_ok=False))
                raise AssertionError("expected refusal")
            except TaskAgentFallbackRefused as error:
                assert "STOP" in str(error)
            assert client.calls == []
            rows = journal_entries(folder)
            assert len(rows) == 1 and rows[0]["outcome"] == "refused"
    check("STOP set -> refused, no agent call", t_stop)

    # 4. default STOP check fails closed in the VM (no vendor module)
    def t_default_stop_closed():
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-4",
                             "action": {"kind": "open_notepad"}},
                    grant_reader=lambda d: True,
                    client_factory=lambda grant_on: client)
                raise AssertionError("expected refusal")
            except TaskAgentFallbackRefused as error:
                assert "STOP authority is unavailable" in str(error)
            assert client.calls == []
    check("default STOP check fails closed without vendor module", t_default_stop_closed)

    # 5. wrong kinds -> refused, client never called
    def t_wrong_kinds():
        for kind in ("type", "click", "key", "desktop_type",
                     "desktop_click", "ui_invoke", "window_activate"):
            client = FakeClient()
            with tempfile.TemporaryDirectory() as folder:
                try:
                    run_task_agent_action(
                        folder, {"task_id": "task-5-" + kind,
                                 "action": {"kind": kind, "text": "hi"}},
                        **make_hooks(client))
                    raise AssertionError("expected refusal for " + kind)
                except TaskAgentFallbackRefused as error:
                    assert "gateway worker" in str(error), kind
                assert client.calls == [], kind
    check("type/click/key/window kinds refused", t_wrong_kinds)

    # 6. unknown kind -> refused
    def t_unknown_kind():
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-6",
                             "action": {"kind": "teleport"}},
                    **make_hooks(client))
                raise AssertionError("expected refusal")
            except TaskAgentFallbackRefused as error:
                assert "unknown action kind" in str(error)
            assert client.calls == []
    check("unknown kind refused", t_unknown_kind)

    # 7. missing task ID -> refused
    def t_missing_task_id():
        for cmd in ({}, {"action": {"kind": "open_notepad"}},
                    {"task_id": "", "action": {"kind": "open_notepad"}},
                    {"task_id": None, "action": {"kind": "open_notepad"}}):
            client = FakeClient()
            with tempfile.TemporaryDirectory() as folder:
                try:
                    run_task_agent_action(folder, cmd, **make_hooks(client))
                    raise AssertionError("expected refusal for %r" % (cmd,))
                except TaskAgentFallbackRefused as error:
                    assert "task/turn ID is required" in str(error)
                assert client.calls == []
    check("missing task ID refused", t_missing_task_id)

    # 8. duplicate task ID after executed -> refused, no second run
    def t_duplicate():
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            hooks = make_hooks(client)
            run_task_agent_action(
                folder, {"task_id": "task-8",
                         "action": {"kind": "open_notepad"}}, **hooks)
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-8",
                             "action": {"kind": "open_notepad"}}, **hooks)
                raise AssertionError("expected duplicate refusal")
            except TaskAgentFallbackRefused as error:
                assert "already executed" in str(error)
            assert len(client.calls) == 1
    check("duplicate task ID refused, no re-execution", t_duplicate)

    # 9. retry after a refusal is allowed (refusal does not execute)
    def t_retry_after_refusal():
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-9",
                             "action": {"kind": "open_notepad"}},
                    **make_hooks(client, grant=False))
            except TaskAgentFallbackRefused:
                pass
            out = run_task_agent_action(
                folder, {"task_id": "task-9",
                         "action": {"kind": "open_notepad"}},
                **make_hooks(client, grant=True))
            assert out["ok"] is True
            assert len(client.calls) == 1
            rows = journal_entries(folder)
            assert [r["outcome"] for r in rows] == ["refused", "executed"]
    check("retry after refusal allowed", t_retry_after_refusal)

    # 10. agent down DURING/AFTER dispatch -> outcome UNCERTAIN (v2).
    # v1 journaled this as "failed", but an acting-call failure cannot
    # prove the request was never sent (timeout vs refused are not
    # distinguishable at the op level), so v2 settles the claim
    # "uncertain" and blocks retry until an operator resolves it.
    # (The agent-down-before-dispatch case -- the health check inside
    # the client's gate check failing -- is TaskAgentHealthUnavailable
    # and still settles "failed"; see test 16.)
    def t_agent_down():
        client = FakeClient(fail_with=TaskAgentUnavailable("agent not reachable"))
        with tempfile.TemporaryDirectory() as folder:
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-10",
                             "action": {"kind": "open_notepad"}},
                    **make_hooks(client))
                raise AssertionError("expected TaskAgentUnavailable")
            except TaskAgentUnavailable:
                pass
            assert len(client.calls) == 1
            rows = journal_entries(folder)
            assert len(rows) == 1 and rows[0]["outcome"] == "uncertain"
            assert "TaskAgentUnavailable" in rows[0]["detail"]
            assert rows[0]["claim_state"] == "uncertain"
            # Retry is refused while the claim is uncertain.
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-10",
                             "action": {"kind": "open_notepad"}},
                    **make_hooks(client))
                raise AssertionError("expected refusal while uncertain")
            except TaskAgentFallbackRefused as error:
                assert "uncertain" in str(error)
            assert len(client.calls) == 1
    check("agent down -> uncertain, one attempt, no replay", t_agent_down)

    # 11. screenshot receipt journals dimensions only, no image data
    def t_screenshot_journal():
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            out = run_task_agent_action(
                folder, {"task_id": "task-11",
                         "action": {"kind": "screenshot"}},
                **make_hooks(client))
            assert out["result"]["result"]["bytes"] == 1234
            raw = open(os.path.join(folder, "task_agent_fallback.jsonl"),
                       encoding="utf-8").read()
            assert "iVBORw0KGgo" not in raw
            assert "data_b64" not in raw
            rows = journal_entries(folder)
            assert rows[0]["detail"] == {"width": 640, "height": 360,
                                         "bytes": 1234}
    check("screenshot journal has no image data", t_screenshot_journal)

    # 12. exec receipt journals exit code + length only, no stdout
    def t_exec_journal():
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            run_task_agent_action(
                folder, {"task_id": "task-12",
                         "action": {"kind": "exec", "command": "echo hi"}},
                **make_hooks(client))
            raw = open(os.path.join(folder, "task_agent_fallback.jsonl"),
                       encoding="utf-8").read()
            assert "SECRET-OUTPUT" not in raw
            rows = journal_entries(folder)
            assert rows[0]["detail"] == {"exit_code": 0, "stdout_len": 13}
    check("exec journal has no stdout content", t_exec_journal)

    # 13. handle() branch: success with injected hooks; wrong kind
    # refused with no hooks; unknown op still raises.
    def t_handle_branch():
        from deskhand_service import ChatService
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True,
                                  task_agent_hooks=make_hooks(client))
            try:
                out = service.handle(
                    {"op": "task_agent_action", "task_id": "task-13",
                     "action": {"kind": "open_notepad"}})
                assert out["route"] == "task_agent_fallback"
                assert len(client.calls) == 1
                try:
                    service.handle({"op": "no_such_op"})
                    raise AssertionError("expected ValueError")
                except ValueError as error:
                    assert "Unknown chat operation" in str(error)
            finally:
                service.close()
        # No hooks: wrong kind is refused before the STOP check runs.
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                try:
                    service.handle(
                        {"op": "task_agent_action", "task_id": "task-13b",
                         "action": {"kind": "type", "text": "x"}})
                    raise AssertionError("expected refusal")
                except TaskAgentFallbackRefused as error:
                    assert "gateway worker" in str(error)
            finally:
                service.close()
    check("handle() task_agent_action branch", t_handle_branch)

    # 14. grant reader against a real config.json layout
    def t_grant_reader():
        with tempfile.TemporaryDirectory() as folder:
            deskhand = os.path.join(folder, "Deskhand")
            data = os.path.join(deskhand, "assistant")
            os.makedirs(data)
            cfg = os.path.join(deskhand, "config.json")
            assert read_deskhand_grant(data) is False  # missing
            with open(cfg, "w") as handle:
                json.dump({"FullPcAccess": {"Enabled": True},
                           "Paused": False}, handle)
            assert read_deskhand_grant(data) is True
            with open(cfg, "w") as handle:
                json.dump({"FullPcAccess": {"Enabled": True},
                           "Paused": True}, handle)
            assert read_deskhand_grant(data) is False
            with open(cfg, "w") as handle:
                json.dump({"FullPcAccess": {"Enabled": False},
                           "Paused": False}, handle)
            assert read_deskhand_grant(data) is False
            with open(cfg, "w") as handle:
                handle.write("{not json")
            assert read_deskhand_grant(data) is False
    check("grant reader from config.json", t_grant_reader)

    # 15. bridge_stop_allows against a fake operator endpoint.
    # STOP removed (Phil 2026-11-09): the check verifies the bridge
    # operator state is readable/reachable only. No STOP field is
    # read: a state without one (or with one set, as a stale vendor
    # bridge might still report) is allowed, and an unreadable
    # runtime/state still refuses.
    def t_bridge_stop():
        class Handler(BaseHTTPRequestHandler):
            latched = False

            def do_GET(self):
                assert self.headers.get("Authorization") == "Bearer op-secret"
                body = json.dumps({"stop_latched": Handler.latched}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                home = os.path.join(folder, "muse-runtime", "bridge")
                os.makedirs(home)
                with open(os.path.join(home, "runtime.json"), "w") as handle:
                    json.dump({"operator_port": srv.server_address[1],
                               "pid": 1}, handle)
                with open(os.path.join(home, "operator.secret"), "w") as handle:
                    handle.write("op-secret")
                Handler.latched = False
                assert bridge_stop_allows(folder) is True
                Handler.latched = True
                # A STOP field, even set, is ignored after removal.
                assert bridge_stop_allows(folder) is True
            # Unreadable runtime: refuses.
            with tempfile.TemporaryDirectory() as folder:
                try:
                    bridge_stop_allows(folder)
                    raise AssertionError("expected refusal when unreadable")
                except RuntimeError as error:
                    assert "cannot verify bridge state" in str(error)
        finally:
            srv.shutdown()
    check("bridge_stop_allows via fake operator", t_bridge_stop)

    # 16. (v2) health-check failure before dispatch -> "failed", and a
    # retry of the same payload is allowed once the agent is back.
    # The gate check runs before any acting request, so a health
    # failure proves the action was never sent.
    def t_health_down_failed_retry_allowed():
        from deskhand_task_agent import TaskAgentHealthUnavailable
        client = FakeClient(
            fail_with=TaskAgentHealthUnavailable("health check failed"))
        with tempfile.TemporaryDirectory() as folder:
            hooks = make_hooks(client)
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-16",
                             "action": {"kind": "open_notepad"}}, **hooks)
                raise AssertionError("expected TaskAgentHealthUnavailable")
            except TaskAgentHealthUnavailable:
                pass
            rows = journal_entries(folder)
            assert len(rows) == 1 and rows[0]["outcome"] == "failed"
            assert rows[0]["claim_state"] == "failed"
            out = run_task_agent_action(
                folder, {"task_id": "task-16",
                         "action": {"kind": "open_notepad"}},
                **make_hooks(FakeClient()))
            assert out["ok"] is True
    check("health-check failure -> failed, retry allowed", t_health_down_failed_retry_allowed)

    # 17. (B2-D H13) a failed claim settle after dispatch is SURFACED,
    # never swallowed into a journal line. The fake client corrupts
    # the claim file mid-dispatch, so the settle cannot read it.
    def t_settle_failure_after_dispatch_surfaces():
        from deskhand_action_claim import _claim_filename
        from deskhand_task_agent import TaskAgentUnavailable

        class CorruptingClient(FakeClient):
            def __init__(self, folder, task_id, fail_with=None):
                super().__init__(fail_with=fail_with)
                self._claim_path = os.path.join(
                    folder, "task_agent_claims",
                    _claim_filename(task_id))

            def run_action(self, action):
                # Corrupt the claim file, then run (or fail) as usual.
                with open(self._claim_path, "w", encoding="utf-8") as handle:
                    handle.write("{corrupt")
                return super().run_action(action)

        # (a) success path: the action EXECUTED but the settle failed.
        with tempfile.TemporaryDirectory() as folder:
            client = CorruptingClient(folder, "task-17a")
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-17a",
                             "action": {"kind": "open_notepad"}},
                    **make_hooks(client))
                raise AssertionError("expected settle-failure RuntimeError")
            except RuntimeError as error:
                assert "claim settle failed" in str(error)
                assert "EXECUTED" in str(error)
                assert "do not retry" in str(error)
            assert len(client.calls) == 1
            rows = journal_entries(folder)
            assert any(r["outcome"] == "executed"
                       and "claim settle failed" in str(r.get("detail", ""))
                       for r in rows)
        # (b) failed-dispatch path: both errors are reported, and the
        # claim stays "claimed" (no silent "failed" retry story).
        with tempfile.TemporaryDirectory() as folder:
            client = CorruptingClient(
                folder, "task-17b",
                fail_with=TaskAgentUnavailable("agent not reachable"))
            try:
                run_task_agent_action(
                    folder, {"task_id": "task-17b",
                             "action": {"kind": "open_notepad"}},
                    **make_hooks(client))
                raise AssertionError("expected settle-failure RuntimeError")
            except RuntimeError as error:
                assert "claim settle failed" in str(error)
                assert "TaskAgentUnavailable" in str(error)
            rows = journal_entries(folder)
            assert any(r["claim_state"] == "claimed" for r in rows)
    check("settle failure after dispatch surfaces (H13)", t_settle_failure_after_dispatch_surfaces)

    # 18. (B2-D M4) the client factory receives the LIVE grant state,
    # not a hardcoded True: the client's own grant gate is real.
    def t_factory_receives_live_grant():
        seen = {}

        class GrantCapturingClient(FakeClient):
            pass

        def factory(grant_on):
            seen["grant_on"] = grant_on
            return FakeClient()

        with tempfile.TemporaryDirectory() as folder:
            hooks = make_hooks(FakeClient())
            hooks["client_factory"] = factory
            out = run_task_agent_action(
                folder, {"task_id": "task-18",
                         "action": {"kind": "open_notepad"}}, **hooks)
            assert out["ok"] is True
            assert seen["grant_on"] is True
    check("client factory receives live grant (M4)", t_factory_receives_live_grant)

    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
