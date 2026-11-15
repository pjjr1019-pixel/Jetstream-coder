"""Tests for Batch 2 reply speed (v100.004) — unlatch copy.

No live model, gateway, or PC is touched: fake HTTP clients, fake
Codex rpc, and the real Mailbox/Dispatcher over temp SQLite files.
Covers: StreamSink (text-only, protocol JSON refused), router
(classify/suggest), per-hop timing, mailbox
partials/timing/route-claim/requeue, per-route parallel dispatch
(read-only only; agent turns stay serial), Ollama + OpenAI-compatible
streaming, Codex item/delta streaming, fallback status cache, and the
service ops (route_suggest, router_settings, warm_start,
runtime_stream, timing).
Run: python3 -m unittest test_batch2_speed -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# deskhand_fallback_caller.py lives in deskhand-port-work/, not in service/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from deskhand_stream import StreamSink, MAX_PARTIAL_CHARS
from deskhand_router import classify, suggest_route
from deskhand_timing import TimingRecorder
from deskhand_mailbox import Mailbox
from deskhand_dispatch import Dispatcher, Reply, Request, Route


class StreamSinkTests(unittest.TestCase):
    def test_feed_accumulates_text_and_seq(self):
        sink = StreamSink("t1")
        self.assertTrue(sink.feed("Hel"))
        self.assertTrue(sink.feed("lo"))
        self.assertEqual(sink.text, "Hello")
        self.assertEqual(sink.seq, 2)
        self.assertIsNotNone(sink.first_byte_at)

    def test_protocol_json_chunk_is_dropped_never_forwarded(self):
        seen = []
        sink = StreamSink("t1", on_delta=lambda text, seq: seen.append((text, seq)))
        self.assertTrue(sink.feed("ok "))
        self.assertFalse(sink.feed('{"type": "deskhand.actions", "actions": []}'))
        self.assertEqual(sink.text, "ok ")
        self.assertEqual(sink.dropped_protocol_chunks, 1)
        self.assertEqual(len(seen), 1)

    def test_close_stops_feeding(self):
        sink = StreamSink("t1")
        sink.feed("x")
        self.assertEqual(sink.close(), "x")
        self.assertFalse(sink.feed("y"))
        self.assertEqual(sink.text, "x")

    def test_empty_and_non_string_rejected(self):
        sink = StreamSink("t1")
        self.assertFalse(sink.feed(""))
        self.assertFalse(sink.feed(None))
        self.assertEqual(sink.seq, 0)


class RouterTests(unittest.TestCase):
    def test_short_greeting_is_fast(self):
        decision = classify("hi")
        self.assertEqual(decision["kind"], "fast")

    def test_reasoning_and_file_work_is_smart(self):
        self.assertEqual(classify("write a script to move my files then delete the old ones")["kind"], "smart")
        self.assertEqual(classify("why is my pc slow and how do I fix it")["kind"], "smart")

    def test_empty_routes_up_and_long_text_is_smart(self):
        self.assertEqual(classify("")["kind"], "smart")
        self.assertEqual(classify("x" * 200)["kind"], "smart")

    def test_suggest_fast_prefers_local_smart_prefers_default_codex(self):
        routes = [
            {"assistant": "codex", "model": "gpt-5", "account": "codex:1", "effort": "high", "default": True},
            {"assistant": "local", "model": "qwen3:8b", "account": "", "effort": ""},
        ]
        fast = suggest_route("hi", routes)
        self.assertEqual(fast["route"]["assistant"], "local")
        smart = suggest_route("research the best laptop and compare prices", routes)
        self.assertEqual(smart["route"]["assistant"], "codex")
        self.assertTrue(smart["route"]["default"])

    def test_always_smart_override_routes_up(self):
        routes = [{"assistant": "local", "model": "qwen3:8b", "account": "", "effort": ""}]
        decision = suggest_route("hi", routes, always_smart=True)
        self.assertEqual(decision["kind"], "smart")

    def test_fast_picks_lowest_effort_codex_not_alphabetical(self):
        # B2-D M2: effort is a reasoning level; alphabetical sort
        # picked "high" first ("high" < "low" < "medium").
        routes = [
            {"assistant": "codex", "model": "m", "account": "a", "effort": "high"},
            {"assistant": "codex", "model": "m", "account": "a", "effort": "medium"},
            {"assistant": "codex", "model": "m", "account": "a", "effort": "low"},
        ]
        fast = suggest_route("hi", routes)
        self.assertEqual(fast["route"]["effort"], "low")
        smart = suggest_route("research the best laptop and compare prices", routes)
        self.assertEqual(smart["route"]["effort"], "high")


class TimingTests(unittest.TestCase):
    def test_derived_durations(self):
        recorder = TimingRecorder()
        recorder.mark("submitted", at=100.0)
        recorder.mark("claimed", at=101.0)
        recorder.mark("first_byte", at=101.5)
        recorder.mark("adapter_done", at=103.0)
        recorder.mark("finished", at=103.2)
        result = recorder.as_dict()
        self.assertEqual(result["total_secs"], 3.2)
        self.assertEqual(result["time_to_first_byte_secs"], 0.5)
        self.assertEqual(result["generation_secs"], 1.5)

    def test_unknown_hop_raises(self):
        with self.assertRaises(ValueError):
            TimingRecorder().mark("nope")

    def test_first_mark_wins(self):
        recorder = TimingRecorder()
        recorder.mark("claimed", at=5.0)
        recorder.mark("claimed", at=9.0)
        self.assertEqual(recorder.as_dict()["claimed"], 5.0)



class MailboxBatch2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Mailbox(Path(self.tmp.name) / "turns.sqlite")
        self.route = Route("codex", "m", "a", "high")
        self.turn_id = str(uuid.uuid4())
        self.box.submit(self.turn_id, "hello", **vars(self.route))

    def test_publish_partial_newer_seq_only_and_finish_clears(self):
        self.box.claim()
        self.box.publish_partial(self.turn_id, "Hel", 1)
        self.box.publish_partial(self.turn_id, "Hello", 2)
        self.assertEqual(self.box.get(self.turn_id)["partial"], "Hello")
        with self.assertRaises(ValueError):
            self.box.publish_partial(self.turn_id, "old", 1)
        self.box.finish(self.turn_id, reply="Hello")
        row = self.box.get(self.turn_id)
        self.assertIsNone(row["partial"])
        self.assertEqual(row["reply"], "Hello")

    def test_record_timing_merges(self):
        self.box.claim()
        self.box.record_timing(self.turn_id, {"claimed": 1.0})
        self.box.record_timing(self.turn_id, {"finished": 2.0})
        timing = json.loads(self.box.get(self.turn_id)["timing_json"])
        self.assertEqual(timing, {"claimed": 1.0, "finished": 2.0})

    def test_claim_for_route_picks_matching_route_only(self):
        other = Route("local", "qwen3:8b", "", "")
        other_id = str(uuid.uuid4())
        self.box.submit(other_id, "local hi", **vars(other))
        claimed = self.box.claim_for_route(other)
        self.assertEqual(claimed["id"], other_id)
        self.assertEqual(self.box.get(self.turn_id)["status"], "queued")

    def test_requeue_returns_turn_to_queued(self):
        self.box.claim()
        self.box.requeue(self.turn_id)
        self.assertEqual(self.box.get(self.turn_id)["status"], "queued")


class DispatchBatch2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Mailbox(Path(self.tmp.name) / "turns.sqlite")
        self.route_a = Route("local", "model-a", "", "")
        self.route_b = Route("local", "model-b", "", "")

    def _streaming_adapter(self, text, barrier=None, seen=None):
        def adapter(request):
            return Reply(request.turn_id, request.route, text)
        def stream(request, on_delta=None):
            if seen is not None:
                seen.append(request.turn_id)
            if barrier is not None:
                barrier.wait(timeout=5)
            if on_delta is not None:
                on_delta(text[:2])
                on_delta(text)
            return Reply(request.turn_id, request.route, text)
        adapter.stream = stream
        return adapter

    def test_parallel_read_only_turns_on_two_routes_run_concurrently(self):
        id_a = str(uuid.uuid4())
        id_b = str(uuid.uuid4())
        self.box.submit(id_a, "a?", **vars(self.route_a))
        self.box.submit(id_b, "b?", **vars(self.route_b))
        barrier = threading.Barrier(2)
        adapters = {self.route_a: self._streaming_adapter("reply-a", barrier=barrier),
                    self.route_b: self._streaming_adapter("reply-b", barrier=barrier)}
        with Dispatcher(self.box, adapters) as dispatcher:
            results = {}
            def run(route, key):
                results[key] = dispatcher.dispatch_one_for_route(route)
            ta = threading.Thread(target=run, args=(self.route_a, "a"))
            tb = threading.Thread(target=run, args=(self.route_b, "b"))
            ta.start(); tb.start(); ta.join(10); tb.join(10)
            self.assertFalse(ta.is_alive())
            self.assertFalse(tb.is_alive())
            self.assertEqual(results["a"]["status"], "completed")
            self.assertEqual(results["b"]["status"], "completed")
            # Streaming partials were published and cleared on finish.
            self.assertIsNone(self.box.get(id_a)["partial"])
            self.assertEqual(self.box.get(id_a)["reply"], "reply-a")
            timing = json.loads(self.box.get(id_a)["timing_json"])
            self.assertIn("claimed", timing)
            self.assertIn("first_byte", timing)
            self.assertIn("finished", timing)

    def test_same_route_turns_stay_serial(self):
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        for turn_id in ids:
            self.box.submit(turn_id, "hi", **vars(self.route_a))
        concurrent = {"current": 0, "max": 0}
        lock = threading.Lock()
        def stream(request, on_delta=None):
            with lock:
                concurrent["current"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["current"])
            time.sleep(0.05)
            with lock:
                concurrent["current"] -= 1
            return Reply(request.turn_id, request.route, "ok")
        adapter = lambda request: Reply(request.turn_id, request.route, "ok")
        adapter.stream = stream
        with Dispatcher(self.box, {self.route_a: adapter}) as dispatcher:
            threads = [threading.Thread(target=lambda: dispatcher.dispatch_one_for_route(self.route_a)) for _ in ids]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(concurrent["max"], 1)
        for turn_id in ids:
            self.assertEqual(self.box.get(turn_id)["status"], "completed")

    def test_agent_turn_is_requeued_not_parallelised(self):
        turn_id = str(uuid.uuid4())
        self.box.submit(turn_id, "do it", **vars(self.route_a), agent_task_id=str(uuid.uuid4()))
        with Dispatcher(self.box, {self.route_a: lambda r: Reply(r.turn_id, r.route, "done")}) as dispatcher:
            self.assertIsNone(dispatcher.dispatch_one_for_route(self.route_a))
            self.assertEqual(self.box.get(turn_id)["status"], "queued")
            row = dispatcher.dispatch_one()
            self.assertEqual(row["status"], "completed")

    def test_read_only_false_falls_back_to_serial(self):
        turn_id = str(uuid.uuid4())
        self.box.submit(turn_id, "hi", **vars(self.route_a))
        with Dispatcher(self.box, {self.route_a: lambda r: Reply(r.turn_id, r.route, "done")}) as dispatcher:
            row = dispatcher.dispatch_one_for_route(self.route_a, read_only=False)
            self.assertEqual(row["status"], "completed")


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._payload) - self._pos
        chunk = self._payload[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


class FakeClient:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.bodies = []

    def open(self, request, timeout=None):
        self.bodies.append(request.data)
        return FakeResponse(self.payload)


class LocalStreamTests(unittest.TestCase):
    def test_ollama_stream_feeds_deltas_and_keeps_alive(self):
        from deskhand_local import LocalChat
        route = Route("local", "qwen3:8b", "", "")
        chat = LocalChat(route, "http://127.0.0.1:11434")
        lines = b"".join([
            json.dumps({"model": "qwen3:8b", "message": {"role": "assistant", "content": "Hel"}, "done": False}).encode() + b"\n",
            json.dumps({"model": "qwen3:8b", "message": {"role": "assistant", "content": "lo"}, "done": True}).encode() + b"\n",
        ])
        chat.client = FakeClient(lines)
        deltas = []
        reply = chat.stream(Request(str(uuid.uuid4()), "hi", route), on_delta=deltas.append)
        self.assertEqual(reply.text, "Hello")
        self.assertEqual(deltas, ["Hel", "Hello"])
        self.assertEqual(json.loads(chat.client.bodies[0])["keep_alive"], "30m")
        self.assertTrue(json.loads(chat.client.bodies[0])["stream"])

    def test_ollama_warmup_discards_reply_and_never_raises(self):
        from deskhand_local import LocalChat
        route = Route("local", "qwen3:8b", "", "")
        chat = LocalChat(route, "http://127.0.0.1:11434")
        chat.client = FakeClient(json.dumps({"model": "qwen3:8b", "done": True}).encode())
        self.assertTrue(chat.warmup())

        class BrokenClient:
            def open(self, *args, **kwargs):
                raise ConnectionError("down")
        chat.client = BrokenClient()
        self.assertFalse(chat.warmup())


class OpenAIStreamTests(unittest.TestCase):
    def test_openai_sse_stream(self):
        from deskhand_openai_local import OpenAILocalChat
        route = Route("local", "m", "", "")
        chat = OpenAILocalChat(route, "http://127.0.0.1:1234")
        payload = b"".join([
            b'data: {"model": "m", "choices": [{"delta": {"content": "Hel"}, "finish_reason": null}]}\n\n',
            b'data: {"model": "m", "choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]}\n\n',
            b"data: [DONE]\n",
        ])
        chat.client = FakeClient(payload)
        deltas = []
        reply = chat.stream(Request(str(uuid.uuid4()), "hi", route), on_delta=deltas.append)
        self.assertEqual(reply.text, "Hello")
        self.assertEqual(deltas, ["Hel", "Hello"])


class FakeProc:
    def poll(self):
        return None


class FakeCodexRpc:
    def __init__(self, home=None, timeout=180):
        self.proc = FakeProc()
        self._events = []

    def call(self, method, params=None):
        if method == "thread/start":
            return {"thread": {"id": "t1"}}
        if method == "turn/start":
            self._events = [
                {"method": "item/delta",
                 "params": {"threadId": "t1", "turnId": "turn1",
                            "item": {"id": "m1", "type": "agentMessage"},
                            "delta": {"text": "Hel"}}},
                {"method": "item/delta",
                 "params": {"threadId": "t1", "turnId": "turn1",
                            "item": {"id": "m1", "type": "agentMessage"},
                            "delta": {"text": "lo"}}},
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
            raise TimeoutError("no events")
        return self._events.pop(0)

    def close(self):
        pass


class CodexStreamTests(unittest.TestCase):
    def test_item_delta_feeds_on_delta_and_returns_full_reply(self):
        from deskhand_codex import CodexChat
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        route = Route("codex", "gpt-5", "default", "")
        chat = CodexChat(route, Path(tmp.name), rpc_factory=FakeCodexRpc)
        deltas = []
        reply = chat.stream(Request(str(uuid.uuid4()), "hi", route), on_delta=deltas.append)
        self.assertEqual(reply.text, "Hello")
        self.assertEqual(deltas, ["Hel", "Hello"])
        chat.close()


class FallbackCacheTests(unittest.TestCase):
    def test_cached_status_returned_without_fetch_and_counter(self):
        import deskhand_fallback_caller as caller
        body = {"action_routes_available": True}
        with caller._STATUS_CACHE_LOCK:
            caller._STATUS_CACHE["at"] = time.monotonic()
            caller._STATUS_CACHE["body"] = dict(body)
            caller._STATUS_COUNTER["fresh"] = 0
            caller._STATUS_COUNTER["cached"] = 0
        self.assertEqual(caller.fetch_gateway_status(), body)
        stats = caller.status_cache_stats()
        self.assertEqual(stats["cached"], 1)
        self.assertEqual(stats["fresh"], 0)


class ServiceOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from deskhand_service import ChatService
        self.service = ChatService(Path(self.tmp.name), fixture=True)
        self.addCleanup(self.service.close)
        self.service.routes = [
            {"assistant": "codex", "model": "gpt-5", "account": "codex:1", "effort": "high",
             "efforts": ["high"], "name": "Codex", "default": True},
            {"assistant": "local", "model": "qwen3:8b", "account": "", "effort": "",
             "efforts": [], "name": "Local · qwen3:8b"},
        ]

    def test_route_suggest_and_router_settings(self):
        fast = self.service.handle({"op": "route_suggest", "text": "hi"})
        self.assertEqual(fast["kind"], "fast")
        self.assertEqual(fast["route"]["assistant"], "local")
        self.assertEqual(self.service.handle({"op": "router_settings"}), {"always_smart": False})
        self.service.handle({"op": "router_settings", "always_smart": True})
        smart = self.service.handle({"op": "route_suggest", "text": "hi"})
        self.assertEqual(smart["kind"], "smart")
        self.assertEqual(smart["route"]["assistant"], "codex")

    def test_warm_start_and_timing_and_runtime_stream(self):
        self.assertEqual(self.service.handle({"op": "warm_start"}), {"results": {}})
        self.assertEqual(self.service.handle({"op": "timing", "id": "nope"}), {"turn_id": "nope", "timing": {}})
        stream = self.service.handle({"op": "runtime_stream", "since": None, "limit": 100})
        self.assertIn("events", stream)
        self.assertIn("latest_seq", stream)
        self.assertIn("sources", stream)
        self.assertEqual(stream["sources"]["chat"], "ok")
        self.assertFalse(stream["resynced"])
        # A second poll with the latest seq returns nothing new.
        again = self.service.handle({"op": "runtime_stream", "since": stream["latest_seq"], "limit": 100})
        self.assertEqual(again["events"], [])
        self.assertFalse(again["resynced"])

    def test_submit_auto_route_picks_local_for_trivial_text(self):
        turn_id = str(uuid.uuid4())
        # Fixture mode: local route submits are accepted; the lambda
        # adapter replies immediately.
        row = self.service.handle({"op": "submit", "id": turn_id, "text": "hi", "auto_route": True})
        self.assertEqual(row["assistant"], "local")
        self.assertEqual(row["model"], "qwen3:8b")


if __name__ == "__main__":
    unittest.main()
