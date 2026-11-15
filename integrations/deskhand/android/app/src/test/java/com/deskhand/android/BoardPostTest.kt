package com.deskhand.android

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 100.033 (message board, spec §10.1 V1–V6): BoardPost model + the
 * event-leg parser boardPostsFromRuntimeEvents, the merge/dedup helper,
 * the per-kind display caps, and the footer honesty line.
 *
 * Pure logic only — plain JUnit, no Robolectric, no Android framework.
 * The VM cannot run these tests (no Android SDK / no Gradle here); they
 * run at Gate 4 on the PC (`testDebugUnitTest`). Do not claim they pass
 * on the VM.
 *
 * NOTE (Step 0 re-scope): the Monitor snapshot is unreachable from the
 * phone, so there is no snapshot leg and no parseAgentsSnapshot — derived
 * spawn/result posts come from subagent.* events in the runtime_stream
 * window, and their origin is LIVE. The "both legs" dedup case in V3 is
 * therefore exercised as merge-of-two-derivations and re-parse-after-
 * resync, not as snapshot-vs-event legs.
 */
class BoardPostTest {

    // nowMs is 2026-11-10 ~06:30 EST in epoch ms; event ts values are
    // epoch SECONDS floats (the live service format — never ISO strings).
    private val nowMs = 1_770_000_000_000L
    private val t0 = 1_769_999_000.0 // nowMs - 1000 s
    private val t1 = 1_769_999_100.0
    private val t2 = 1_769_999_252.0
    private val t3 = 1_769_999_500.0

    private fun ev(
        seq: Long,
        ts: Double,
        type: String,
        actor: String = "",
        summary: String = "",
        detail: JSONObject = JSONObject(),
    ): JSONObject = JSONObject()
        .put("seq", seq).put("ts", ts).put("source", "vm")
        .put("type", type).put("actor", actor).put("summary", summary)
        .put("detail", detail)

    private fun stream(vararg events: JSONObject, resynced: Boolean = false): String =
        JSONObject()
            .put("events", JSONArray(events.toList()))
            .put("latest_seq", events.size)
            .put("sources", JSONObject().put("vm_events", "ok"))
            .put("counts", JSONObject().put("skipped_bad_lines", 0))
            .put("resynced", resynced)
            .toString()

    private fun spawn(seq: Long, ts: Double, agent: String, task: String, chatLabel: String = "DeskHand chat"): JSONObject =
        ev(seq, ts, "subagent.spawned", actor = agent, summary = task,
            detail = JSONObject().put("agent_id", agent).put("task", task).put("chat_label", chatLabel))

    private fun finish(seq: Long, ts: Double, agent: String, result: String? = null, durationSec: Long? = null): JSONObject {
        val detail = JSONObject().put("agent_id", agent)
        if (result != null) detail.put("result", result)
        if (durationSec != null) detail.put("duration_sec", durationSec)
        return ev(seq, ts, "subagent.finished", actor = agent, summary = result ?: "", detail = detail)
    }

    // ---------------------------------------------------------------- V1 --
    @Test
    fun v1_emptyStringReturnsEmpty() {
        assertEquals(emptyList<BoardPost>(), boardPostsFromRuntimeEvents("", nowMs))
    }

    @Test
    fun v1_malformedJsonReturnsEmpty() {
        assertEquals(emptyList<BoardPost>(), boardPostsFromRuntimeEvents("hello", nowMs))
        assertEquals(emptyList<BoardPost>(), boardPostsFromRuntimeEvents("[{\"type\": \"subagent.spawned\"}]", nowMs))
        assertEquals(emptyList<BoardPost>(), boardPostsFromRuntimeEvents("{", nowMs))
    }

    @Test
    fun v1_noEventsReturnsEmpty() {
        assertEquals(emptyList<BoardPost>(), boardPostsFromRuntimeEvents("{}", nowMs))
        assertEquals(
            emptyList<BoardPost>(),
            boardPostsFromRuntimeEvents(JSONObject().put("sources", JSONObject()).toString(), nowMs),
        )
    }

    @Test
    fun v1_malformedEventsSkippedNeverThrow() {
        val json = JSONObject()
            .put("events", JSONArray().put(123).put("x").put(JSONObject()).put(JSONObject.NULL))
            .put("sources", JSONObject().put("vm_events", "ok"))
            .toString()
        assertEquals(emptyList<BoardPost>(), boardPostsFromRuntimeEvents(json, nowMs))
    }

