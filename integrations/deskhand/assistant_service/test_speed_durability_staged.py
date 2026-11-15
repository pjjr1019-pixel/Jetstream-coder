"""Regression checks for the staged, non-deployed speed fixes."""
import unittest
import tempfile
import uuid
import threading
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from deskhand_dispatch import Dispatcher, Request, Route
from deskhand_tunnel import TunnelError, TunnelHttpClient, TunnelProvider
from deskhand_failfast import evaluate_gate
from deskhand_mailbox import Mailbox


class TunnelEndpointTests(unittest.TestCase):
    def test_remote_or_malformed_agent_url_disables_tunnel_and_rejects_client(self):
        import json
        from deskhand_tunnel import DEFAULT_AGENT_URL, load_tunnel_config
        cases = (
            "https://127.0.0.1:8899",
            "http://localhost:8899",
            "http://127.0.0.1@example.invalid:8899",
            "http://example.invalid:8899",
            "http://127.0.0.1:8899/chat",
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "deskhand_tunnel_config.json"
            for endpoint in cases:
                with self.subTest(endpoint=endpoint):
                    config.write_text(json.dumps({
                        "enabled": True, "agent_url": endpoint,
                    }), encoding="utf-8")
                    loaded = load_tunnel_config(directory)
                    self.assertFalse(loaded["enabled"])
                    self.assertEqual(loaded["agent_url"], DEFAULT_AGENT_URL)
                    self.assertIn("agent_url", loaded["configuration_error"])
                    with self.assertRaises(ValueError):
                        TunnelHttpClient(endpoint, "test-token")

    def test_client_does_not_follow_redirects_or_use_environment_proxy(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        import urllib.request

        redirected = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                redirected.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/collect")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with patch.object(urllib.request, "getproxies",
                              return_value={"http": "http://127.0.0.1:1"}) as getproxies:
                client = TunnelHttpClient(
                    f"http://127.0.0.1:{server.server_port}", "test-token", timeout_secs=1)
                getproxies.assert_not_called()
            with self.assertRaisesRegex(TunnelError, "HTTP 302"):
                client._request("GET", "/health")
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()
        self.assertEqual(redirected, ["/health"])

    def test_invalid_remote_config_does_not_redirect_receipt_recovery_to_default(self):
        import deskhand_service
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            box = Mailbox(data / "turns.sqlite")
            turn = str(uuid.uuid4())
            box.submit(turn, "hello", "muse", "m")
            box.claim()
            box.mark_tunnel_dispatch(turn)
            box.recover_interrupted()
            (data / "deskhand_tunnel_config.json").write_text(
                '{"enabled": true, "agent_url": "http://example.invalid:8899"}',
                encoding="utf-8")
            (data / "deskhand_bridge_config.json").write_text(
                '{"token":"test-token"}', encoding="utf-8")
            service = deskhand_service.ChatService.__new__(deskhand_service.ChatService)
            service.data = data
            service.box = box
            service.stopping = threading.Event()
            service._legacy_terminal_cursor = None
            with patch.object(deskhand_service.deskhand_tunnel, "TunnelHttpClient") as factory:
                service._reconcile_tunnel_receipts_once()
            factory.assert_not_called()
            self.assertEqual([row["id"] for row in box.pending_tunnel_receipts()], [turn])

    def test_unavailable_or_malformed_config_holds_pending_receipts(self):
        from contextlib import nullcontext
        import deskhand_service
        cases = (
            ("missing", None, False),
            ("malformed", "{", False),
            ("non-object", "[]", False),
            ("unreadable", '{"enabled": false}', True),
        )
        for label, config_text, unreadable in cases:
            with self.subTest(config=label), tempfile.TemporaryDirectory() as directory:
                data = Path(directory)
                config = data / "deskhand_tunnel_config.json"
                if config_text is not None:
                    config.write_text(config_text, encoding="utf-8")
                (data / "deskhand_bridge_config.json").write_text(
                    '{"token":"test-token"}', encoding="utf-8")
                box = Mailbox(data / "turns.sqlite")
                turn = str(uuid.uuid4())
                box.submit(turn, "hello", "muse", "m")
                box.claim()
                box.mark_tunnel_dispatch(turn)
                box.recover_interrupted()
                service = deskhand_service.ChatService.__new__(deskhand_service.ChatService)
                service.data = data
                service.box = box
                service.stopping = threading.Event()
                service._legacy_terminal_cursor = None
                reader_patch = (patch.object(Path, "read_text",
                                             side_effect=PermissionError("denied"))
                                if unreadable else nullcontext())
                with reader_patch:
                    cfg = deskhand_service.deskhand_tunnel.load_tunnel_config(data)
                    self.assertFalse(cfg["enabled"])
                    self.assertIn("configuration_error", cfg)
                    with patch.object(deskhand_service.deskhand_tunnel,
                                      "TunnelHttpClient") as factory:
                        service._reconcile_tunnel_receipts_once()
                    factory.assert_not_called()
                self.assertEqual([row["id"] for row in box.pending_tunnel_receipts()], [turn])


class FakeClient:
    def __init__(self):
        self.acks = []

    def submit_turn(self, *args, **kwargs):
        return None

    def wait_reply(self, *args):
        return {"turn_id": "turn-1", "reply": "done"}

    def ack_reply(self, turn_id):
        self.acks.append(turn_id)


class TunnelDurabilityTests(unittest.TestCase):
    def test_stop_after_prepare_before_call_never_submits(self):
        from deskhand_dispatch import StopConfirmed
        class Client:
            def submit_turn(self, *args, **kwargs):
                raise AssertionError('must not submit after stop')
        provider = TunnelProvider(Client())
        provider.prepare_turn()
        provider.request_stop()
        with self.assertRaises(StopConfirmed):
            provider(Request('turn', 'hello', Route('muse', 'm', '', '')))

    def test_stop_after_receipt_marker_before_submit_clears_marker(self):
        from deskhand_dispatch import StopConfirmed
        provider = TunnelProvider(FakeClient())
        class Box:
            cleared = []
            def mark_tunnel_dispatch(self, turn_id):
                provider.request_stop()
            def clear_tunnel_receipt(self, turn_id):
                self.cleared.append(turn_id)
        box = Box()
        provider.mailbox = box
        provider.prepare_turn()
        with self.assertRaises(StopConfirmed):
            provider(Request('turn', 'hello', Route('muse', 'm', '', '')))
        self.assertEqual(box.cleared, ['turn'])

    def test_stop_after_submit_boundary_is_uncertain(self):
        from deskhand_tunnel import TunnelError
        provider = TunnelProvider(FakeClient())
        class Client(FakeClient):
            def submit_turn(self, *args, **kwargs):
                self.boundary_observed = provider._submit_started
                provider.request_stop()
                raise TunnelError('response lost after submit began')
        client = Client()
        provider.client = client
        provider.prepare_turn()
        with self.assertRaisesRegex(RuntimeError, 'submit failed'):
            provider(Request('turn', 'hello', Route('muse', 'm', '', '')))
        self.assertTrue(client.boundary_observed)
        self.assertTrue(provider.stopping.is_set())

    def test_concurrent_stop_while_submit_is_in_flight_is_uncertain(self):
        entered, release = threading.Event(), threading.Event()
        class Client(FakeClient):
            def submit_turn(self, *args, **kwargs):
                entered.set()
                if not release.wait(2):
                    raise AssertionError('test timed out')
            def wait_reply(self, turn_id, timeout, interval, stop):
                return None
        provider = TunnelProvider(Client())
        provider.prepare_turn()
        result = []
        def dispatch():
            try:
                provider(Request('turn', 'hello', Route('muse', 'm', '', '')))
            except Exception as error:
                result.append(error)
        thread = threading.Thread(target=dispatch)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            provider.request_stop()
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], RuntimeError)
        self.assertIn('delivery may still complete', str(result[0]))

    def test_service_admission_uses_actual_tunnel_provider_only(self):
        from types import SimpleNamespace
        from deskhand_service import ChatService
        import time
        route = Route('muse', 'muse', 'deskhand', '')
        provider = TunnelProvider(FakeClient())
        service = ChatService.__new__(ChatService)
        service.dispatcher = SimpleNamespace(adapters={route: SimpleNamespace(provider=provider)})
        service._muse_worker_gate_signal = lambda: {'down': True}
        service._failfast_snapshot = lambda: {'ok': True, 'as_of': time.time(), 'components': {
            'worker': {'status': 'DOWN', 'last_ok': 0},
            'grant': {'status': 'UP'},
        }}
        self.assertIsNone(service._failfast_check({}, route, None))
        service.dispatcher.adapters[route] = SimpleNamespace(provider=object())
        self.assertEqual(service._failfast_check({}, route, None)['path'], 'muse_worker')
        service.dispatcher.adapters[route] = SimpleNamespace(provider=provider)
        service._failfast_snapshot = lambda: {'ok': True, 'as_of': time.time(), 'components': {
            'worker': {'status': 'DOWN', 'last_ok': 0},
            'grant': {'status': 'DOWN'},
        }}
        self.assertEqual(service._failfast_check({}, route, None)['path'], 'grant')

    def test_inflight_trip_skips_only_the_exact_tunnel_turn(self):
        from types import SimpleNamespace
        from deskhand_service import ChatService
        service = ChatService.__new__(ChatService)
        service.dispatcher = SimpleNamespace(running_turn_info=lambda: {'turn_id': 'a', 'agent_task_id': None})
        service._running_turn_is_tunnel_served = lambda: ('a', True)
        marked = []
        service.box = SimpleNamespace(uncertain=lambda *args: marked.append(args))
        service._failfast_snapshot = lambda: None
        service._muse_worker_gate_signal = lambda: {'down': True}
        service._failfast_trip_check()
        self.assertEqual(marked, [])
        service._running_turn_is_tunnel_served = lambda: ('b', True)
        service._failfast_trip_check()
        self.assertEqual(marked[0][0], 'a')

    def test_shutdown_retains_ownership_when_receipt_worker_is_stuck(self):
        from types import SimpleNamespace
        from deskhand_service import ChatService
        class Thread:
            def __init__(self, alive):
                self.alive = alive
                self.timeout = None
            def join(self, timeout):
                self.timeout = timeout
            def is_alive(self):
                return self.alive
        class Dispatcher:
            adapters = {}
            exited = False
            shutdown_called = False
            def shutdown(self):
                self.shutdown_called = True
            def __exit__(self):
                self.exited = True
        receipts = Thread(True)
        dispatcher = Dispatcher()
        subject = SimpleNamespace(
            stopping=threading.Event(), wake=threading.Event(), host_rpc=None,
            local_runtime=SimpleNamespace(close=lambda: None), muse=None,
            worker=Thread(False), watchdog=Thread(False),
            _tunnel_receipts_thread=receipts, dispatcher=dispatcher,
            _stop_bridge=lambda: None, task_agent_lifecycle=None,
            data=Path('unused'))
        ChatService.close(subject)
        self.assertEqual(receipts.timeout, 5)
        self.assertFalse(dispatcher.exited)
        self.assertTrue(dispatcher.shutdown_called)

    def test_shutdown_during_fetch_preserves_unknown_and_unacked_reply(self):
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            box = Mailbox(Path(directory) / 'turns.sqlite')
            turn = str(uuid.uuid4())
            box.submit(turn, 'hello', 'muse', 'm')
            box.claim()
            box.mark_tunnel_dispatch(turn)
            box.recover_interrupted()
            class Client:
                def fetch_reply(self, turn_id):
                    stop.set()
                    return {'turn_id': turn_id, 'reply': 'late'}
                def ack_reply(self, turn_id):
                    raise AssertionError('must not acknowledge after shutdown')
            TunnelProvider(Client(), mailbox=box).reconcile_receipts(stop_event=stop)
            self.assertEqual(box.get(turn)['status'], 'unknown')
            self.assertEqual(box.pending_tunnel_receipts()[0]['id'], turn)

    def test_shutdown_during_ack_preserves_marker_for_retry(self):
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            box = Mailbox(Path(directory) / 'turns.sqlite')
            turn = str(uuid.uuid4())
            box.submit(turn, 'hello', 'muse', 'm')
            box.claim()
            box.mark_tunnel_dispatch(turn)
            box.finish(turn, reply='done')
            class Client:
                def ack_reply(self, turn_id):
                    stop.set()
                    return {'turn_id': turn_id, 'delivered': True}
            TunnelProvider(Client(), mailbox=box).reconcile_receipts(stop_event=stop)
            self.assertEqual(box.pending_tunnel_receipts()[0]['id'], turn)

    def test_stopped_wait_after_submit_stays_uncertain(self):
        from deskhand_timing import TimingRecorder
        from deskhand_stream import StreamSink
        with tempfile.TemporaryDirectory() as directory:
            box = Mailbox(Path(directory) / 'turns.sqlite')
            turn = str(uuid.uuid4())
            box.submit(turn, 'hello', 'muse', 'm')
            row = box.claim()
            client = FakeClient()
            provider = TunnelProvider(client, mailbox=box)
            def stopped(*args):
                provider.stopping.set()
                return None
            client.wait_reply = stopped
            request = Request(turn, 'hello', Route('muse', 'm', '', ''))
            with self.assertRaisesRegex(RuntimeError, 'delivery may still complete'):
                Dispatcher(box, {})._run_adapter(request, provider, TimingRecorder(), StreamSink(turn))
            self.assertEqual(box.get(turn)['status'], 'unknown')
            self.assertEqual(box.pending_tunnel_receipts()[0]['id'], turn)
    def test_restart_recovers_only_marked_unknown_and_retries_ack(self):
        class Client:
            def __init__(self, turn_id):
                self.turn_id = turn_id
                self.attempts = 0
            def fetch_reply(self, turn_id):
                return {'turn_id': turn_id, 'reply': 'late'}
            def ack_reply(self, turn_id):
                self.attempts += 1
                if self.attempts == 1:
                    from deskhand_tunnel import TunnelError
                    raise TunnelError('lost response')
                return {'turn_id': turn_id, 'delivered': True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'turns.sqlite'
            box = Mailbox(path)
            turn = str(uuid.uuid4())
            box.submit(turn, 'hello', 'muse', 'm')
            box.claim()
            box.mark_tunnel_dispatch(turn)
            box.recover_interrupted()
            client = Client(turn)
            TunnelProvider(client, mailbox=Mailbox(path)).reconcile_receipts()
            self.assertEqual(box.get(turn)['reply'], 'late')
            self.assertEqual(len(box.pending_tunnel_receipts()), 1)
            TunnelProvider(client, mailbox=Mailbox(path)).reconcile_receipts()
            self.assertEqual(box.pending_tunnel_receipts(), [])
            self.assertEqual(client.attempts, 2)

    def test_unknown_without_provenance_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Mailbox(Path(directory) / 'turns.sqlite')
            turn = str(uuid.uuid4())
            box.submit(turn, 'hello', 'muse', 'm')
            box.claim()
            box.recover_interrupted()
            TunnelProvider(FakeClient(), mailbox=box).reconcile_receipts()
            self.assertEqual(box.get(turn)['status'], 'unknown')

    def test_legacy_unknown_is_marked_and_reconciled_by_original_id(self):
        class Client(FakeClient):
            def __init__(self, turn_id):
                super().__init__()
                self.turn_id = turn_id
            def fetch_reply(self, turn_id):
                self.asserted_id = turn_id
                return {'turn_id': turn_id, 'reply': 'late answer'}
            def ack_reply(self, turn_id):
                self.acks.append(turn_id)
                return {'turn_id': turn_id, 'delivered': True}
        with tempfile.TemporaryDirectory() as directory:
            box = Mailbox(Path(directory) / 'turns.sqlite')
            turn = str(uuid.uuid4())
            box.submit(turn, 'hello', 'muse', 'm')
            box.claim()
            box.uncertain(turn)
            self.assertEqual([row['id'] for row in box.unmarked_unknown_muse_turns()], [turn])
            self.assertTrue(box.mark_unknown_muse_for_recovery(turn))
            client = Client(turn)
            TunnelProvider(client, mailbox=box).reconcile_receipts()
            self.assertEqual(client.asserted_id, turn)
            self.assertEqual(box.get(turn)['reply'], 'late answer')
            self.assertEqual(client.acks, [turn])
            self.assertEqual(box.pending_tunnel_receipts(), [])

    def test_legacy_terminal_reply_is_acked_only_when_content_matches(self):
        class Client(FakeClient):
            def __init__(self, result):
                super().__init__()
                self.result = result
            def fetch_reply(self, turn_id):
                return {'turn_id': turn_id, **self.result}
            def ack_reply(self, turn_id):
                self.acks.append(turn_id)
                return {'turn_id': turn_id, 'delivered': True}
        with tempfile.TemporaryDirectory() as directory:
            box = Mailbox(Path(directory) / 'turns.sqlite')
            turn = str(uuid.uuid4())
            box.submit(turn, 'hello', 'muse', 'm')
            box.claim()
            box.finish(turn, reply='durable answer')
            row = box.unmarked_terminal_muse_turns()[0]
            wrong = Client({'reply': 'different'})
            TunnelProvider(wrong).reconcile_historical_terminals([row])
            self.assertEqual(wrong.acks, [])
            matched = Client({'reply': 'durable answer'})
            TunnelProvider(matched).reconcile_historical_terminals([row])
            self.assertEqual(matched.acks, [turn])

    def test_disabled_tunnel_still_reconciles_pending_receipt(self):
        import deskhand_service
        class Client:
            def fetch_reply(self, turn_id):
                return {'turn_id': turn_id, 'reply': 'recovered'}
            def ack_reply(self, turn_id):
                return {'turn_id': turn_id, 'delivered': True}
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            box = Mailbox(data / 'turns.sqlite')
            turn = str(uuid.uuid4())
            box.submit(turn, 'hello', 'muse', 'm')
            box.claim()
            box.mark_tunnel_dispatch(turn)
            box.recover_interrupted()
            (data / 'deskhand_tunnel_config.json').write_text(
                '{"enabled": false}', encoding='utf-8')
            (data / 'deskhand_bridge_config.json').write_text(
                '{"token":"test-token"}', encoding='utf-8')
            service = deskhand_service.ChatService.__new__(deskhand_service.ChatService)
            service.data = data
            service.box = box
            service.stopping = threading.Event()
            service._legacy_terminal_cursor = None
            with patch.object(deskhand_service.deskhand_tunnel, 'TunnelHttpClient',
                              return_value=Client()) as client_factory:
                service._reconcile_tunnel_receipts_once()
            self.assertEqual(box.get(turn)['reply'], 'recovered')
            self.assertEqual(box.pending_tunnel_receipts(), [])
            client_factory.assert_called_once()

    def test_provider_swap_stops_admitted_tunnel_wait(self):
        from deskhand_cloud_tools import HostToolChat
        from deskhand_service import ChatService
        route = Route('muse', 'muse', 'deskhand', '')
        old = TunnelProvider(FakeClient())
        subject = ChatService.__new__(ChatService)
        subject.muse_route = route
        subject.data = Path('unused')
        subject._agent_turn = lambda *args: None
        subject.dispatcher = SimpleNamespace(
            adapters={route: HostToolChat(old, subject._agent_turn, Path('unused'))})
        replacement = object()
        subject._install_muse_provider(replacement)
        self.assertTrue(old.stopping.is_set())
        self.assertIs(subject.dispatcher.adapters[route].provider, replacement)

    def test_dispatcher_before_admit_is_atomic_and_refusal_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Mailbox(Path(directory) / 'turns.sqlite')
            dispatcher = Dispatcher(box, {})
            dispatcher.__enter__()
            turn = str(uuid.uuid4())
            entered = threading.Event()
            lock_was_held = []
            def before_admit():
                entered.set()
                acquired = dispatcher.admission_gate.acquire(timeout=0.01)
                lock_was_held.append(not acquired)
                if acquired:
                    dispatcher.admission_gate.release()
                raise RuntimeError('refused')
            with self.assertRaisesRegex(RuntimeError, 'refused'):
                dispatcher.submit(turn, 'hello', 'muse', 'm', '', '',
                                  before_admit=before_admit)
            self.assertTrue(entered.is_set())
            self.assertEqual(lock_was_held, [True])
            self.assertIsNone(box.get(turn))
            dispatcher.__exit__()

    def test_shutdown_interrupts_running_provider_without_cancelling_queue(self):
        class Interruptible:
            def __init__(self):
                self.stopped = False
            def request_stop(self):
                self.stopped = True
        with tempfile.TemporaryDirectory() as directory:
            box = Mailbox(Path(directory) / 'turns.sqlite')
            provider = Interruptible()
            dispatcher = Dispatcher(box, {})
            dispatcher.__enter__()
            dispatcher.running = ('active', provider)
            queued = str(uuid.uuid4())
            box.submit(queued, 'hello', 'muse', 'm')
            dispatcher.shutdown()
            self.assertTrue(provider.stopped)
            self.assertEqual(box.get(queued)['status'], 'queued')
            self.assertFalse(box.admission_state()[0])
            dispatcher.running = None
            dispatcher.__exit__()

    def test_bridge_child_process_is_terminated_on_explicit_stop(self):
        from deskhand_service import ChatService
        with tempfile.TemporaryDirectory() as directory:
            child = subprocess.Popen(
                [sys.executable, '-c', 'import time; time.sleep(60)'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            subject = SimpleNamespace(
                bridge_proc=child, bridge_log=None, data=Path(directory))
            result = ChatService._stop_bridge(subject)
            self.assertFalse(result['running'])
            self.assertIsNotNone(child.poll())
    def test_acknowledges_only_after_saved_hook(self):
        client = FakeClient()
        provider = TunnelProvider(client)
        request = Request("turn-1", "hello", Route("muse", "m", "", ""))
        result = provider(request)
        self.assertEqual(result.text, "done")
        self.assertEqual(client.acks, [])
        provider.reply_saved(request)
        self.assertEqual(client.acks, ["turn-1"])

    def test_obsolete_tunnel_down_does_not_refuse(self):
        import time
        snapshot = {"ok": True, "as_of": time.time(), "components": {
            "tunnel": {"status": "DOWN"},
        }}
        self.assertIsNone(evaluate_gate(snapshot, {"route": "muse", "origin": "phone"}))

    def test_real_worker_failure_is_still_refused(self):
        import time
        snapshot = {"ok": True, "as_of": time.time(), "components": {
            "tunnel": {"status": "DOWN"},
            "worker": {"status": "DOWN", "last_ok": 0},
        }}
        result = evaluate_gate(snapshot, {"route": "muse", "origin": "phone"})
        self.assertEqual(result["path"], "worker")
        self.assertIsNone(result["cause"])

    def test_error_reply_is_not_acknowledged_before_durable_error(self):
        client = FakeClient()
        client.wait_reply = lambda *args: {"turn_id": "turn-1", "error": "failed"}
        provider = TunnelProvider(client)
        request = Request("turn-1", "hello", Route("muse", "m", "", ""))
        with self.assertRaisesRegex(RuntimeError, "failed"):
            provider(request)
        self.assertEqual(client.acks, [])

    def test_dispatcher_saves_terminal_error_before_ack(self):
        from deskhand_timing import TimingRecorder
        from deskhand_stream import StreamSink
        events = []
        client = FakeClient()
        client.wait_reply = lambda *args: {"turn_id": "turn-1", "error": "failed"}
        client.ack_reply = lambda turn_id: events.append("ack")
        provider = TunnelProvider(client)
        request = Request("turn-1", "hello", Route("muse", "m", "", ""))
        class Box:
            path = "test-only.sqlite"
            def finish(self, turn_id, **values):
                self.values = values
                events.append("save")
        box = Box()
        dispatcher = Dispatcher(box, {})
        dispatcher._run_adapter(request, provider, TimingRecorder(), StreamSink("turn-1"))
        self.assertEqual(events, ["save", "ack"])
        self.assertIn("failed", box.values["error"])


if __name__ == "__main__":
    unittest.main()
