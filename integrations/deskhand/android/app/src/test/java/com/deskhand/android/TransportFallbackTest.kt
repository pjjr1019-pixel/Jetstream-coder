package com.deskhand.android

import android.os.Looper
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Shadows
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.concurrent.TimeUnit

/**
 * 100.015 (spec §11, §15.1): transport classification, QR candidate
 * list, PairedPc migration, ordered-fallback decisions, the indicator
 * text, and Model.connect driven end-to-end with a fake PcLink.
 *
 * 100.040: the tunnel kind is removed (Cloudflare gone) — tunnel URLs
 * classify as RELAY, isTunnelUrl() is the filter, and Model.connect
 * skips tunnel candidates (tunnel-only pairing shows "pair again").
 *
 * What this file does NOT prove (needs the PC, §15.3): a real PcLink
 * dialling a real relay — the fake stands in for the socket, so a
 * real-network failure mode (TLS, DNS, CGNAT routing) is a live-test
 * item, not a unit-test claim.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class TransportFallbackTest {
    private val secret = ByteArray(16) { 1 }
    private val tailscale = "ws://100.64.0.1:47801"
    private val tunnel = "wss://abc123.trycloudflare.com"
    private val lan = "ws://192.168.1.5:47801"

    private fun idleMain() {
        Shadows.shadowOf(Looper.getMainLooper()).idle()
    }

    @After fun resetModelTransportState() {
        // Model is a process-wide singleton: never leak transport state,
        // a fake factory, or a Viewer screen into the next test class.
        Model.disconnect()
        Model.linkFactory = { relay, linkSecret, events -> PcLink(relay, linkSecret, events) }
        Model.viewerPc = null
        Model.screen = Screen.Home
        Model.transport = null
        Model.tryingTransport = null
        Model.lastTransport = null
        Model.lastTransportPcId = null
        Model.transportAllFailed = false
        Model.transportFailedCount = 0
        idleMain()
    }

    // ---- §11.1 classification ----

    @Test fun classifyTailscaleCgnatRange() {
        assertEquals(TransportKind.TAILSCALE, classifyRelayUrl("ws://100.64.0.1:47801"))
        assertEquals(TransportKind.TAILSCALE, classifyRelayUrl("wss://100.127.255.9:47801"))
        assertEquals(TransportKind.TAILSCALE, classifyRelayUrl("ws://100.64.1.2:47801"))
        assertEquals(TransportKind.RELAY, classifyRelayUrl("ws://100.128.0.1:47801"))
        assertEquals(TransportKind.RELAY, classifyRelayUrl("ws://100.63.0.1:47801"))
    }

    @Test fun classifyTunnelAsPlainRelay() {
        // 100.040: the tunnel kind is gone — a tunnel URL falls through to RELAY.
        assertEquals(TransportKind.RELAY, classifyRelayUrl("wss://abc123.trycloudflare.com"))
        assertEquals(TransportKind.RELAY, classifyRelayUrl("wss://abc123.trycloudflare.com:443"))
        assertEquals(TransportKind.RELAY, classifyRelayUrl("wss://trycloudflare.com.evil.example"))
    }

    @Test fun isTunnelUrlDetectsCloudflareHostsOnly() {
        assertTrue(isTunnelUrl("wss://abc123.trycloudflare.com"))
        assertTrue(isTunnelUrl("wss://abc123.trycloudflare.com:443"))
        assertTrue(isTunnelUrl("wss://trycloudflare.com"))
        assertFalse(isTunnelUrl("wss://trycloudflare.com.evil.example"))
        assertFalse(isTunnelUrl(tailscale))
        assertFalse(isTunnelUrl(lan))
        assertFalse(isTunnelUrl("not a url"))
        assertFalse(isTunnelUrl(""))
    }

    @Test fun classifyLocalRelay() {
        assertEquals(TransportKind.LOCAL, classifyRelayUrl("ws://192.168.1.5:47801"))
        assertEquals(TransportKind.LOCAL, classifyRelayUrl("ws://10.0.0.2:47801"))
        assertEquals(TransportKind.LOCAL, classifyRelayUrl("ws://172.16.0.1:47801"))
        assertEquals(TransportKind.LOCAL, classifyRelayUrl("ws://172.31.255.255:47801"))
        assertEquals(TransportKind.RELAY, classifyRelayUrl("ws://172.32.0.1:47801"))
        assertEquals(TransportKind.LOCAL, classifyRelayUrl("ws://127.0.0.1:47801"))
        assertEquals(TransportKind.LOCAL, classifyRelayUrl("ws://localhost:47801"))
        assertEquals(TransportKind.LOCAL, classifyRelayUrl("ws://[::1]:47801"))
    }

    @Test fun classifyAnythingElseIsPlainRelay() {
        assertEquals(TransportKind.RELAY, classifyRelayUrl("wss://relay.example.com"))
        assertEquals(TransportKind.RELAY, classifyRelayUrl("ws://8.8.8.8:47801"))
        assertEquals(TransportKind.RELAY, classifyRelayUrl("not a url"))
        assertEquals(TransportKind.RELAY, classifyRelayUrl(""))
    }

    @Test fun labelsUseOnlySpecWording() {
        assertEquals("Tailscale", TransportKind.TAILSCALE.label())
        assertEquals("Local relay", TransportKind.LOCAL.label())
        assertEquals("Relay", TransportKind.RELAY.label())
        assertEquals("Trying Tailscale…", TransportKind.TAILSCALE.tryingLabel())
        assertEquals("Trying Local relay…", TransportKind.LOCAL.tryingLabel())
        assertEquals("Connected via Tailscale", TransportKind.TAILSCALE.connectedLabel())
        assertEquals("Connected via Local relay", TransportKind.LOCAL.connectedLabel())
        assertEquals("No transport — all three failed. Retrying…", noTransportLabel(3))
        assertEquals("No transport — all failed. Retrying…", noTransportLabel(2))
    }

    // ---- §11.2 QR candidate list ----

    @Test fun newQrBuildsOrderedCandidateList() {
        assertEquals(listOf(tailscale, tunnel, lan), buildRelayCandidates(tailscale, tunnel, lan))
    }

    @Test fun oldQrWithOnlyRProducesOneItemList() {
        assertEquals(listOf(tunnel), buildRelayCandidates(null, tunnel, null))
        assertEquals(listOf(tunnel), buildRelayCandidates("", tunnel, ""))
    }

    @Test fun candidatesDedupeBlanksAndExactDuplicates() {
        // t == r dedupes to one entry (§15.1).
        assertEquals(listOf(tunnel, lan), buildRelayCandidates(tunnel, tunnel, lan))
        assertEquals(listOf(tailscale), buildRelayCandidates(tailscale, "$tailscale/", null))
        assertEquals(listOf(tunnel), buildRelayCandidates("  ", tunnel, ""))
        // l == r dedupes.
        assertEquals(listOf(tailscale, lan), buildRelayCandidates(tailscale, lan, lan))
    }

    @Test fun onlyWsCandidatesAreDialled() {
        assertEquals(listOf("wss://a.example", "ws://c.example"),
            validRelayCandidates(listOf("wss://a.example", "https://b.example", "ws://c.example", "garbage")))
    }

    @Test fun fullRetryBackoffDoublesToThirtySeconds() {
        assertEquals(1_000L, fullRetryDelayMs(0))
        assertEquals(2_000L, fullRetryDelayMs(1))
        assertEquals(4_000L, fullRetryDelayMs(2))
        assertEquals(30_000L, fullRetryDelayMs(5))
        assertEquals(30_000L, fullRetryDelayMs(9))
    }

    // ---- §11.3 PairedPc migration ----

    @Test fun oldFourArgConstructionBuildsOneItemList() {
        val pc = PairedPc("My PC", tunnel, secret, true)
        assertEquals(listOf(tunnel), pc.relays)
        assertEquals(tunnel, pc.relay)
    }

    @Test fun pairedPcJsonRoundTripKeepsOrderedRelays() {
        val pc = PairedPc("My PC", secret, true, listOf(tailscale, tunnel, lan))
        val parsed = parsePairedPcRecord(pairedPcToJson(pc))
        assertNotNull(parsed)
        assertEquals(listOf(tailscale, tunnel, lan), parsed!!.relays)
        assertEquals("My PC", parsed.name)
        assertEquals(tailscale, parsed.relay)
        assertArrayEquals(secret, parsed.secret)
        assertTrue(parsed.control)
    }

    @Test fun legacySingleRelayMigratesToOneItemList() {
        val legacy = JSONObject().put("name", "Old").put("relay", tunnel)
            .put("secret", Base64.encodeToString(secret, Base64.NO_WRAP)).put("control", false)
        assertEquals(listOf(tunnel), parsePairedPcRecord(legacy)?.relays)
    }

    @Test fun emptyListWithNoLegacyRelayIsCorrupt() {
        val corrupt = JSONObject().put("name", "Bad").put("relays", JSONArray())
            .put("secret", Base64.encodeToString(secret, Base64.NO_WRAP)).put("control", true)
        assertNull(parsePairedPcRecord(corrupt))
        // …but an empty list with a surviving legacy relay keeps the old behaviour.
        val withLegacy = JSONObject().put("name", "Old2").put("relays", JSONArray()).put("relay", tunnel)
            .put("secret", Base64.encodeToString(secret, Base64.NO_WRAP)).put("control", true)
        assertEquals(listOf(tunnel), parsePairedPcRecord(withLegacy)?.relays)
    }

    @Test fun storedRelaysWinOverLegacyRelay() {
        val o = JSONObject().put("name", "PC").put("relay", tunnel)
            .put("relays", JSONArray().put(tailscale).put(lan))
            .put("secret", Base64.encodeToString(secret, Base64.NO_WRAP)).put("control", true)
        assertEquals(listOf(tailscale, lan), parsePairedPcRecord(o)?.relays)
    }

    // ---- §11.4 fallback decisions (pure state machine) ----

    @Test fun fallbackAdvancesInOrderAndExhausts() {
        val fb = OrderedFallback(listOf(tailscale, tunnel, lan))
        assertEquals(tailscale, fb.current)
        assertEquals(TransportKind.TAILSCALE, fb.currentKind)
        assertEquals(tunnel, fb.advanceAfterFailure())
        assertEquals(lan, fb.advanceAfterFailure())
        assertNull(fb.advanceAfterFailure())
        assertFalse(fb.shouldStayAfterFailure())
    }

    @Test fun fallbackDropStaysThenFallsBackOnReconnectFailure() {
        val fb = OrderedFallback(listOf(tailscale, tunnel, lan))
        fb.markSessionLive()
        fb.markSessionLost()
        // First failure after the drop: stay (same-transport retry pending).
        assertNull(fb.advanceAfterFailure())
        assertTrue(fb.shouldStayAfterFailure())
        // The same-transport reconnect also failed: now fall back.
        assertEquals(tunnel, fb.advanceAfterFailure())
        assertFalse(fb.shouldStayAfterFailure())
        // The fresh candidate never had a session: its failure advances at once.
        assertEquals(lan, fb.advanceAfterFailure())
        assertNull(fb.advanceAfterFailure())
    }

    @Test fun fallbackRelayUpResetsDropGrace() {
        val fb = OrderedFallback(listOf(tailscale, tunnel))
        fb.markSessionLive()
        fb.markSessionLost()
        assertNull(fb.advanceAfterFailure()) // drop: stay
        fb.markRelayUp() // same-transport reconnect succeeded
        fb.markSessionLost() // …then the socket fails again
        assertNull(fb.advanceAfterFailure()) // grace applies again: stay
        assertTrue(fb.shouldStayAfterFailure())
    }

    @Test fun fallbackRestartStartsANewRoundFromCandidateOne() {
        val fb = OrderedFallback(listOf(tailscale, tunnel))
        fb.advanceAfterFailure()
        assertEquals(tunnel, fb.current)
        fb.restart()
        assertEquals(tailscale, fb.current)
        assertFalse(fb.sessionLive)
        assertFalse(fb.sessionEverLive)
    }

    // ---- §11.5 indicator text (derived Model state) ----

    @Test fun transportTextShowsTryingConnectedAndNoTransport() {
        Model.viewerConnection = "Connecting"
        Model.tryingTransport = TransportKind.TAILSCALE
        assertEquals("Trying Tailscale…", Model.transportText)
        Model.transport = TransportKind.LOCAL
        Model.viewerConnection = "Connected"
        assertEquals("Connected via Local relay", Model.transportText)
        Model.transport = null
        Model.tryingTransport = null
        Model.transportAllFailed = true
        Model.transportFailedCount = 3
        assertEquals("No transport — all three failed. Retrying…", Model.transportText)
        Model.transportAllFailed = false
        Model.transportFailedCount = 0
        assertEquals("", Model.transportText)
    }

    // ---- §11.4 + §11.5 end to end through Model.connect (fake link) ----

    private class FakeLink(val relay: String, val events: PcLink.Events, secret: ByteArray) : PcLink(relay, secret, events) {
        var closed = false
        var started = false
        override fun start() { started = true }
        override fun close() { closed = true; super.close() }
        override fun send(type: Int, payload: ByteArray): Boolean = false
    }

    private fun connectWithFakeLinks(pc: PairedPc): MutableList<FakeLink> {
        val created = mutableListOf<FakeLink>()
        Model.linkFactory = { relay, linkSecret, events -> FakeLink(relay, events, linkSecret).also { created.add(it) } }
        Model.connect(pc)
        idleMain()
        return created
    }

    @Test fun candidateOneFailsCandidateTwoConnectsAndIndicatorFollows() {
        val pc = PairedPc("My PC", secret, true, listOf(tailscale, tunnel, lan))
        val created = connectWithFakeLinks(pc)
        // 100.040: the tunnel candidate is filtered out at connect, so
        // the list actually tried is [tailscale, lan].
        assertEquals(1, created.size)
        assertEquals(tailscale, created[0].relay)
        assertTrue(created[0].started)
        assertEquals("Trying Tailscale…", Model.viewerStatus)
        assertEquals("Trying Tailscale…", Model.transportText)

        created[0].events.onConnection(false)
        idleMain()
        assertTrue(created[0].closed)
        assertEquals(2, created.size)
        assertEquals(lan, created[1].relay)
        assertEquals("Trying Local relay…", Model.viewerStatus)

        created[1].events.onConnection(true)
        idleMain()
        assertEquals(TransportKind.LOCAL, Model.transport)
        created[1].events.onSession("1234")
        idleMain()
        assertEquals("Connected", Model.viewerConnection)
        assertEquals("Connected via Local relay", Model.transportText)
        assertEquals(TransportKind.LOCAL, Model.lastTransport)
    }

    @Test fun dropStaysOnSameTransportThenFallsBackWhenReconnectFails() {
        val pc = PairedPc("My PC", secret, true, listOf(tailscale, tunnel, lan))
        val created = connectWithFakeLinks(pc)
        created[0].events.onConnection(true)
        idleMain()
        created[0].events.onSession("1234")
        idleMain()
        assertEquals("Connected via Tailscale", Model.transportText)

        created[0].events.onSessionLost()
        idleMain()
        assertNull(Model.transport)
        assertEquals(TransportKind.TAILSCALE, Model.lastTransport)
        created[0].events.onConnection(false)
        idleMain()
        // No mid-session hopping: still one link, same transport retrying.
        assertEquals(1, created.size)
        assertEquals("Lost the PC. Reconnecting…", Model.viewerStatus)

        // The same-transport reconnect also failed: now fall back.
        created[0].events.onConnection(false)
        idleMain()
        assertEquals(2, created.size)
        assertEquals(lan, created[1].relay)
        assertTrue(created[0].closed)
    }

    @Test fun tunnelCandidatesAreSkippedAtConnect() {
        // 100.040: a tunnel URL in the stored list is never dialled —
        // not first, not as a fallback.
        val pc = PairedPc("My PC", secret, true, listOf(tunnel, tailscale, lan))
        val created = connectWithFakeLinks(pc)
        assertEquals(1, created.size)
        assertEquals(tailscale, created[0].relay)
        assertEquals("Trying Tailscale…", Model.viewerStatus)
        created[0].events.onConnection(false)
        idleMain()
        assertEquals(2, created.size)
        assertEquals(lan, created[1].relay)
        assertTrue(created.none { it.relay == tunnel })
    }

    @Test fun tunnelOnlyPairingShowsPairAgainAndDialsNothing() {
        // 100.040: every stored candidate is a tunnel URL — connect
        // must NOT crash (OrderedFallback requires a candidate) and
        // must NOT try the tunnel; it shows the "pair again" state.
        val pc = PairedPc("My PC", secret, true, listOf(tunnel))
        val created = connectWithFakeLinks(pc)
        assertEquals(0, created.size)
        assertEquals("Disconnected", Model.viewerConnection)
        assertTrue(Model.viewerStatus.contains("Pair again"))
        assertNull(Model.transport)
        assertNull(Model.tryingTransport)
        assertFalse(Model.transportAllFailed)
    }

    @Test fun allCandidatesFailShowsNoTransportThenRetriesWholeList() {
        val pc = PairedPc("My PC", secret, true, listOf(tailscale, tunnel, lan))
        val created = connectWithFakeLinks(pc)
        // 100.040: the tunnel is filtered out, so two candidates are tried.
        created[0].events.onConnection(false)
        idleMain()
        created[1].events.onConnection(false)
        idleMain()
        assertEquals(2, created.size)
        assertTrue(Model.transportAllFailed)
        assertEquals("No transport — all failed. Retrying…", Model.viewerStatus)
        assertEquals("No transport — all failed. Retrying…", Model.transportText)

        // The whole list is retried with the existing backoff (≤ 1.5 s
        // for the first round): a fresh round starts at candidate 1.
        Shadows.shadowOf(Looper.getMainLooper()).idleFor(5, TimeUnit.SECONDS)
        assertEquals(3, created.size)
        assertEquals(tailscale, created[2].relay)
        assertEquals("Trying Tailscale…", Model.viewerStatus)
        assertFalse(Model.transportAllFailed)
    }

    @Test fun oldSingleRelayPairingKeepsTodaysWordingOnFailure() {
        // 100.040: the single relay is LAN now — a single tunnel relay
        // is the "pair again" case (see tunnelOnlyPairingShowsPairAgain*).
        val pc = PairedPc("My PC", lan, secret, true)
        val created = connectWithFakeLinks(pc)
        assertEquals(1, created.size)
        created[0].events.onConnection(false)
        idleMain()
        assertFalse(Model.transportAllFailed)
        assertTrue(Model.viewerStatus.startsWith("Can't reach the relay. Retrying…"))
        assertEquals("", Model.transportText)
    }
}
