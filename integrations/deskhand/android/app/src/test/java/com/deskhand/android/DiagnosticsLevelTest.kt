package com.deskhand.android

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 100.029 (E41 structured logging, spec §6.2–§6.3, §9.1): the Android
 * logcat capture + scrub + section, and the runtime-stream footer
 * (health + idle age + configured level per source).
 *
 * Pure functions only — no Model singleton, no session, no
 * ServiceController, so these tests add nothing to the
 * ChatSubmissionTest isolation baseline. The VM cannot run them at
 * all (no Android SDK) — the PC runs them at deploy (Gate 4).
 * Live proof (a real phone share, T6/T7) is a live-test item, not a
 * unit-test claim.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DiagnosticsLevelTest {

    private val now = 1792345678L

    private fun row(
        status: HealthStatus,
        lastOk: Long,
        level: String = "",
        error: String = "",
    ) = HealthComponentRow("x", "X", status, lastOk, "", error, level)

    private fun snapshot(vararg rows: Pair<String, HealthComponentRow>): HealthSnapshot {
        val byKey = rows.toMap()
        val components = DIAGNOSTIC_COMPONENT_ORDER.map { (key, label) ->
            // The row() helper builds rows with a placeholder key ("x") —
            // sourceFooterLines looks rows up by their key, so a snapshot
            // row must carry its pair key, or the footer reads UNKNOWN.
            byKey[key]?.copy(key = key, label = label)
                ?: HealthComponentRow(key, label, HealthStatus.UNKNOWN, 0, "", "not checked yet")
        }
        return HealthSnapshot(now, "", components)
    }

    private fun logLine(pid: Int, prio: Char, tag: String, msg: String) =
        "11-10 04:12:34.567  $pid  5678 $prio $tag: $msg"

    // ---- §6.3 footer: health + idle age + level ----

    @Test fun footerOkLineShowsHealthIdleAgeAndLevel() {
        val line = sourceFooterLine(row(HealthStatus.OK, now - 12, "INFO"), "agent", now)
        assertEquals("agent: OK · idle 12s · INFO", line)
    }

    @Test fun footerDownNamesItHonestlyWithNoStaleAge() {
        // A source down for an hour must not show its stale age as fresh.
        val line = sourceFooterLine(row(HealthStatus.DOWN, now - 3600, "INFO"), "bridge", now)
        assertEquals("bridge: DOWN · source down", line)
        assertFalse(line.contains("idle"))
    }

    @Test fun footerNoReadingYetWhenLastOkIsZero() {
        val line = sourceFooterLine(row(HealthStatus.UNKNOWN, 0, "INFO"), "agent", now)
        assertEquals("agent: UNKNOWN · no reading yet · INFO", line)
        assertFalse(line.contains("idle 0s"))
    }

    @Test fun footerMissingLevelIsHonestNotInvented() {
        val line = sourceFooterLine(row(HealthStatus.OK, now - 5, ""), "agent", now)
        assertEquals("agent: OK · idle 5s · level unknown", line)
    }

    @Test fun footerIdleAgeNeverAppearsWithoutTheHealthWord() {
        val snap = snapshot(
            "agent" to row(HealthStatus.OK, now - 12, "INFO"),
            "lifecycle" to row(HealthStatus.IDLE, now - 30, "WARN"),
            "bridge" to row(HealthStatus.DOWN, now - 3600, "ERROR"),
            "tunnel" to row(HealthStatus.DEGRADED, now - 2500, "INFO"),
        )
        for (line in sourceFooterLines(snap, now)) {
            val idleAt = line.indexOf("idle")
            if (idleAt >= 0) {
                // The health word (OK / IDLE / DEGRADED) sits before "idle" on every line that shows one.
                val healthAt = listOf("OK", "IDLE", "DEGRADED", "UNKNOWN")
                    .map { line.indexOf(it) }.filter { it >= 0 }.minOrNull() ?: -1
                assertTrue("idle age without a health word: $line", healthAt >= 0 && healthAt < idleAt)
            }
        }
    }

    @Test fun footerCoversFourSourcesInOrder() {
        // 100.040: the snapshot builder follows DIAGNOSTIC_COMPONENT_ORDER
        // (now 12 keys, tunnel removed), so a tunnel row can never reach
        // the footer — the tunnel line has no row and reads UNKNOWN. The
        // footer list itself (SOURCE_FOOTER_KEYS) is unchanged: four lines.
        val snap = snapshot(
            "agent" to row(HealthStatus.OK, now - 12, "INFO"),
            "lifecycle" to row(HealthStatus.IDLE, now - 30, "INFO"),
            "bridge" to row(HealthStatus.DOWN, now - 60, "ERROR"),
        )
        val lines = sourceFooterLines(snap, now)
        assertEquals(4, lines.size)
        assertEquals("agent: OK · idle 12s · INFO", lines[0])
        assertEquals("lifecycle: IDLE · idle 30s · INFO", lines[1])
        assertEquals("bridge: DOWN · source down", lines[2])
        assertEquals("tunnel: UNKNOWN · no reading yet · level unknown", lines[3])
        assertEquals(lines.joinToString("\n"), sourceFooterText(snap, now))
    }

    @Test fun footerWithNoSnapshotIsUnknownNotOk() {
        val lines = sourceFooterLines(null, now)
        assertEquals(4, lines.size)
        assertTrue(lines.all { it.contains("UNKNOWN") })
        assertTrue(lines.all { it.contains("no reading yet") })
        assertFalse(lines.any { it.contains("OK") || it.contains("idle") })
    }

    // ---- §6.3: the health() `level` field reaches the row ----

    @Test fun parseHealthSnapshotCarriesLevelPerComponent() {
        val components = JSONObject()
        components.put("agent", JSONObject().put("status", "OK").put("last_ok", now - 12)
            .put("version", "").put("error", "").put("level", "DEBUG"))
        components.put("bridge", JSONObject().put("status", "DOWN").put("last_ok", 0)
            .put("version", "").put("error", "").put("level", "ERROR"))
        val snap = parseHealthSnapshot(JSONObject().put("as_of", now).put("components", components))!!
        val byKey = snap.components.associateBy { it.key }
        assertEquals("DEBUG", byKey["agent"]!!.level)
        assertEquals("ERROR", byKey["bridge"]!!.level)
        // A PC that predates the `level` field sends none — blank, never invented.
        assertEquals("", byKey["service"]!!.level)
    }

    // ---- §6.2 logcat filter: tag + pid + INFO and above, max 200 ----

    @Test fun filterLogcatKeepsOwnTagPidAndInfoAndAbove() {
        val lines = listOf(
            "--------- beginning of main",
            logLine(1234, 'I', "Deskhand", "session connected"),
            logLine(1234, 'W', "DeskhandVideo", "decoder retry 1/3"),
            logLine(1234, 'E', "Deskhand", "relay socket failed"),
            logLine(9999, 'I', "Deskhand", "another app, same tag"),
            logLine(1234, 'I', "SomeOtherApp", "wrong tag"),
            logLine(1234, 'D', "Deskhand", "debug detail — never in a share"),
            logLine(1234, 'V', "Deskhand", "verbose — never in a share"),
            "short line",
        )
        val kept = filterLogcatLines(lines, pid = 1234)
        assertEquals(
            listOf(lines[1], lines[2], lines[3]),
            kept,
        )
    }

    @Test fun filterLogcatCapsAt200NewestLast() {
        val lines = (1..250).map { logLine(1234, 'I', "Deskhand", "msg $it") }
        val kept = filterLogcatLines(lines, pid = 1234)
        assertEquals(200, kept.size)
        assertTrue(kept.first().endsWith("msg 51"))
        assertTrue(kept.last().endsWith("msg 250"))
    }

    // ---- §6.2 phone-side scrub: same rules as the PC export ----

    @Test fun scrubLogcatRedactsTokenShapes() {
        val hex32 = "a".repeat(32)
        assertEquals("[REDACTED-TOKEN]", scrubLogcatLine("saw $hex32 here")!!.substringAfter("saw ").substringBefore(" here"))
        val b64 = "Ab3_dEf9Gh2JkLmNoPqRsTuVwXyZ01234"
        assertTrue(b64.length >= 32)
        assertTrue(scrubLogcatLine("key $b64")!!.contains("[REDACTED-TOKEN]"))
        assertFalse(scrubLogcatLine("key $b64")!!.contains(b64))
        // Short values are not tokens: a 16-char hex run survives.
        val hex16 = "deadbeefdeadbeef"
        assertEquals("id $hex16", scrubLogcatLine("id $hex16"))
    }

    @Test fun scrubLogcatRedactsCredentialFieldsAndTokenUrls() {
        assertEquals("token=[REDACTED]", scrubLogcatLine("token=abc123"))
        assertEquals("password:[REDACTED]", scrubLogcatLine("password: hunter2"))
        assertEquals(
            "relay wss://x.trycloudflare.com/?[REDACTED]",
            scrubLogcatLine("relay wss://x.trycloudflare.com/?token=abc&x=1"),
        )
        // A URL whose query carries no sensitive key is left alone.
        assertEquals(
            "https://example.com/?page=2",
            scrubLogcatLine("https://example.com/?page=2"),
        )
    }

    @Test fun scrubLogcatCutsConfigContents() {
        val line = "reading agent_config.json {\"token\": \"abc\"}"
        assertEquals("reading agent_config.json", scrubLogcatLine(line))
    }

    @Test fun scrubLogcatDropsControlCharacterLines() {
        assertNull(scrubLogcatLine("bad\u0001line"))
    }

    // ---- §6.2 the [logcat] section ----

    @Test fun buildLogcatSectionEmptyReportsNoLinesCaptured() {
        assertEquals("[logcat: no lines captured]", buildLogcatSection(emptyList(), 1234))
        assertEquals("[logcat: no lines captured]", buildLogcatSection(listOf(logLine(9999, 'I', "Deskhand", "not ours")), 1234))
    }

    @Test fun buildLogcatSectionNeverIncludesDebugAndRedactsTokens() {
        val hex32 = "f".repeat(32)
        val raw = listOf(
            logLine(1234, 'I', "Deskhand", "session connected"),
            logLine(1234, 'D', "Deskhand", "debug detail $hex32"),
            logLine(1234, 'W', "Deskhand", "saw token $hex32"),
        )
        val section = buildLogcatSection(raw, 1234)
        assertTrue(section.startsWith("[logcat]\n"))
        assertTrue(section.contains("session connected"))
        assertFalse(section.contains("debug detail"))
        assertFalse(section.contains(hex32))
        assertTrue(section.contains("[REDACTED-TOKEN]"))
    }

    @Test fun appendLogcatSectionKeepsBundleAndAddsSection() {
        // Deterministic on any host: whether or not a logcat binary
        // exists where the tests run, the bundle is intact and a
        // [logcat] section follows it — the empty marker when nothing
        // was captured, real lines when it was.
        val bundle = "Deskhand diagnostics — test bundle"
        val shared = appendLogcatSection(bundle)
        assertTrue(shared.startsWith("$bundle\n\n[logcat"))
    }
}
