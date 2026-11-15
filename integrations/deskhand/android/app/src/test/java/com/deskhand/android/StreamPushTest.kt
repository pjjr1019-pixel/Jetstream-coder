package com.deskhand.android

import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows
import org.robolectric.annotation.Config

/**
 * 100.017 (C13 push + absorbed 100.018 Part B fetch / Part A5 parser):
 * stream frame parsing, seq ordering + both dedup keys, resync on a
 * backwards seq, ack contiguity, the 3 s / 6 s fallback state machine,
 * the restored runtime_stream parser, and the drawer footer lines.
 *
 * Worker A contract note (100.017) — the two subtleties this file
 * proves, not just states:
 * (1) A stale event (seq <= latestSeen) is dropped INDIVIDUALLY; a
 * whole frame arriving backwards is a resync (one full poll fetch,
 * since=null). Covered by staleEventDroppedIndividuallyInMixedFrame,
 * wholeFrameBackwardsIsResync, and
 * wholeFrameBackwardsPureRetransmitIsDropped.
 * (2) Gaps heal ONLY via full fetch — never by waiting for push frames
 * to fill them (the host never re-sends). Covered by
 * gapDetectedOnMissingSeq and ackStopsAtGapAndGapHealsOnlyByFullFetch:
 * a gap issues a full fetch immediately, no ack passes it, and the heal
 * is the poll reply's rebase — not a later push frame.
 *
 * What this file does NOT prove (needs the PC, spec §9.2–§9.3): a real
 * host sending stream.* frames, the real relay socket, live latency, or
 * the 1 s / 5 s drawer cadence against a real PC — the fake link stands
 * in for the socket, so a real-host failure mode is a live-test item,
 * not a unit-test claim. The VM cannot run these tests at all (no
 * Android SDK) — see PATCH-REPORT-C.md for the PC commands.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class StreamPushTest {
    private val secret = ByteArray(16) { 1 }
    // 100.040: the tunnel is removed — Model.connect filters tunnel URLs,
    // so a tunnel-only fixture never creates a link and connectCapturing
    // NPEs on link!!. Same Tailscale address as TransportFallbackTest's
    // tailscale fixture.
    private val relay = "ws://100.64.0.1:47801"

    private fun idleMain() {
        Shadows.shadowOf(Looper.getMainLooper()).idle()
    }

    @After fun resetModelStreamState() {
        // Model is a process-wide singleton: never leak stream state, a
        // fake factory, or a Viewer screen into the next test class.
        Model.disconnect()
        Model.linkFactory = { relay, linkSecret, events -> PcLink(relay, linkSecret, events) }
        Model.viewerPc = null
        Model.screen = Screen.Home
        Model.chatConnected = false
        Model.viewerConnection = "Disconnected"
        Model.agentsSnapshot = AgentsSnapshot.EMPTY
        Model.agentsLoading = false
        Model.streamTransport = StreamTransport.POLLING
        Model.streamLastFrameMs = 0L
        Model.streamLastDeliveryMs = 0L
        Model.streamAckedSeq = 0L
        Model.streamSubscribed = false
        Model.streamBuffer.reset()
        Model.streamEvents = emptyList()
        Model.streamSources = null
        idleMain()
    }

    // ---- fake link + connect harness (same shape as DiagnosticsScreenTest) ----

    private class StreamCapturingLink(relay: String, secret: ByteArray, val events: PcLink.Events) :
        PcLink(relay, secret, events) {
        val chatRequests = mutableListOf<JSONObject>()
        val streamSends = mutableListOf<JSONObject>()
        override fun start() {}
        override fun close() {}
        override fun send(type: Int, payload: ByteArray): Boolean {
            runCatching { JSONObject(String(payload, Charsets.UTF_8)) }.getOrNull()?.let {
                if (type == Msg.CHAT_REQUEST) chatRequests.add(it)
                if (type == Msg.STREAM) streamSends.add(it)
            }
            return true
        }
    }

    private fun connectCapturing(pc: PairedPc): StreamCapturingLink {
        var link: StreamCapturingLink? = null
        Model.linkFactory = { relay, linkSecret, events -> StreamCapturingLink(relay, linkSecret, events).also { link = it } }
        Model.connect(pc)
        idleMain()
        val created = link!!
        created.events.onConnection(true)
        idleMain()
        created.events.onSession("1234")
        idleMain()
        assertEquals("Connected", Model.viewerConnection)
        // INFO is not driven here — the chat flag is what
        // fetchAgentsSnapshot / handleStreamFrame actually read.
        Model.chatConnected = true
        return created
    }

    // ---- frame / event builders ----

    private fun streamEvent(seq: Long, ts: Double, type: String, source: String = "bridge", summary: String = "e$seq", path: String = "/tmp/f$seq.txt"): JSONObject =
        JSONObject()
            .put("seq", seq).put("ts", ts).put("source", source).put("type", type)
            .put("summary", summary)
            .put("detail", JSONObject().put("operation", "write").put("path", path).put("ok", true))

    private fun streamFrame(kind: String, block: JSONObject.() -> Unit = {}): ByteArray =
        JSONObject().put("kind", kind).apply(block).toString().toByteArray(Charsets.UTF_8)

    private fun eventsFrame(vararg events: JSONObject, latestSeq: Long): ByteArray {
        val arr = JSONArray()
        events.forEach { arr.put(it) }
        return streamFrame("stream.events") {
            put("events", arr)
            put("latest_seq", latestSeq)
        }
    }

    private fun runtimeResult(events: JSONArray, latestSeq: Long, vmSource: String): JSONObject =
        JSONObject()
            .put("events", events)
            .put("latest_seq", latestSeq)
            .put("sources", JSONObject().put("chat", "ok").put("bridge", "ok").put("vm_events", vmSource))
            .put("counts", JSONObject().put("skipped_bad_lines", 0).put("returned", events.length()))

    private fun runtimeStreamReply(rpc: String, result: JSONObject): JSONObject =
        JSONObject().put("op", "runtime_stream").put("ok", true).put("rpc", rpc).put("result", result)

    private fun lastAck(link: StreamCapturingLink): Long? =
        link.streamSends.lastOrNull { it.optString("kind") == "stream.ack" }?.optLong("acked_seq")

    private fun fetchRpcs(link: StreamCapturingLink): List<JSONObject> =
        link.chatRequests.filter { it.optString("op") == "runtime_stream" }

    // ---- §3.4 frame parse: all six frames ----

    @Test fun frameParseAllSixKinds() {
        val hello = parseStreamFrame(streamFrame("stream.hello") {
            put("latest_seq", 7)
            put("sources", JSONObject().put("chat", "ok").put("bridge", "down").put("vm_events", "missing"))
        })
        assertEquals("stream.hello", hello?.kind)
        assertEquals(7L, hello?.latestSeq)
        assertEquals("ok", hello?.sources?.optString("chat"))

        val sub = parseStreamFrame(streamFrame("stream.subscribe") { put("since", 3) })
        assertEquals("stream.subscribe", sub?.kind)
        assertEquals(3L, sub?.since)

        val events = parseStreamFrame(eventsFrame(streamEvent(1, 1762650000.0, "file.write"), latestSeq = 1))
        assertEquals("stream.events", events?.kind)
        assertEquals(1, events?.events?.size)
        assertEquals(1L, events?.latestSeq)

        val ack = parseStreamFrame(streamFrame("stream.ack") { put("acked_seq", 9) })
        assertEquals("stream.ack", ack?.kind)
        assertEquals(9L, ack?.ackedSeq)

        val resynced = parseStreamFrame(streamFrame("stream.resynced") { put("latest_seq", 41) })
        assertEquals("stream.resynced", resynced?.kind)
        assertEquals(41L, resynced?.latestSeq)

        val ping = parseStreamFrame(streamFrame("stream.ping"))
        assertEquals("stream.ping", ping?.kind)

        // Malformed payloads and kind-less payloads parse to null and are
        // ignored by handleStreamFrame — never a throw on the socket path.
        assertNull(parseStreamFrame("not json".toByteArray(Charsets.UTF_8)))
        assertNull(parseStreamFrame(JSONObject().put("latest_seq", 1).toString().toByteArray(Charsets.UTF_8)))
    }

    // ---- §3.3 steps 3–4: seq ordering + both dedup keys (buffer level) ----

    @Test fun seqOrderingAndBothDedupKeys() {
        val buf = StreamEventBuffer()
        val now = 1_000_000L
        val e1 = streamEvent(1, 1762650000.0, "file.write")
        val e2 = streamEvent(2, 1762650001.0, "file.write")
        // Out of order inside one frame: applied in seq order.
        val r1 = buf.applyEvents(listOf(e2, e1), now)
        assertEquals(listOf(1L, 2L), r1.applied.map { it.optLong("seq") })
        assertEquals(2L, r1.ackSeq)
        assertFalse(r1.resyncNeeded)
        assertFalse(r1.gapDetected)
        // The same event again: dropped by BOTH keys (seq <= frontier
        // AND the (source, ts, type, path) key matches).
        val r2 = buf.applyEvents(listOf(e1), now + 100)
        assertTrue(r2.applied.isEmpty())
        assertEquals(1, r2.droppedDuplicates)
        assertFalse(r2.resyncNeeded)
        // Same seq, different content (different path): the ring
        // re-numbered — a backwards seq with an unseen key is a resync.
        val impostor = streamEvent(1, 1762650000.0, "file.write", path = "/tmp/other.txt")
        val r3 = buf.applyEvents(listOf(impostor), now + 200)
        assertTrue(r3.resyncNeeded)
        assertFalse(r3.gapDetected)
    }

    // ---- Worker A contract (1): stale event drops individually ----

    @Test fun staleEventDroppedIndividuallyInMixedFrame() {
        val buf = StreamEventBuffer()
        val now = 1_000_000L
        buf.applyEvents(
            listOf(
                streamEvent(1, 1762650000.0, "file.write"),
                streamEvent(2, 1762650001.0, "file.write"),
                streamEvent(3, 1762650002.0, "file.write"),
            ),
            now,
        )
        assertEquals(3L, buf.frontierSeq)
        // One stale duplicate + one fresh event in the same frame: the
        // stale one drops individually, the fresh one applies, no resync.
        val e2dup = streamEvent(2, 1762650001.0, "file.write")
        val e4 = streamEvent(4, 1762650003.0, "file.write")
        val r = buf.applyEvents(listOf(e2dup, e4), now + 100)
        assertEquals(listOf(4L), r.applied.map { it.optLong("seq") })
        assertEquals(1, r.droppedDuplicates)
        assertFalse(r.resyncNeeded)
        assertFalse(r.gapDetected)
        assertEquals(4L, buf.frontierSeq)

        // Same, end to end: no full fetch is issued, the ack advances.
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.handleStreamFrame(
            eventsFrame(
                streamEvent(1, 1762650000.0, "file.write"),
                streamEvent(2, 1762650001.0, "file.write"),
                streamEvent(3, 1762650002.0, "file.write"),
                latestSeq = 3,
            ),
        )
        assertEquals(3L, lastAck(link))
        link.chatRequests.clear()
        link.streamSends.clear()
        Model.handleStreamFrame(eventsFrame(e2dup, e4, latestSeq = 4))
        assertEquals(4L, lastAck(link))
        assertTrue(fetchRpcs(link).isEmpty())
    }

    // ---- Worker A contract (1): whole frame backwards is a resync ----

    @Test fun wholeFrameBackwardsIsResync() {
        val buf = StreamEventBuffer()
        val now = 1_000_000L
        buf.applyEvents(
            listOf(
                streamEvent(1, 1762650000.0, "file.write"),
                streamEvent(2, 1762650001.0, "file.write"),
                streamEvent(3, 1762650002.0, "file.write"),
            ),
            now,
        )
        // A whole frame below the frontier with NEW content: the ring
        // re-numbered (seq is window-relative, V1) — one full fetch.
        val r = buf.applyEvents(
            listOf(
                streamEvent(1, 1762650000.0, "file.write", path = "/tmp/other1.txt"),
                streamEvent(2, 1762650001.0, "file.write", path = "/tmp/other2.txt"),
            ),
            now + 100,
        )
        assertTrue(r.resyncNeeded)
        assertFalse(r.gapDetected)

        // Same, end to end: exactly one full poll fetch, since=null.
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.handleStreamFrame(
            eventsFrame(
                streamEvent(1, 1762650000.0, "file.write"),
                streamEvent(2, 1762650001.0, "file.write"),
                streamEvent(3, 1762650002.0, "file.write"),
                latestSeq = 3,
            ),
        )
        assertEquals(3L, Model.streamBuffer.frontierSeq)
        link.chatRequests.clear()
        link.streamSends.clear()
        Model.handleStreamFrame(
            eventsFrame(
                streamEvent(1, 1762650000.0, "file.write", path = "/tmp/other1.txt"),
                streamEvent(2, 1762650001.0, "file.write", path = "/tmp/other2.txt"),
                latestSeq = 2,
            ),
        )
        assertEquals(1, fetchRpcs(link).size)
        assertEquals("runtime_stream", fetchRpcs(link)[0].optString("op"))
        assertFalse(fetchRpcs(link)[0].has("since"))
        assertEquals(100, fetchRpcs(link)[0].optInt("limit"))
        assertNull(lastAck(link))
    }

    @Test fun wholeFrameBackwardsPureRetransmitIsDropped() {
        val buf = StreamEventBuffer()
        val now = 1_000_000L
        buf.applyEvents(
            listOf(
                streamEvent(1, 1762650000.0, "file.write"),
                streamEvent(2, 1762650001.0, "file.write"),
                streamEvent(3, 1762650002.0, "file.write"),
            ),
            now,
        )
        // The same frame again (same content, same keys): every event is
        // a duplicate — dropped, no resync, no fetch.
        val r = buf.applyEvents(
            listOf(
                streamEvent(1, 1762650000.0, "file.write"),
                streamEvent(2, 1762650001.0, "file.write"),
            ),
            now + 100,
        )
        assertTrue(r.applied.isEmpty())
        assertEquals(2, r.droppedDuplicates)
        assertFalse(r.resyncNeeded)
        assertFalse(r.gapDetected)

        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.handleStreamFrame(
            eventsFrame(
                streamEvent(1, 1762650000.0, "file.write"),
                streamEvent(2, 1762650001.0, "file.write"),
                streamEvent(3, 1762650002.0, "file.write"),
                latestSeq = 3,
            ),
        )
        link.chatRequests.clear()
        link.streamSends.clear()
        Model.handleStreamFrame(
            eventsFrame(
                streamEvent(1, 1762650000.0, "file.write"),
                streamEvent(2, 1762650001.0, "file.write"),
                latestSeq = 2,
            ),
        )
        assertTrue(fetchRpcs(link).isEmpty())
        assertNull(lastAck(link))
    }

    // ---- Worker A contract (2): gaps heal ONLY via full fetch ----

    @Test fun gapDetectedOnMissingSeq() {
        val buf = StreamEventBuffer()
        val now = 1_000_000L
        // seq 2 with no seq 1: a frame was lost.
        val r1 = buf.applyEvents(listOf(streamEvent(2, 1762650001.0, "file.write")), now)
        assertTrue(r1.gapDetected)
        assertFalse(r1.resyncNeeded)
        assertTrue(r1.applied.isEmpty())
        assertEquals(0L, buf.frontierSeq)
        // The event is NOT buffered: nothing is retained waiting for push
        // to fill the gap — the full fetch is the only heal.
        val r2 = buf.applyEvents(
            listOf(
                streamEvent(1, 1762650000.0, "file.write"),
                streamEvent(3, 1762650002.0, "file.write"),
            ),
            now + 100,
        )
        // seq 1 applies (contiguous); seq 3 is another gap, reported with
        // the partial application — the Model discards the partial batch
        // on recovery because the poll supersedes it.
        assertTrue(r2.gapDetected)
        assertEquals(listOf(1L), r2.applied.map { it.optLong("seq") })
        assertEquals(1L, buf.frontierSeq)
    }

    @Test fun ackStopsAtGapAndGapHealsOnlyByFullFetch() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        // Frame [2] with frontier 0: a gap. The ack never passes it, no
        // ack is sent at all, and a full fetch is issued immediately.
        Model.handleStreamFrame(eventsFrame(streamEvent(2, 1762650001.0, "file.write"), latestSeq = 2))
        assertNull(lastAck(link))
        assertEquals(0L, Model.streamAckedSeq)
        assertEquals(1, fetchRpcs(link).size)
        assertFalse(fetchRpcs(link)[0].has("since"))
        // The heal is the POLL reply, not a later push frame: the reply
        // re-bases the cursor on the full window (events 1 + 2 present).
        val rpc = fetchRpcs(link)[0].optString("rpc")
        val window = JSONArray()
            .put(streamEvent(1, 1762650000.0, "file.write"))
            .put(streamEvent(2, 1762650001.0, "file.write"))
        Model.handleChatResponse(runtimeStreamReply(rpc, runtimeResult(window, 2, "missing")))
        assertEquals(2L, Model.streamBuffer.frontierSeq)
        assertEquals(2, Model.streamEvents.size)
        assertEquals(1_762_650_001_000L, Model.agentsSnapshot.newestEventTsMs)
        // The next push frame continues from the re-based frontier and
        // its ack advances — the gap is behind us.
        Model.handleStreamFrame(eventsFrame(streamEvent(3, 1762650002.0, "file.write"), latestSeq = 3))
        assertEquals(3L, lastAck(link))
        assertEquals(3L, Model.streamAckedSeq)
    }

    // ---- §4 subscribe / resync ----

    @Test fun helloTriggersSubscribeFromFrontier() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.handleStreamFrame(streamFrame("stream.hello") {
            put("latest_seq", 9)
            put("sources", JSONObject().put("chat", "ok").put("bridge", "ok").put("vm_events", "missing"))
        })
        assertTrue(Model.streamSubscribed)
        val sub = link.streamSends.lastOrNull { it.optString("kind") == "stream.subscribe" }
        assertNotNull(sub)
        assertEquals(0L, sub?.optLong("since")) // frontier is 0: "everything"
        assertEquals(StreamTransport.LIVE_PUSH, Model.streamTransport)
    }

    @Test fun resyncedFrameTriggersFullFetch() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        link.chatRequests.clear()
        Model.handleStreamFrame(streamFrame("stream.resynced") { put("latest_seq", 50) })
        assertEquals(1, fetchRpcs(link).size)
        assertEquals("runtime_stream", fetchRpcs(link)[0].optString("op"))
        assertFalse(fetchRpcs(link)[0].has("since"))
    }

    // ---- §6.1 fallback state machine at the 3 s / 6 s thresholds ----

    @Test fun fallbackStateMachineThresholds() {
        val now = 10_000_000L
        // Socket open + frame within 3 s: push live (boundary inclusive).
        assertEquals(StreamTransport.LIVE_PUSH, evaluateStreamTransport(true, now - 3_000, now - 3_000, now))
        // 3 s + 1 ms without a frame, but a delivery 1 s ago: polling.
        assertEquals(StreamTransport.POLLING, evaluateStreamTransport(true, now - 3_001, now - 1_000, now))
        // Nothing delivered for 6 s + 1 ms, socket still open: STALE.
        assertEquals(StreamTransport.STALE, evaluateStreamTransport(true, now - 60_000, now - 6_001, now))
        // Nothing for 6 s + socket down: the tunnel is named, not "push down".
        assertEquals(StreamTransport.TUNNEL_DOWN, evaluateStreamTransport(false, now - 60_000, now - 6_001, now))
        // Delivery 6 s ago exactly: still polling (boundary inclusive).
        assertEquals(StreamTransport.POLLING, evaluateStreamTransport(true, now - 60_000, now - 6_000, now))
        // Socket down but a delivery 1 s ago: polling (fresh data wins).
        assertEquals(StreamTransport.POLLING, evaluateStreamTransport(false, now - 60_000, now - 1_000, now))
    }

    @Test fun transportFooterText() {
        val now = 10_000_000L
        assertEquals("Live push", streamTransportText(StreamTransport.LIVE_PUSH, now, now))
        assertEquals("Polling — push down", streamTransportText(StreamTransport.POLLING, now, now))
        assertEquals("STALE — no update for 10s", streamTransportText(StreamTransport.STALE, now, now - 10_000))
        assertEquals("STALE — no update yet", streamTransportText(StreamTransport.STALE, now, 0))
        assertTrue(streamTransportText(StreamTransport.TUNNEL_DOWN, now, now - 10_000).contains("tunnel down"))
        assertTrue(streamTransportText(StreamTransport.TUNNEL_DOWN, now, now - 10_000).startsWith("STALE — no update for 10s"))
        assertTrue(streamTransportText(StreamTransport.TUNNEL_DOWN, now, 0).contains("tunnel down"))
    }

    // ---- 100.018 design §A5: the restored runtime_stream parser ----

    @Test fun parserEpochTsFirstAndIsoFallback() {
        val now = 1_794_208_050_000L
        val response = JSONObject()
            .put("events", JSONArray()
                .put(JSONObject()
                    .put("seq", 1).put("ts", 1762650000.0).put("source", "vm").put("type", "subagent.spawned")
                    .put("summary", "Do the thing")
                    .put("detail", JSONObject().put("agent_id", "aaaaaaaa-1111-2222-3333-444444444444").put("task", "Do the thing").put("chat_label", "Main chat")))
                .put(JSONObject()
                    .put("seq", 2).put("ts", "2026-11-09T07:07:00Z").put("source", "vm").put("type", "subagent.spawned")
                    .put("summary", "Do the other thing")
                    .put("detail", JSONObject().put("agent_id", "bbbbbbbb-1111-2222-3333-444444444444").put("task", "Do the other thing"))))
            .put("sources", JSONObject().put("chat", "ok").put("bridge", "ok").put("vm_events", "ok"))
            .put("counts", JSONObject().put("skipped_bad_lines", 0).put("returned", 2))
        val snap = agentsFromRuntimeEvents(response.toString(), now)
        assertEquals(2, snap.running.size)
        // Epoch first: 1762650000 s -> ms.
        assertEquals(1_762_650_000_000L, snap.running[0].startedAtMs)
        // ISO fallback: 2026-11-09T07:07:00Z -> 1794208020 s -> ms.
        assertEquals(1_794_208_020_000L, snap.running[1].startedAtMs)
        assertEquals("Main chat", snap.running[0].chatLabel)
        assertNull(snap.running[1].chatLabel)
        assertEquals(1_794_208_020_000L, snap.newestEventTsMs)
        assertEquals("ok", snap.vmEventsSource)
        assertEquals(0, snap.skippedBadLines)
    }

    @Test fun parserDuplicateFinishedDedupedByAgentId() {
        val now = 1_762_650_500_000L
        fun finished(result: String) = JSONObject()
            .put("seq", 9).put("ts", 1762650400.0).put("source", "vm").put("type", "subagent.finished")
            .put("summary", result)
            .put("detail", JSONObject().put("agent_id", "cccccccc-1111-2222-3333-444444444444")
                .put("task", "Build it").put("result", result).put("duration_sec", 420))
        val response = JSONObject()
            .put("events", JSONArray().put(finished("first")).put(finished("second")))
            .put("sources", JSONObject().put("vm_events", "ok"))
            .put("counts", JSONObject().put("skipped_bad_lines", 0))
        val snap = agentsFromRuntimeEvents(response.toString(), now)
        assertEquals(1, snap.finished.size)
        assertEquals("second", snap.finished[0].result) // keep last
        assertEquals(420L, snap.finished[0].durationSec)
    }

    @Test fun parserDroppedRemovesWithoutFinishing() {
        val now = 1_762_650_500_000L
        val agentA = "dddddddd-1111-2222-3333-444444444444"
        val agentB = "eeeeeeee-1111-2222-3333-444444444444"
        fun spawned(id: String, task: String) = JSONObject()
            .put("seq", 1).put("ts", 1762650000.0).put("source", "vm").put("type", "subagent.spawned")
            .put("summary", task)
            .put("detail", JSONObject().put("agent_id", id).put("task", task))
        val dropped = JSONObject()
            .put("seq", 3).put("ts", 1762650200.0).put("source", "vm").put("type", "subagent.dropped")
            .put("summary", "left the lists")
            .put("detail", JSONObject().put("agent_id", agentA).put("reason", "left the monitor lists without a recorded finish"))
        val response = JSONObject()
            .put("events", JSONArray().put(spawned(agentA, "Task A")).put(spawned(agentB, "Task B")).put(dropped))
            .put("sources", JSONObject().put("vm_events", "ok"))
            .put("counts", JSONObject().put("skipped_bad_lines", 0))
        val snap = agentsFromRuntimeEvents(response.toString(), now)
        assertEquals(listOf("Task B"), snap.running.map { it.task })
        assertTrue(snap.finished.isEmpty()) // dropped is NOT a finish
    }

    @Test fun parserHeartbeatUpdatesLastActive() {
        val now = 1_762_650_500_000L
        val agent = "ffffffff-1111-2222-3333-444444444444"
        val spawned = JSONObject()
            .put("seq", 1).put("ts", 1762650000.0).put("source", "vm").put("type", "subagent.spawned")
            .put("summary", "Long task")
            .put("detail", JSONObject().put("agent_id", agent).put("task", "Long task"))
        val beat = JSONObject()
            .put("seq", 2).put("ts", 1762650400.0).put("source", "vm").put("type", "subagent.heartbeat")
            .put("summary", "still running")
            .put("detail", JSONObject().put("agent_id", agent).put("last_active_age_sec", 49))
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray().put(spawned).put(beat))
                .put("sources", JSONObject().put("vm_events", "ok"))
                .put("counts", JSONObject().put("skipped_bad_lines", 0)).toString(),
            now,
        )
        assertEquals(1, snap.running.size)
        assertEquals(49L, snap.running[0].lastActiveSec)
    }

    @Test fun parserLatestActivityFooterAndMissingFeed() {
        val now = 1_762_650_500_000L
        val turn = JSONObject()
            .put("seq", 1).put("ts", 1762650000.0).put("source", "chat").put("type", "turn_done")
            .put("summary", "did the thing").put("detail", JSONObject())
        val file = JSONObject()
            .put("seq", 2).put("ts", 1762650100.0).put("source", "bridge").put("type", "file.write")
            .put("summary", "write /tmp/x.txt").put("detail", JSONObject().put("path", "/tmp/x.txt"))
        // No vm_events key in sources: the feed was never wired.
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray().put(turn).put(file))
                .put("sources", JSONObject().put("chat", "ok").put("bridge", "ok"))
                .put("counts", JSONObject().put("skipped_bad_lines", 0)).toString(),
            now,
        )
        assertEquals(0, snap.running.size)
        assertEquals("file.write — write /tmp/x.txt", snap.latestActivity)
        assertEquals("missing", snap.vmEventsSource)
    }

    @Test fun parserErrorFeedReportsSkippedLines() {
        val now = 1_762_650_500_000L
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray())
                .put("sources", JSONObject().put("chat", "ok").put("bridge", "ok").put("vm_events", "error"))
                .put("counts", JSONObject().put("skipped_bad_lines", 3).put("returned", 0)).toString(),
            now,
        )
        assertEquals("error", snap.vmEventsSource)
        assertEquals(3, snap.skippedBadLines)
        assertNull(snap.latestActivity)
    }

    // ---- 100.018 §5.6 remainders (Worker D): tests 9, 8, 5, 7 ----

    // §5.6 test 9: malformed input never throws. A top-level parse
    // failure returns AgentsSnapshot.EMPTY exactly (AgentsRepository.kt
    // :167, runCatching { JSONObject(responseJson) } ?: return EMPTY).

    @Test fun test9BlankStringReturnsEmpty() {
        val now = 1_762_650_500_000L
        val snap = agentsFromRuntimeEvents("", now)
        assertEquals(AgentsSnapshot.EMPTY, snap)
    }

    // §5.6 test 9: a well-formed object with no "events" array is NOT
    // AgentsSnapshot.EMPTY — the parser still stamps updatedAtMs with
    // nowMs (AgentsRepository.kt:247-253) — so this asserts the empty
    // contents + defaults instead of EMPTY equality. Asserting EMPTY
    // here would fail on the PC; the empty-content contract is the
    // behaviour the parser actually provides.

    @Test fun test9MissingEventsArrayReturnsEmptyContents() {
        val now = 1_762_650_500_000L
        val snap = agentsFromRuntimeEvents("{}", now)
        assertEquals(0, snap.totalCount)
        assertTrue(snap.running.isEmpty())
        assertTrue(snap.finished.isEmpty())
        assertEquals(0L, snap.newestEventTsMs)
        assertNull(snap.latestActivity)
        assertEquals("missing", snap.vmEventsSource)
        assertEquals(0, snap.skippedBadLines)
        assertEquals(now, snap.updatedAtMs)
    }

    @Test fun test9TopLevelArrayReturnsEmpty() {
        val now = 1_762_650_500_000L
        val snap = agentsFromRuntimeEvents("[{\"type\": \"subagent.spawned\"}]", now)
        assertEquals(AgentsSnapshot.EMPTY, snap)
    }

    @Test fun test9TopLevelBareStringReturnsEmpty() {
        val now = 1_762_650_500_000L
        val snap = agentsFromRuntimeEvents("hello", now)
        assertEquals(AgentsSnapshot.EMPTY, snap)
    }

    // §5.6 test 9: "events" present but not an array — optJSONArray
    // returns null, so the parser takes the same empty-contents path
    // as a missing array (updatedAtMs = nowMs, not EMPTY; see the
    // note on test9MissingEventsArrayReturnsEmptyContents).

    @Test fun test9EventsNotArrayReturnsEmptyContents() {
        val now = 1_762_650_500_000L
        val snap = agentsFromRuntimeEvents(
            JSONObject().put("events", "not-an-array").toString(),
            now,
        )
        assertEquals(0, snap.totalCount)
        assertTrue(snap.running.isEmpty())
        assertTrue(snap.finished.isEmpty())
        assertEquals(0L, snap.newestEventTsMs)
        assertNull(snap.latestActivity)
        assertEquals("missing", snap.vmEventsSource)
        assertEquals(0, snap.skippedBadLines)
        assertEquals(now, snap.updatedAtMs)
    }

    // §5.6 test 9 ("non-object lines"): entries in the events array
    // that are not JSON objects are skipped individually
    // (getOrNull() ?: continue, AgentsRepository.kt:178) and must not
    // abort parsing of the valid events that follow them.

    @Test fun test9NonObjectEventEntriesSkipped() {
        val now = 1_762_650_500_000L
        val spawned = JSONObject()
            .put("seq", 4).put("ts", 1762650000.0).put("source", "vm").put("type", "subagent.spawned")
            .put("summary", "Do the thing")
            .put("detail", JSONObject().put("agent_id", "aaaaaaaa-1111-2222-3333-444444444444").put("task", "Do the thing"))
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray().put("not-an-object").put(42).put(JSONObject.NULL).put(spawned))
                .put("sources", JSONObject().put("vm_events", "ok"))
                .put("counts", JSONObject().put("skipped_bad_lines", 0)).toString(),
            now,
        )
        assertEquals(1, snap.running.size)
        assertEquals("Do the thing", snap.running[0].task)
        assertEquals(0, snap.finished.size)
    }

    // §5.6 test 8 remainder: the turn_/file_ branch runs BEFORE any
    // agent_id is read (AgentsRepository.kt:189-195, `continue`), so
    // an id-like field in the detail must still not create a card —
    // while latestActivity still records the event.

    @Test fun test8TurnEventWithAgentIdCreatesNoCard() {
        val now = 1_762_650_500_000L
        val turn = JSONObject()
            .put("seq", 1).put("ts", 1762650000.0).put("source", "chat").put("type", "turn_done")
            .put("summary", "did the thing")
            .put("detail", JSONObject().put("agent_id", "aaaaaaaa-1111-2222-3333-444444444444").put("task", "not an agent"))
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray().put(turn))
                .put("sources", JSONObject().put("vm_events", "ok"))
                .put("counts", JSONObject().put("skipped_bad_lines", 0)).toString(),
            now,
        )
        assertEquals(0, snap.totalCount)
        assertTrue(snap.running.isEmpty())
        assertTrue(snap.finished.isEmpty())
        assertEquals("turn_done — did the thing", snap.latestActivity)
    }

    @Test fun test8FileEventWithAgentCreatesNoCard() {
        val now = 1_762_650_500_000L
        val file = JSONObject()
            .put("seq", 1).put("ts", 1762650000.0).put("source", "bridge").put("type", "file.write")
            .put("summary", "write /tmp/x.txt")
            .put("detail", JSONObject().put("agent", "bbbbbbbb-1111-2222-3333-444444444444").put("path", "/tmp/x.txt"))
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray().put(file))
                .put("sources", JSONObject().put("vm_events", "ok"))
                .put("counts", JSONObject().put("skipped_bad_lines", 0)).toString(),
            now,
        )
        assertEquals(0, snap.totalCount)
        assertTrue(snap.running.isEmpty())
        assertTrue(snap.finished.isEmpty())
        assertEquals("file.write — write /tmp/x.txt", snap.latestActivity)
    }

    // §5.6 test 5 remainder: chatLabel from detail.chatLabel
    // (camelCase fallback, AgentsRepository.kt:200-201) on the
    // spawned branch.

    @Test fun test5ChatLabelCamelCaseFallbackOnSpawn() {
        val now = 1_762_650_500_000L
        val spawned = JSONObject()
            .put("seq", 1).put("ts", 1762650000.0).put("source", "vm").put("type", "subagent.spawned")
            .put("summary", "Do the thing")
            .put("detail", JSONObject().put("agent_id", "aaaaaaaa-1111-2222-3333-444444444444")
                .put("task", "Do the thing").put("chatLabel", "DeskHand chat"))
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray().put(spawned))
                .put("sources", JSONObject().put("vm_events", "ok"))
                .put("counts", JSONObject().put("skipped_bad_lines", 0)).toString(),
            now,
        )
        assertEquals(1, snap.running.size)
        assertEquals("DeskHand chat", snap.running[0].chatLabel)
    }

    // §5.6 test 5 remainder: a finished event for an agent with no
    // prior spawn takes its label from its own detail.chat_label
    // (finished branch: label = chatLabel ?: prior?.chatLabel,
    // AgentsRepository.kt:224 — chatLabel is read from the detail
    // for every subagent.* event, :200-202).

    @Test fun test5FinishedCardUsesDetailChatLabelWithoutPriorSpawn() {
        val now = 1_762_650_500_000L
        val finished = JSONObject()
            .put("seq", 1).put("ts", 1762650400.0).put("source", "vm").put("type", "subagent.finished")
            .put("summary", "done")
            .put("detail", JSONObject().put("agent_id", "cccccccc-1111-2222-3333-444444444444")
                .put("task", "Build it").put("chat_label", "Main chat")
                .put("result", "done").put("duration_sec", 420))
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray().put(finished))
                .put("sources", JSONObject().put("vm_events", "ok"))
                .put("counts", JSONObject().put("skipped_bad_lines", 0)).toString(),
            now,
        )
        assertEquals(1, snap.finished.size)
        assertEquals("Main chat", snap.finished[0].chatLabel)
        assertEquals(AgentStatus.DONE, snap.finished[0].status)
        assertEquals(420L, snap.finished[0].durationSec)
    }

    // §5.6 test 5 remainder (label inheritance): a finished event
    // whose own detail carries no label inherits the spawn's label.

    @Test fun test5FinishedCardInheritsSpawnLabelWhenDetailHasNone() {
        val now = 1_762_650_500_000L
        val agent = "dddddddd-1111-2222-3333-444444444444"
        val spawned = JSONObject()
            .put("seq", 1).put("ts", 1762650000.0).put("source", "vm").put("type", "subagent.spawned")
            .put("summary", "Build it")
            .put("detail", JSONObject().put("agent_id", agent).put("task", "Build it").put("chat_label", "Main chat"))
        val finished = JSONObject()
            .put("seq", 2).put("ts", 1762650400.0).put("source", "vm").put("type", "subagent.finished")
            .put("summary", "done")
            .put("detail", JSONObject().put("agent_id", agent).put("result", "done").put("duration_sec", 420))
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray().put(spawned).put(finished))
                .put("sources", JSONObject().put("vm_events", "ok"))
                .put("counts", JSONObject().put("skipped_bad_lines", 0)).toString(),
            now,
        )
        assertTrue(snap.running.isEmpty())
        assertEquals(1, snap.finished.size)
        assertEquals("Main chat", snap.finished[0].chatLabel)
    }

    // §5.6 test 7 remainder: both defaults together — no sources
    // object and no counts object at all (existing tests only cover
    // the present case, or a sources object missing just the
    // vm_events key).

    @Test fun test7DefaultsWhenCountsAndSourcesAbsent() {
        val now = 1_762_650_500_000L
        val snap = agentsFromRuntimeEvents(
            JSONObject().put("events", JSONArray()).toString(),
            now,
        )
        assertEquals("missing", snap.vmEventsSource)
        assertEquals(0, snap.skippedBadLines)
    }

    // §5.6 test 7 remainder: counts absent on its own — sources
    // present and "ok", so only skippedBadLines must default to 0.

    @Test fun test7SkippedBadLinesDefaultsZeroWhenCountsAbsent() {
        val now = 1_762_650_500_000L
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray())
                .put("sources", JSONObject().put("chat", "ok").put("bridge", "ok").put("vm_events", "ok")).toString(),
            now,
        )
        assertEquals("ok", snap.vmEventsSource)
        assertEquals(0, snap.skippedBadLines)
    }

    // §5.6 test 7 remainder: sources absent on its own — counts
    // present with a non-zero value, so only vmEventsSource must
    // default to "missing" while the counts value is still read.

    @Test fun test7VmEventsSourceDefaultsMissingWhenSourcesAbsent() {
        val now = 1_762_650_500_000L
        val snap = agentsFromRuntimeEvents(
            JSONObject()
                .put("events", JSONArray())
                .put("counts", JSONObject().put("skipped_bad_lines", 2).put("returned", 0)).toString(),
            now,
        )
        assertEquals("missing", snap.vmEventsSource)
        assertEquals(2, snap.skippedBadLines)
    }

    // ---- §6.2 worker line (footer fact #2) ----

    @Test fun workerFooterLine() {
        assertEquals("Worker status unknown", workerFooterLine(null))
        val ok = parseHealthSnapshot(
            JSONObject().put("components", JSONObject().put("worker", JSONObject().put("status", "OK").put("last_ok", 1762650000L))),
        )!!
        assertEquals("Worker connected", workerFooterLine(ok))
        val down = parseHealthSnapshot(
            JSONObject().put("components", JSONObject().put("worker", JSONObject().put("status", "DOWN").put("last_ok", 1762650000L))),
        )!!
        assertTrue(workerFooterLine(down).startsWith("Worker disconnected since "))
        val downNoTime = parseHealthSnapshot(
            JSONObject().put("components", JSONObject().put("worker", JSONObject().put("status", "DOWN").put("last_ok", 0L))),
        )!!
        assertEquals("Worker disconnected", workerFooterLine(downNoTime))
    }

    // ---- Model fetch path: rebase, keep-last-good, no overlap ----

    @Test fun pollFetchRebasesStreamCursorAndDedupsRedelivery() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.fetchAgentsSnapshot()
        idleMain()
        assertEquals(1, fetchRpcs(link).size)
        val rpc = fetchRpcs(link)[0].optString("rpc")
        val spawned = JSONObject()
            .put("seq", 5).put("ts", 1762650000.0).put("source", "vm").put("type", "subagent.spawned")
            .put("summary", "Build it")
            .put("detail", JSONObject().put("agent_id", "11111111-2222-3333-4444-555555555555").put("task", "Build it").put("chat_label", "Main chat"))
        val result = runtimeResult(JSONArray().put(spawned), 5, "missing")
        Model.handleChatResponse(runtimeStreamReply(rpc, result))
        assertEquals(5L, Model.streamBuffer.frontierSeq)
        assertEquals(1, Model.agentsSnapshot.running.size)
        assertEquals("Build it", Model.agentsSnapshot.running[0].task)
        // Push now re-delivers the same window event: the rebase seeded
        // its dedup key, so it dedups — no duplicate card, no resync.
        link.chatRequests.clear()
        Model.handleStreamFrame(eventsFrame(spawned, latestSeq = 5))
        assertEquals(1, Model.agentsSnapshot.running.size)
        assertTrue(fetchRpcs(link).isEmpty())
        assertEquals(5L, Model.streamBuffer.frontierSeq)
    }

    @Test fun failedPollKeepsLastGoodSnapshot() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.fetchAgentsSnapshot()
        idleMain()
        val rpc1 = fetchRpcs(link)[0].optString("rpc")
        val spawned = JSONObject()
            .put("seq", 1).put("ts", 1762650000.0).put("source", "vm").put("type", "subagent.spawned")
            .put("summary", "Build it")
            .put("detail", JSONObject().put("agent_id", "22222222-2222-3333-4444-555555555555").put("task", "Build it"))
        Model.handleChatResponse(runtimeStreamReply(rpc1, runtimeResult(JSONArray().put(spawned), 1, "ok")))
        assertEquals(1, Model.agentsSnapshot.running.size)
        val statusBefore = Model.chatStatus
        // Second fetch fails: the guard clears, the snapshot stays, and
        // the chat status line is untouched (design §B2).
        link.chatRequests.clear()
        Model.fetchAgentsSnapshot()
        idleMain()
        val rpc2 = fetchRpcs(link)[0].optString("rpc")
        Model.handleChatResponse(
            JSONObject().put("op", "runtime_stream").put("ok", false).put("rpc", rpc2).put("error", "boom"),
        )
        assertFalse(Model.agentsLoading)
        assertEquals(1, Model.agentsSnapshot.running.size)
        assertEquals(statusBefore, Model.chatStatus)
    }

    @Test fun fetchAgentsSnapshotNoOverlappingFetches() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.fetchAgentsSnapshot()
        Model.fetchAgentsSnapshot()
        assertEquals(1, fetchRpcs(link).size)
        // No session at all: no request is attempted (the in-flight guard
        // is still holding the first fetch, and the chat flag is down).
        Model.chatConnected = false
        Model.fetchAgentsSnapshot()
        idleMain()
        assertEquals(1, fetchRpcs(link).size)
    }
}
