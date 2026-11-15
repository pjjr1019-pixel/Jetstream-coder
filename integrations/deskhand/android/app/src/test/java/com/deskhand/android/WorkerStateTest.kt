package com.deskhand.android

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 100.037 (E12 watchdog, spec §7.4, §10.4): the Muse-worker state parser
 * workerStateFromRuntimeEvents + the second drawer footer line
 * museWorkerFooterLine, plus the relay-line regression guard (the
 * existing workerFooterLine is relay-based and must stay that way —
 * spec §13 R12, two facts, never one blended claim).
 *
 * Pure logic only — plain JUnit, no Robolectric, no Android framework.
 * The VM cannot run these tests (no Android SDK / no Gradle here); they
 * run at Gate 4 on the PC (`testDebugUnitTest`). Do not claim they pass
 * on the VM.
 *
 * Clock strings in the expected lines are built with the same
 * device-local HH:mm:ss / HH:mm formatters the production code uses
 * (hhMmSs / hhMm below), so the assertions verify the assembly —
 * prefix, punctuation, order, live countdown math — on any timezone,
 * not the clock itself.
 */
class WorkerStateTest {

    // nowMs is 2026-11-10 ~06:30 EST in epoch ms; event ts values are
    // epoch SECONDS floats (the live service format — never ISO strings),
    // except where a detail timestamp is deliberately ISO-8601.
    private val nowMs = 1_770_000_000_000L
    private val tDisc = 1_769_999_000.0 // disconnect / first miss
    private val tAtt1 = 1_769_999_003.0 // attempt 1, 3 s later
    private val tStop = 1_769_999_600.0 // auto-reconnect stopped
    private val tRec = 1_769_999_700.0 // verified recovery
    private val tDiscIso = "2026-02-02T02:23:20Z" // == tDisc, ISO form

