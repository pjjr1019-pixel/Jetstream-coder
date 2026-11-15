package com.deskhand.android

import android.os.Looper
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows
import org.robolectric.annotation.Config

/**
 * 100.016 (E14 diagnostics, spec §6, §11): the `health` snapshot parse,
 * the offline banner, the Share button state, the phone-local rows, and
 * the Model poll/export path driven end-to-end with a fake PcLink
 * (same harness as TransportFallbackTest).
 *
 * What this file does NOT prove (needs the PC, spec §11.3): a real
 * service answering `health` / `diagnostics_export`, the real share
 * sheet opening, or the live 2.5 s poll against a real PC — the fake
 * link stands in for the socket, so a real-service failure mode is a
 * live-test item, not a unit-test claim. The VM cannot run these tests
 * at all (no Android SDK) — see PATCH-REPORT-C.md for the PC commands.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DiagnosticsScreenTest {
    private val secret = ByteArray(16) { 1 }
    // 100.040: the old tunnel fixture is gone — Model.connect filters
    // tunnel URLs, so a tunnel-only relays list never dials and the fake
    // link would never be created. Dial a Tailscale address instead.
    private val relay = "wss://100.64.0.1:47801"

    private fun idleMain() {
        Shadows.shadowOf(Looper.getMainLooper()).idle()
    }

    @After fun resetModelDiagnosticsState() {
        // Model is a process-wide singleton: never leak a snapshot, a
        // bundle, a fake factory, or a Diagnostics screen into the next
        // test class.
        Model.disconnect()
        Model.linkFactory = { relay, linkSecret, events -> PcLink(relay, linkSecret, events) }
        Model.viewerPc = null
        Model.screen = Screen.Home
        Model.healthSnapshot = null
        Model.diagnosticsExportText = null
        Model.diagnosticsExportPending = false
        Model.diagnosticsExportError = null
        Model.diagnosticsExportNonce = 0
        Model.sessionConnectedAtMs = null
        Model.chatConnected = false
        Model.viewerConnection = "Disconnected"
        idleMain()
    }

    private fun healthResult(vararg rows: Pair<String, JSONObject>): JSONObject {
        val components = JSONObject()
        rows.forEach { (key, row) -> components.put(key, row) }
        return JSONObject().put("ok", true).put("as_of", 1792345678L)
            .put("as_of_human", "2026-11-09 04:12:00 EST").put("components", components)
    }

    private fun row(status: String, lastOk: Long, version: String = "", error: String = "") =
        JSONObject().put("status", status).put("last_ok", lastOk).put("version", version).put("error", error)

    // ---- §2.1 snapshot parse ----

    @Test fun parseFullSnapshotKeepsSpecOrderAndValues() {
        val result = healthResult(*DIAGNOSTIC_COMPONENT_ORDER.map { (key, _) ->
            key to row("OK", 1792345600L, "v$key", "")
        }.toTypedArray())
        val snapshot = parseHealthSnapshot(result)
        assertNotNull(snapshot)
        assertEquals(1792345678L, snapshot!!.asOfEpochSec)
        assertEquals(12, snapshot.components.size)
        assertEquals(DIAGNOSTIC_COMPONENT_ORDER.map { it.first }, snapshot.components.map { it.key })
        assertEquals(DIAGNOSTIC_COMPONENT_ORDER.map { it.second }, snapshot.components.map { it.label })
        assertTrue(snapshot.components.all { it.status == HealthStatus.OK })
        assertTrue(snapshot.components.all { it.lastOkEpochSec == 1792345600L })
    }

    @Test fun parseMixedStatesSurvive() {
        val result = healthResult(
            "gateway" to row("DOWN", 1792300000L, "", "404 on /status and /capabilities"),
            "lifecycle" to row("IDLE", 1792340000L, "c00e69ad", "state -> stopped (grant off)"),
            "grant" to row("DOWN", 0L, "Generation 471", "FullPcAccess.Enabled=false — Muse sign-in: Needs sign-in"),
        )
        val snapshot = parseHealthSnapshot(result)!!
        val byKey = snapshot.components.associateBy { it.key }
        assertEquals(HealthStatus.DOWN, byKey["gateway"]!!.status)
        assertEquals("404 on /status and /capabilities", byKey["gateway"]!!.error)
        assertEquals(HealthStatus.IDLE, byKey["lifecycle"]!!.status)
        assertEquals("c00e69ad", byKey["lifecycle"]!!.version)
        // last_ok 0 stays 0 — the screen renders "never", never "0s ago".
        assertEquals(0L, byKey["grant"]!!.lastOkEpochSec)
    }

    @Test fun parseMissingKeysAreUnknownNotOk() {
        val result = healthResult("service" to row("OK", 1792345678L, "7dd89f43", ""))
        val snapshot = parseHealthSnapshot(result)!!
        assertEquals(12, snapshot.components.size)
        val byKey = snapshot.components.associateBy { it.key }
        assertEquals(HealthStatus.OK, byKey["service"]!!.status)
        // Every key the op omits is UNKNOWN with "not checked yet" —
        // never OK by default, never a missing row.
        listOf("windows_app", "gateway", "worker", "relay", "node", "agent", "bridge", "grant", "muse", "lifecycle", "phone").forEach { key ->
            assertEquals(key, HealthStatus.UNKNOWN, byKey[key]!!.status)
            assertEquals(key, 0L, byKey[key]!!.lastOkEpochSec)
            assertEquals(key, "not checked yet", byKey[key]!!.error)
        }
    }

    @Test fun parseUnknownStatusWordIsUnknown() {
        val result = healthResult("gateway" to row("WEIRD", 1792300000L, "", ""))
        val snapshot = parseHealthSnapshot(result)!!
        assertEquals(HealthStatus.UNKNOWN, snapshot.components.first { it.key == "gateway" }.status)
    }

    @Test fun parseNoComponentsReturnsNull() {
        assertNull(parseHealthSnapshot(JSONObject().put("ok", true).put("as_of", 1792345678L)))
    }

    @Test fun parseExtraKeysAreIgnored() {
        val components = JSONObject().put("teleporter", row("OK", 1792345678L, "", ""))
        DIAGNOSTIC_COMPONENT_ORDER.forEach { (key, _) -> components.put(key, row("OK", 1792345678L, "", "")) }
        val snapshot = parseHealthSnapshot(JSONObject().put("components", components))!!
        assertEquals(12, snapshot.components.size)
        assertFalse(snapshot.components.any { it.key == "teleporter" })
    }

    // ---- ages and clock ----

    @Test fun formatAgeSecondsBoundaries() {
        assertEquals("0s", formatAgeSeconds(0))
        assertEquals("5s", formatAgeSeconds(5))
        assertEquals("59s", formatAgeSeconds(59))
        assertEquals("1m", formatAgeSeconds(60))
        assertEquals("2m", formatAgeSeconds(125))
        assertEquals("59m", formatAgeSeconds(3599))
        assertEquals("1h 0m", formatAgeSeconds(3600))
        assertEquals("1h 2m", formatAgeSeconds(3720))
        assertEquals("0s", formatAgeSeconds(-10))
    }

    @Test fun formatClockIsHhMmSs() {
        assertTrue(formatClockHhMmSs(1792345678L).matches(Regex("""\d{2}:\d{2}:\d{2}""")))
    }

    // ---- §6.2 offline banner ----

    @Test fun offlineBannerIsNullWhileSessionLive() {
        val snapshot = parseHealthSnapshot(healthResult("service" to row("OK", 1792345678L)))!!
        assertNull(offlineBannerText(true, snapshot))
        assertNull(offlineBannerText(true, null))
    }

    @Test fun offlineBannerShowsLastSnapshotVerbatim() {
        val snapshot = parseHealthSnapshot(healthResult("service" to row("OK", 1792345678L)))!!
        val banner = offlineBannerText(false, snapshot)!!
        assertTrue(banner.startsWith("last snapshot "))
        assertTrue(banner.endsWith(" — session down, values may be stale"))
        // The HH:MM:SS in the middle is the snapshot's as_of, not now.
        assertTrue(banner.contains(formatClockHhMmSs(1792345678L)))
    }

    @Test fun offlineBannerWithoutSnapshot() {
        assertEquals("no snapshot yet — session down", offlineBannerText(false, null))
        val noAsOf = HealthSnapshot(0, "", emptyList())
        assertEquals("no snapshot yet — session down", offlineBannerText(false, noAsOf))
    }

    // ---- §6.3 share button ----

    @Test fun shareButtonDisabledWhenOffline() {
        assertFalse(shareButtonEnabled(false, false))
        assertEquals("session down — export from the PC", shareButtonLabel(false, false))
        assertEquals(SHARE_SESSION_DOWN_LABEL, shareButtonLabel(false, false))
    }

    @Test fun shareButtonStatesWhileLive() {
        assertTrue(shareButtonEnabled(true, false))
        assertEquals("Share diagnostics", shareButtonLabel(true, false))
        assertFalse(shareButtonEnabled(true, true))
        assertEquals("Requesting diagnostics…", shareButtonLabel(true, true))
    }

    // ---- §6.2 phone-local rows ----

    @Test fun phoneConnectionTextNeverGuesses() {
        assertEquals("Tailscale", phoneConnectionText(true, TransportKind.TAILSCALE))
        // 100.040: the tunnel kind is removed — a tunnel URL falls
        // through to Relay, and Relay is the third label now.
        assertEquals("Relay", phoneConnectionText(true, TransportKind.RELAY))
        assertEquals("Local relay", phoneConnectionText(true, TransportKind.LOCAL))
        assertEquals("UNKNOWN", phoneConnectionText(true, null))
        // Last-known is not in-use: a dead session never claims a transport.
        assertEquals("UNKNOWN", phoneConnectionText(false, TransportKind.TAILSCALE))
    }

    @Test fun sessionDurationFormatting() {
        assertEquals("not connected", formatSessionDuration(null, 1_000L))
        assertEquals("0s", formatSessionDuration(1_000L, 1_000L))
        assertEquals("1m 30s", formatSessionDuration(1_000L, 91_000L))
        assertEquals("1h 1m", formatSessionDuration(0L, 3_700_000L))
    }

    @Test fun localRowTextHelpers() {
        assertEquals("foreground service: running", foregroundServiceText(true))
        assertEquals("foreground service: not running", foregroundServiceText(false))
        assertEquals("notifications: granted", notificationPermissionText(true, false))
        assertEquals("notifications: not granted", notificationPermissionText(false, false))
        assertEquals("notifications: allowed (Android 12 and earlier)", notificationPermissionText(false, true))
    }

    @Test fun approvalsEmptyStateLiteral() {
        // §6.5: the reserved section shows exactly this until E08 lands.
        assertEquals("no approvals recorded", APPROVALS_EMPTY_TEXT)
    }

    // ---- Model poll / export path, fake link ----

    // 100.016 §8 deploy-time fix #3 (carried forward in 100.017): PcLink's
    // `events` is private, so the test stores `val events` as its own
    // property (same shape as TransportFallbackTest's FakeLink).
    private class CapturingLink(relay: String, secret: ByteArray, val events: PcLink.Events) :
        PcLink(relay, secret, events) {
        var closed = false
        var started = false
        val chatRequests = mutableListOf<JSONObject>()
        override fun start() { started = true }
        override fun close() { closed = true; super.close() }
        override fun send(type: Int, payload: ByteArray): Boolean {
            if (type == Msg.CHAT_REQUEST) {
                runCatching { JSONObject(String(payload, Charsets.UTF_8)) }.getOrNull()?.let { chatRequests.add(it) }
            }
            return true
        }
    }

    private fun connectCapturing(pc: PairedPc): CapturingLink {
        var link: CapturingLink? = null
        Model.linkFactory = { relay, linkSecret, events -> CapturingLink(relay, linkSecret, events).also { link = it } }
        Model.connect(pc)
        idleMain()
        val created = link!!
        created.events.onConnection(true)
        idleMain()
        created.events.onSession("1234")
        idleMain()
        assertEquals("Connected", Model.viewerConnection)
        // INFO is not driven here (AppUpdates paths) — the chat flag is
        // what pollHealth / requestDiagnosticsExport actually read.
        Model.chatConnected = true
        return created
    }

    private fun healthReply(rpc: String, result: JSONObject): JSONObject =
        JSONObject().put("op", "health").put("ok", true).put("rpc", rpc).put("result", result)

    @Test fun modelHealthReplyStoresSnapshot() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.pollHealth()
        idleMain()
        val sent = link.chatRequests.firstOrNull { it.optString("op") == "health" }
        assertNotNull(sent)
        val result = healthResult(
            "gateway" to row("DOWN", 1792300000L, "", "404 on /status and /capabilities"),
            "service" to row("OK", 1792345678L, "7dd89f43", ""),
        )
        Model.handleChatResponse(healthReply(sent!!.optString("rpc"), result))
        val snapshot = Model.healthSnapshot
        assertNotNull(snapshot)
        assertEquals(12, snapshot!!.components.size)
        val byKey = snapshot.components.associateBy { it.key }
        assertEquals(HealthStatus.DOWN, byKey["gateway"]!!.status)
        assertEquals("404 on /status and /capabilities", byKey["gateway"]!!.error)
        assertEquals(HealthStatus.UNKNOWN, byKey["worker"]!!.status)
        assertEquals("not checked yet", byKey["worker"]!!.error)
    }

    @Test fun modelPollHealthSendsOneRequestWhileOneIsInFlight() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.pollHealth()
        Model.pollHealth()
        idleMain()
        assertEquals(1, link.chatRequests.count { it.optString("op") == "health" })
    }

    @Test fun modelExportRequestSharesReturnedText() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.requestDiagnosticsExport()
        idleMain()
        assertTrue(Model.diagnosticsExportPending)
        val sent = link.chatRequests.firstOrNull { it.optString("op") == "diagnostics_export" }
        assertNotNull(sent)
        val reply = JSONObject().put("op", "diagnostics_export").put("ok", true)
            .put("rpc", sent!!.optString("rpc"))
            .put("result", JSONObject().put("text", "Deskhand diagnostics — test bundle"))
        val nonceBefore = Model.diagnosticsExportNonce
        Model.handleChatResponse(reply)
        assertFalse(Model.diagnosticsExportPending)
        assertEquals("Deskhand diagnostics — test bundle", Model.diagnosticsExportText)
        assertEquals(nonceBefore + 1, Model.diagnosticsExportNonce)
        assertNull(Model.diagnosticsExportError)
    }

    @Test fun modelExportRequestWhenSessionDownSetsError() {
        Model.chatConnected = false
        Model.requestDiagnosticsExport()
        assertFalse(Model.diagnosticsExportPending)
        assertEquals("Session down — export from the PC", Model.diagnosticsExportError)
        assertNull(Model.diagnosticsExportText)
    }

    @Test fun modelHealthReplyDoesNotCarryBundle() {
        // Worker A decision (PATCH-REPORT-A §0): the `health` response
        // does NOT carry the export bundle — a separate
        // `diagnostics_export` op serves it. A health reply must never
        // set the bundle text, even if it carried a stray field.
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.pollHealth()
        idleMain()
        val sent = link.chatRequests.firstOrNull { it.optString("op") == "health" }
        assertNotNull(sent)
        val result = healthResult("service" to row("OK", 1792345678L, "7dd89f43", ""))
            .put("diagnostics_export", "stray — must be ignored")
        Model.handleChatResponse(healthReply(sent!!.optString("rpc"), result))
        assertNotNull(Model.healthSnapshot)
        assertNull(Model.diagnosticsExportText)
    }

    @Test fun modelExportRequestCarriesAndroidVersion() {
        // Worker A (PATCH-REPORT-A §0): the optional `android_version`
        // request field fills the export header. The phone sends its own
        // version only — never a `config` (the PC's config is the
        // service's to report, not the phone's to guess).
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        val link = connectCapturing(pc)
        Model.requestDiagnosticsExport("100.016 (code 42)")
        idleMain()
        val sent = link.chatRequests.firstOrNull { it.optString("op") == "diagnostics_export" }
        assertNotNull(sent)
        assertEquals("100.016 (code 42)", sent!!.optString("android_version"))
        assertFalse(sent.has("config"))
    }

    @Test fun modelSessionClockStartsOnConnectAndStopsOnDisconnect() {
        val pc = PairedPc("My PC", secret, true, listOf(relay))
        connectCapturing(pc)
        assertNotNull(Model.sessionConnectedAtMs)
        assertTrue(Model.sessionConnectedAtMs!! <= System.currentTimeMillis())
        Model.disconnect()
        assertNull(Model.sessionConnectedAtMs)
    }
}