    // ---------------------------------------------------------------- V2 --
    @Test
    fun v2_runningAgentDerivesOneSpawnPost() {
        val posts = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "aaaa1111", "Fix the leak in the downstairs bathroom")), nowMs)
        assertEquals(1, posts.size)
        val post = posts.single()
        assertEquals(BoardPostKind.SPAWN, post.kind)
        assertEquals(BoardAuthorKind.SUBAGENT, post.authorKind)
        assertEquals(BoardOrigin.LIVE, post.origin)
        assertEquals(AgentStatus.RUNNING, post.status)
        assertEquals("Fix the leak in the downstairs bathroom", post.text)
        assertEquals("aaaa1111", post.agentId)
        assertEquals(1_769_999_000_000L, post.tsMs)
        assertEquals(1000L, post.elapsedSec)
        assertNull(post.durationSec)
    }

    @Test
    fun v2_newlyFinishedAgentDerivesResultPost() {
        val posts = boardPostsFromRuntimeEvents(
            stream(spawn(1, t0, "bbbb2222", "Polish the icons"), finish(2, t2, "bbbb2222", "Fixed the leak", 252)),
            nowMs,
        )
        assertEquals(2, posts.size)
        val result = posts.single { it.kind == BoardPostKind.RESULT }
        assertEquals("Fixed the leak", result.text)
        assertEquals(AgentStatus.DONE, result.status)
        assertEquals(252L, result.durationSec)
        assertEquals(1_769_999_252_000L, result.tsMs)
        assertEquals(BoardOrigin.LIVE, result.origin)
        // The spawn post is still there, once — no duplicate of it.
        assertEquals(1, posts.count { it.kind == BoardPostKind.SPAWN })
    }

    @Test
    fun v2_finishWithoutSpawnStillDerivesResult() {
        val posts = boardPostsFromRuntimeEvents(stream(finish(1, t2, "cccc3333", "Done, no spawn seen")), nowMs)
        assertEquals(1, posts.size)
        assertEquals(BoardPostKind.RESULT, posts.single().kind)
        assertEquals(AgentStatus.DONE, posts.single().status)
        assertNull(posts.single().durationSec)
    }

    @Test
    fun v2_stillRunningAgentDerivesNoNewPost() {
        val heartbeat = ev(2, t1, "subagent.heartbeat", actor = "dddd4444",
            detail = JSONObject().put("agent_id", "dddd4444"))
        val posts = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "dddd4444", "Keep polishing"), heartbeat), nowMs)
        assertEquals(1, posts.size)
        assertEquals(BoardPostKind.SPAWN, posts.single().kind)
    }

    @Test
    fun v2_disappearedAgentDerivesNoPost() {
        // The window holds only a turn event; agent "eeee5555" appears
        // nowhere — disappearance is a feed gap, not an event.
        val turn = ev(1, t0, "turn_done", summary = "did something")
        val posts = boardPostsFromRuntimeEvents(stream(turn), nowMs)
        assertEquals(emptyList<BoardPost>(), posts)
        assertTrue(posts.none { it.agentId == "eeee5555" })
    }

    @Test
    fun v2_finishWithNoResultIsLabelledNotInvented() {
        val posts = boardPostsFromRuntimeEvents(
            stream(spawn(1, t0, "ffff6666", "Do the thing"), finish(2, t2, "ffff6666")),
            nowMs,
        )
        val result = posts.single { it.kind == BoardPostKind.RESULT }
        assertTrue(result.text.startsWith("no result reported"))
        assertTrue(result.text.contains("Do the thing"))
        assertFalse(result.text.contains("Fixed"))
    }

    @Test
    fun v2_droppedAgentGetsNoFinishPost() {
        val dropped = ev(2, t1, "subagent.dropped", actor = "gggg7777",
            detail = JSONObject().put("agent_id", "gggg7777"))
        val posts = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "gggg7777", "Doomed work"), dropped), nowMs)
        assertEquals(1, posts.size)
        assertEquals(BoardPostKind.SPAWN, posts.single().kind)
        assertTrue(posts.none { it.kind == BoardPostKind.RESULT })
    }

    @Test
    fun v2_stepNoteDerivesLiveStepPost() {
        val step = ev(2, t1, "board.post", actor = "hhhh8888", summary = "Found the root cause",
            detail = JSONObject().put("agent_id", "hhhh8888").put("kind", "step").put("text", "Found the root cause: the token was stale"))
        val posts = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "hhhh8888", "Hunt the bug"), step), nowMs)
        val stepPost = posts.single { it.kind == BoardPostKind.STEP }
        assertEquals("Found the root cause: the token was stale", stepPost.text)
        assertEquals(BoardOrigin.LIVE, stepPost.origin)
        assertEquals(AgentStatus.RUNNING, stepPost.status)
    }

    // ---------------------------------------------------------------- V3 --
    @Test
    fun v3_sameEventTwiceThroughOneLegRendersOnce() {
        val event = spawn(1, t0, "iiii9999", "Do it twice")
        val posts = boardPostsFromRuntimeEvents(stream(event, event), nowMs)
        assertEquals(1, posts.size)
    }

    @Test
    fun v3_samePostThroughBothLegsRendersOnce() {
        val derived = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "jjjj0000", "Shared post")), nowMs)
        assertEquals(1, derived.size)
        // The same derivation arriving again (second leg / re-parse).
        assertEquals(1, mergeBoardPosts(derived, derived).size)
        val again = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "jjjj0000", "Shared post")), nowMs)
        assertEquals(1, mergeBoardPosts(derived, again).size)
    }

    @Test
    fun v3_resyncRendersOnce() {
        val first = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "kkkk1111", "Before restart")), nowMs)
        // Simulated resync: the same window re-derived (seq may be
        // re-numbered; the merge still dedups by id).
        val resynced = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "kkkk1111", "Before restart"), resynced = true), nowMs)
        assertEquals(1, mergeBoardPosts(first, resynced).size)
        assertEquals(first.single().id, mergeBoardPosts(first, resynced).single().id)
    }

    // ---------------------------------------------------------------- V4 --
    @Test
    fun v4_lateEventSlotsMidList() {
        // JSON order is newest, oldest (late), then the middle two.
        val posts = boardPostsFromRuntimeEvents(
            stream(
                spawn(1, 1_769_999_400.0, "late-a", "newest"),
                spawn(2, 1_769_999_000.0, "late-b", "oldest, arrived late"),
                spawn(3, 1_769_999_300.0, "late-c", "third"),
                spawn(4, 1_769_999_200.0, "late-d", "second"),
            ),
            nowMs,
        )
        assertEquals(listOf(1_769_999_400_000L, 1_769_999_300_000L, 1_769_999_200_000L, 1_769_999_000_000L), posts.map { it.tsMs })
        assertEquals("oldest, arrived late", posts.last().text)
    }

    @Test
    fun v4_equalTsBreaksById() {
        val posts = boardPostsFromRuntimeEvents(
            stream(spawn(2, t0, "mmmm3333", "second seq first"), spawn(1, t0, "llll2222", "first seq second")),
            nowMs,
        )
        assertEquals(listOf("vm:seq:1", "vm:seq:2"), posts.map { it.id })
    }

    // ---------------------------------------------------------------- V5 --
    @Test
    fun v5_spawnCap300() {
        val long = "x".repeat(400)
        val posts = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "nnnn4444", long)), nowMs)
        val post = posts.single()
        assertEquals(300, boardDisplayText(post, expanded = false).length)
        assertEquals(long.take(300), boardDisplayText(post, expanded = false))
        assertTrue(boardNeedsShowMore(post))
        assertEquals(400, boardDisplayText(post, expanded = true).length)
    }

    @Test
    fun v5_stepAndResultCap500() {
        val long = "y".repeat(600)
        val step = ev(2, t1, "board.post", actor = "oooo5555",
            detail = JSONObject().put("agent_id", "oooo5555").put("kind", "step").put("text", long))
        val posts = boardPostsFromRuntimeEvents(
            stream(spawn(1, t0, "oooo5555", "Step work"), step, finish(3, t2, "oooo5555", long)),
            nowMs,
        )
        val stepPost = posts.single { it.kind == BoardPostKind.STEP }
        val resultPost = posts.single { it.kind == BoardPostKind.RESULT }
        assertEquals(500, boardDisplayText(stepPost, expanded = false).length)
        assertEquals(500, boardDisplayText(resultPost, expanded = false).length)
        assertTrue(boardNeedsShowMore(stepPost))
        assertEquals(600, boardDisplayText(stepPost, expanded = true).length)
    }

    @Test
    fun v5_relayCap800() {
        val long = "z".repeat(900)
        val relay = ev(1, t1, "board.post", actor = "GPT",
            detail = JSONObject().put("kind", "relay").put("text", long)
                .put("sourceChatId", "chat-1").put("sourceMessageId", "msg-7")
                .put("saidTs", t0))
        val post = boardPostsFromRuntimeEvents(stream(relay), nowMs).single()
        assertEquals(800, boardDisplayText(post, expanded = false).length)
        assertEquals(long.take(800), boardDisplayText(post, expanded = false))
        assertTrue(boardNeedsShowMore(post))
        assertEquals(900, boardDisplayText(post, expanded = true).length)
    }

    @Test
    fun v5_shortTextNeedsNoShowMore() {
        val posts = boardPostsFromRuntimeEvents(stream(spawn(1, t0, "pppp6666", "short")), nowMs)
        val post = posts.single()
        assertEquals("short", boardDisplayText(post, expanded = false))
        assertFalse(boardNeedsShowMore(post))
    }

    // ---------------------------------------------------------------- V6 --
    @Test
    fun v6_relayPostCarriesOriginAndRef() {
        val relay = ev(1, t1, "board.post", actor = "GPT", summary = "The gateway fix is host-side",
            detail = JSONObject().put("kind", "relay").put("text", "The gateway fix is host-side")
                .put("sourceChatId", "chat-1").put("sourceMessageId", "msg-7")
                .put("saidTs", t0).put("relayedTs", t3))
        val post = boardPostsFromRuntimeEvents(stream(relay), nowMs).single()
        assertEquals(BoardPostKind.RELAY, post.kind)
        assertEquals(BoardAuthorKind.OUTSIDE_AI, post.authorKind)
        assertEquals(BoardOrigin.RELAY, post.origin)
        assertEquals("GPT", post.authorName)
        assertEquals("chat-1", post.sourceChatId)
        assertEquals("msg-7", post.sourceMessageId)
        assertEquals("relay:chat-1:msg-7", post.id)
        assertEquals(1_769_999_000_000L, post.tsMs) // said time, not relay time
        assertEquals(1_769_999_500_000L, post.relayedTsMs)
        assertNull(post.status)
        assertNull(post.durationSec)
    }

    @Test
    fun v6_relayWithoutRefIsDropped() {
        val noMsgId = ev(1, t1, "board.post", actor = "GPT",
            detail = JSONObject().put("kind", "relay").put("text", "Orphan quote")
                .put("sourceChatId", "chat-1").put("saidTs", t0))
        assertEquals(emptyList<BoardPost>(), boardPostsFromRuntimeEvents(stream(noMsgId), nowMs))
        val noChatId = ev(1, t1, "board.post", actor = "GPT",
            detail = JSONObject().put("kind", "relay").put("text", "Orphan quote")
                .put("sourceMessageId", "msg-7").put("saidTs", t0))
        assertEquals(emptyList<BoardPost>(), boardPostsFromRuntimeEvents(stream(noChatId), nowMs))
    }

    @Test
    fun v6_everyPostHasAnOriginChip() {
        val relay = ev(3, t1, "board.post", actor = "Codex",
            detail = JSONObject().put("kind", "relay").put("text", "Ship it")
                .put("sourceChatId", "chat-2").put("sourceMessageId", "msg-9")
                .put("saidTs", t0))
        val posts = boardPostsFromRuntimeEvents(
            stream(spawn(1, t0, "qqqq7777", "Work"), finish(2, t2, "qqqq7777", "Done"), relay),
            nowMs,
        )
        assertEquals(3, posts.size)
        // An origin chip must exist for every post: non-blank label, and
        // one of the three honest origins (a post without one fails).
        for (post in posts) {
            val chip = post.origin.name.lowercase()
            assertTrue(chip.isNotBlank())
            assertTrue(chip in setOf("live", "snapshot", "relay"))
            assertTrue(post.id.isNotBlank())
        }
    }

    @Test
    fun v6_footerNamesMissingLegs() {
        // Event leg unwired + relay not started: both gaps named.
        val missing = boardFooterLine(2, "missing", nowMs - 10_000, nowMs)
        assertTrue(missing.startsWith("Live · 2 posts"))
        assertTrue(missing.contains("live step notes not wired yet"))
        assertTrue(missing.contains("outside-AI relay not started"))
        // Event leg wired: the step-notes gap drops, the relay gap stays.
        val wired = boardFooterLine(2, "ok", nowMs - 10_000, nowMs)
        assertFalse(wired.contains("live step notes not wired yet"))
        assertTrue(wired.contains("outside-AI relay not started"))
        // Dead poll: STALE, and "Live" must not appear.
        val stale = boardFooterLine(2, "ok", nowMs - 300_000, nowMs)
        assertTrue(stale.startsWith("STALE"))
        assertFalse(stale.contains("Live ·"))
        // Never fetched: honest, not "Live".
        val never = boardFooterLine(0, "missing", 0L, nowMs)
        assertFalse(never.contains("Live ·"))
    }
}