    private fun hhMmSs(epochSec: Long): String =
        SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(epochSec * 1000))

    private fun hhMm(epochSec: Long): String =
        SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(epochSec * 1000))

    private fun ev(
        seq: Long,
        ts: Double,
        type: String,
        summary: String = "",
        detail: JSONObject = JSONObject(),
    ): JSONObject = JSONObject()
        .put("seq", seq).put("ts", ts).put("source", "vm")
        .put("type", type).put("summary", summary)
        .put("detail", detail)

    private fun stream(vararg events: JSONObject): String =
        JSONObject()
            .put("events", JSONArray(events.toList()))
            .put("latest_seq", events.size)
            .put("sources", JSONObject().put("vm_events", "ok"))
            .put("counts", JSONObject().put("skipped_bad_lines", 0))
            .toString()

    private fun disconnected(seq: Long, ts: Double, since: Any = ts, signal: String = "not_ready", cause: String = "unknown"): JSONObject =
        ev(seq, ts, "worker_disconnected",
            detail = JSONObject().put("disconnected_since", since).put("signal", signal).put("cause", cause))

    private fun attempt(seq: Long, ts: Double, n: Long, kind: String = "auto", nextDelayS: Long? = null): JSONObject {
        val detail = JSONObject().put("attempt", n).put("kind", kind)
        if (nextDelayS != null) detail.put("next_delay_s", nextDelayS)
        return ev(seq, ts, "worker_reconnect_attempt", detail = detail)
    }

    private fun stopped(seq: Long, ts: Double, attempts: Long, reason: String): JSONObject =
        ev(seq, ts, "worker_auto_reconnect_stopped",
            detail = JSONObject().put("attempts", attempts).put("reason", reason))

    private fun reconnected(seq: Long, ts: Double, downSeconds: Long? = null, at: Any = ts): JSONObject {
        val detail = JSONObject().put("reconnected_at", at)
        if (downSeconds != null) detail.put("down_seconds", downSeconds)
        return ev(seq, ts, "worker_reconnected", detail = detail)
    }

    // ------------------------------------------------------------- Parser --
    @Test
    fun p1_emptyStringReturnsUnknown() {
        assertEquals(MuseWorkerState(), workerStateFromRuntimeEvents("", nowMs))
    }

    @Test
    fun p1_malformedJsonReturnsPreviousUnchanged() {
        val prev = workerStateFromRuntimeEvents(stream(disconnected(1, tDisc)), nowMs)
        assertEquals(prev, workerStateFromRuntimeEvents("hello", nowMs, prev))
        assertEquals(prev, workerStateFromRuntimeEvents("{", nowMs, prev))
        assertEquals(prev, workerStateFromRuntimeEvents("[{\"type\": \"worker_disconnected\"}]", nowMs, prev))
    }

    @Test
    fun p1_noWorkerEventsLeavesPreviousUnchanged() {
        val prev = workerStateFromRuntimeEvents(stream(disconnected(1, tDisc)), nowMs)
        val turn = ev(2, tAtt1, "turn_done", summary = "did something")
        assertEquals(prev, workerStateFromRuntimeEvents(stream(turn), nowMs, prev))
        assertEquals(prev, workerStateFromRuntimeEvents("{}", nowMs, prev))
    }

    @Test
    fun p1_malformedEventsSkippedNeverThrow() {
        val json = JSONObject()
            .put("events", JSONArray().put(123).put("x").put(JSONObject()).put(JSONObject.NULL))
            .put("sources", JSONObject().put("vm_events", "ok"))
            .toString()
        assertEquals(MuseWorkerState(), workerStateFromRuntimeEvents(json, nowMs))
    }

    @Test
    fun p2_disconnectedSetsSinceFromDetailEpoch() {
        val state = workerStateFromRuntimeEvents(stream(disconnected(1, tDisc)), nowMs)
        assertEquals(MuseWorkerConnection.DISCONNECTED, state.connection)
        assertEquals(1_769_999_000_000L, state.disconnectedSinceMs)
        assertEquals(1_769_999_000_000L, state.lastEventTsMs)
        assertEquals(0L, state.lastAttemptMs)
        assertNull(state.stoppedReason)
    }

    @Test
    fun p2_disconnectedSetsSinceFromDetailIso() {
        val state = workerStateFromRuntimeEvents(stream(disconnected(1, tDisc, since = tDiscIso)), nowMs)
        assertEquals(MuseWorkerConnection.DISCONNECTED, state.connection)
        assertEquals(1_769_999_000_000L, state.disconnectedSinceMs)
    }

    @Test
    fun p2_disconnectedWithoutSinceFallsBackToEventTs() {
        val bare = ev(1, tDisc, "worker_disconnected", detail = JSONObject().put("signal", "not_ready"))
        val state = workerStateFromRuntimeEvents(stream(bare), nowMs)
        assertEquals(1_769_999_000_000L, state.disconnectedSinceMs)
    }

    @Test
    fun p3_attemptUpdatesLastAttemptAndDelayKeepsSince() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), attempt(2, tAtt1, 1, nextDelayS = 5)),
            nowMs,
        )
        assertEquals(MuseWorkerConnection.DISCONNECTED, state.connection)
        assertEquals(1_769_999_000_000L, state.disconnectedSinceMs)
        assertEquals(1_769_999_003_000L, state.lastAttemptMs)
        assertEquals(1, state.lastAttemptNumber)
        assertEquals(5L, state.nextDelaySec)
        assertEquals(1, state.attempts)
    }

    @Test
    fun p4_stoppedSetsAttemptsAndReasonClearsDelay() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), attempt(2, tAtt1, 1, nextDelayS = 5), stopped(3, tStop, 10, "cap")),
            nowMs,
        )
        assertEquals(MuseWorkerConnection.DISCONNECTED, state.connection)
        assertEquals(10, state.attempts)
        assertEquals("cap", state.stoppedReason)
        assertNull(state.nextDelaySec)
        assertEquals(1_769_999_600_000L, state.lastEventTsMs)
    }

    @Test
    fun p5_reconnectedSetsVerifiedRecovery() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), attempt(2, tAtt1, 1), reconnected(3, tRec, downSeconds = 106)),
            nowMs,
        )
        assertEquals(MuseWorkerConnection.CONNECTED, state.connection)
        assertEquals(1_769_999_700_000L, state.reconnectedAtMs)
        assertEquals(106L, state.downSeconds)
        // The disconnect episode is cleared; the recovery facts remain.
        assertEquals(0L, state.disconnectedSinceMs)
        assertEquals(0L, state.lastAttemptMs)
        assertNull(state.stoppedReason)
    }

    @Test
    fun p5_reconnectedWithoutPriorStateStillConnects() {
        val state = workerStateFromRuntimeEvents(stream(reconnected(1, tRec, downSeconds = 42)), nowMs)
        assertEquals(MuseWorkerConnection.CONNECTED, state.connection)
        assertEquals(42L, state.downSeconds)
    }

    @Test
    fun p6_olderEventNeverRegressesState() {
        val first = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), attempt(2, tAtt1, 1, nextDelayS = 5)),
            nowMs,
        )
        // A later window re-delivers the original disconnect (older ts)
        // with a DIFFERENT since — the guard must ignore it.
        val again = workerStateFromRuntimeEvents(stream(disconnected(1, tDisc, since = 1_769_999_500.0)), nowMs, first)
        assertEquals(first, again)
        assertEquals(1_769_999_000_000L, again.disconnectedSinceMs)
        assertEquals(1, again.lastAttemptNumber)
    }

    @Test
    fun p7_dotSpellingAccepted() {
        val dotted = ev(1, tDisc, "worker.disconnected",
            detail = JSONObject().put("disconnected_since", tDisc))
        val state = workerStateFromRuntimeEvents(stream(dotted), nowMs)
        assertEquals(MuseWorkerConnection.DISCONNECTED, state.connection)
        assertEquals(1_769_999_000_000L, state.disconnectedSinceMs)
    }

    @Test
    fun p8_unknownWorkerTypeAndOtherEventsIgnored() {
        val state = workerStateFromRuntimeEvents(
            stream(
                ev(1, tDisc, "worker_mystery", detail = JSONObject().put("x", 1)),
                ev(2, tAtt1, "worker", detail = JSONObject()),
                ev(3, tAtt1, "subagent.spawned", detail = JSONObject().put("agent_id", "aaaa1111")),
                ev(4, tAtt1, "board.post", detail = JSONObject().put("kind", "step")),
            ),
            nowMs,
        )
        assertEquals(MuseWorkerState(), state)
    }

    @Test
    fun p9_newDisconnectStartsNewEpisode() {
        val first = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), attempt(2, tAtt1, 1, nextDelayS = 5), stopped(3, tStop, 10, "cap")),
            nowMs,
        )
        val second = workerStateFromRuntimeEvents(stream(disconnected(4, 1_769_999_800.0)), nowMs, first)
        assertEquals(MuseWorkerConnection.DISCONNECTED, second.connection)
        assertEquals(1_769_999_800_000L, second.disconnectedSinceMs)
        assertEquals(0L, second.lastAttemptMs)
        assertEquals(0, second.attempts)
        assertNull(second.stoppedReason)
        assertNull(second.nextDelaySec)
    }

    @Test
    fun p10_attemptAfterStopClearsStopped() {
        // A manual attempt after auto-reconnect stopped: reconnect is
        // trying again, so the "stopped" claim no longer describes the
        // present — the footer must show the attempt, not the stop.
        val first = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), stopped(2, tStop, 10, "cap")),
            nowMs,
        )
        assertEquals("cap", first.stoppedReason)
        val second = workerStateFromRuntimeEvents(
            stream(attempt(3, 1_769_999_650.0, 11, kind = "manual", nextDelayS = 2)),
            nowMs, first,
        )
        assertNull(second.stoppedReason)
        assertEquals(11, second.lastAttemptNumber)
        assertEquals(2L, second.nextDelaySec)
    }

    // ----------------------------------------------------------- Renderer --
    @Test
    fun r1_unknownRendersHonestUnknown() {
        assertEquals("Muse worker status unknown", museWorkerFooterLine(MuseWorkerState(), nowMs))
    }

    @Test
    fun r2_disconnectedFullFormExact() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), attempt(2, tAtt1, 1, nextDelayS = 5)),
            nowMs,
        )
        // nowMs == the attempt time: the full 5 s delay is still ahead.
        assertEquals(
            "Muse worker disconnected since ${hhMmSs(1_769_999_000)}, " +
                "last attempt ${hhMm(1_769_999_003)}, next in 5s",
            museWorkerFooterLine(state, 1_769_999_003_000L),
        )
    }

    @Test
    fun r3_countdownTicksLive() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), attempt(2, tAtt1, 1, nextDelayS = 5)),
            nowMs,
        )
        assertTrue(museWorkerFooterLine(state, 1_769_999_006_000L).endsWith("next in 2s"))
        assertTrue(museWorkerFooterLine(state, 1_769_999_007_000L).endsWith("next in 1s"))
    }

    @Test
    fun r4_countdownDueDropsOffNeverStale() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), attempt(2, tAtt1, 1, nextDelayS = 5)),
            nowMs,
        )
        // Past the scheduled attempt with no new event: the attempt is
        // due, and the line must not show a stale "next in Ns".
        val line = museWorkerFooterLine(state, 1_769_999_009_000L)
        assertFalse(line.contains("next in"))
        assertTrue(line.startsWith("Muse worker disconnected since ${hhMmSs(1_769_999_000)}"))
        assertTrue(line.contains("last attempt ${hhMm(1_769_999_003)}"))
    }

    @Test
    fun r5_noAttemptYet() {
        val state = workerStateFromRuntimeEvents(stream(disconnected(1, tDisc)), nowMs)
        assertEquals(
            "Muse worker disconnected since ${hhMmSs(1_769_999_000)}, no attempt yet",
            museWorkerFooterLine(state, nowMs),
        )
    }

    @Test
    fun r5_disconnectedWithoutSinceTime() {
        // Attempt seen with no disconnect in the window: disconnected,
        // but no since-time is claimed.
        val state = workerStateFromRuntimeEvents(stream(attempt(1, tAtt1, 1, nextDelayS = 5)), nowMs)
        val line = museWorkerFooterLine(state, 1_769_999_003_000L)
        assertTrue(line.startsWith("Muse worker disconnected, last attempt"))
        assertFalse(line.contains("since"))
    }

    @Test
    fun r6_stoppedAfterCap() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), attempt(2, tAtt1, 1), stopped(3, tStop, 10, "cap")),
            nowMs,
        )
        assertEquals(
            "Muse worker disconnected since ${hhMmSs(1_769_999_000)} — " +
                "auto-reconnect stopped after 10 attempts, last attempt ${hhMm(1_769_999_003)}",
            museWorkerFooterLine(state, nowMs),
        )
    }

    @Test
    fun r7_stoppedGrantOffNamesReason() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc, cause = "grant_off"), attempt(2, tAtt1, 1), stopped(3, tStop, 1, "grant-off")),
            nowMs,
        )
        val line = museWorkerFooterLine(state, nowMs)
        assertTrue(line.contains("auto-reconnect stopped after 1 attempts"))
        assertTrue(line.contains(" (grant off)"))
        assertFalse(line.contains("next in"))
    }

    @Test
    fun r7_stoppedReconcileFailingNamesReason() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), stopped(2, tStop, 3, "reconcile-failing")),
            nowMs,
        )
        assertTrue(museWorkerFooterLine(state, nowMs).contains(" (reconnect failing)"))
    }

    @Test
    fun r8_reconnectedFormExact() {
        val state = workerStateFromRuntimeEvents(
            stream(disconnected(1, tDisc), reconnected(2, tRec, downSeconds = 106)),
            nowMs,
        )
        assertEquals(
            "Muse worker: reconnected ${hhMm(1_769_999_700)} (was down 1m 46s)",
            museWorkerFooterLine(state, nowMs),
        )
    }

    @Test
    fun r8_reconnectedWithoutDownSeconds() {
        val state = workerStateFromRuntimeEvents(stream(reconnected(1, tRec)), nowMs)
        assertEquals("Muse worker: reconnected ${hhMm(1_769_999_700)}", museWorkerFooterLine(state, nowMs))
        assertFalse(museWorkerFooterLine(state, nowMs).contains("was down"))
    }

    // ------------------------------------------- Relay line: untouched ----
    @Test
    fun r9_relayLineStillRelayBased() {
        // The FIRST line is the relay/phone link and must keep reading
        // the health snapshot's worker row — this worker does not rewrite
        // it (spec §7.4 R12). OK row → connected; DOWN + last_ok →
        // disconnected since that time; no snapshot → unknown.
        val ok = HealthSnapshot(
            1_769_999_000L, "",
            listOf(HealthComponentRow("worker", "Worker", HealthStatus.OK, 1_769_999_000L, "", "")),
        )
        assertEquals("Worker connected", workerFooterLine(ok))
        val down = HealthSnapshot(
            1_769_999_000L, "",
            listOf(HealthComponentRow("worker", "Worker", HealthStatus.DOWN, 1_769_999_000L, "", "")),
        )
        assertEquals("Worker disconnected since ${hhMm(1_769_999_000)}", workerFooterLine(down))
        assertEquals("Worker status unknown", workerFooterLine(null))
        val downNoTime = HealthSnapshot(
            1_769_999_000L, "",
            listOf(HealthComponentRow("worker", "Worker", HealthStatus.DOWN, 0L, "", "")),
        )
        assertEquals("Worker disconnected", workerFooterLine(downNoTime))
    }

    @Test
    fun r10_twoLinesNeverBlend() {
        // The relay claim and the Muse-worker claim name different
        // subjects and must never collapse into one line.
        val relay = workerFooterLine(
            HealthSnapshot(
                1_769_999_000L, "",
                listOf(HealthComponentRow("worker", "Worker", HealthStatus.OK, 1_769_999_000L, "", "")),
            ),
        )
        val muse = museWorkerFooterLine(
            workerStateFromRuntimeEvents(stream(disconnected(1, tDisc)), nowMs),
            nowMs,
        )
        assertEquals("Worker connected", relay)
        assertTrue(muse.startsWith("Muse worker disconnected since"))
        // Relay up + Muse worker down at once: the two lines disagree
        // honestly, and neither overwrites the other.
        assertTrue(relay != muse)
        assertFalse(muse.contains("Worker connected"))
        assertFalse(relay.contains("Muse worker"))
    }
}
