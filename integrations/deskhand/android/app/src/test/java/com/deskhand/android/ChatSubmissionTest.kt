package com.deskhand.android

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ChatSubmissionTest {
    @Test fun computerTaskNeedsExplicitLocalSelectionAndHostSupport() {
        assertFalse(computerTaskRequested("local", true, false))
        assertFalse(computerTaskRequested("local", false, true))
        assertFalse(computerTaskRequested("unknown", true, true))
        assertTrue(computerTaskRequested("local", true, true))
    }
    @Before fun prepare() {
        // B2-G: unlockUncertainChat() / resetChatTracking() are removed
        // (test-only, no production caller). Isolation now uses the paths
        // the app itself has: a resume_chat success clears any leftover
        // stop banner, and submit acks clear a leftover pending submit
        // (the rpcs the tests below use). Residual: a test that fails
        // mid-flow can still leak route-suggest state into the next test;
        // every test here completes its flow.
        // 100.040 fix round 2: feed a restart_chat success first. It is
        // the only app path that clears the private chat-tracking state
        // (submittedTurnId, pendingChatCommand/Rpc/Text, chatStopPending,
        // chatDeliveryUncertain, epochs) that the acks below cannot
        // reach: the acks clear a pending submit but never touch
        // submittedTurnId, so a leftover submitted turn leaves chatActive
        // true and the next submit returns early (the next test then
        // fails at assertTrue(chatSending)). That fragility is the
        // leading explanation for blocksDuplicate failing after fix
        // round 1 (it passed before, with no production change and this
        // file untouched); the isolation re-run at Gate 4 is the
        // decisive check. The restart gives every test a clean slate;
        // the rest of prepare() then sets the fixture routes/draft/
        // connection as before.
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "restart_chat").put("result", JSONObject()))
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "resume_chat").put("result", JSONObject()))
        Model.chatTurns = emptyList()
        Model.desktopChat.reset()
        // Complete any previous fixture submission without touching a real PC or assistant.
        Model.handleChatResponse(ack("rpc"))
        Model.handleChatResponse(ack("submit-rpc"))
        Model.handleChatResponse(ack("rpc2"))
        Model.chatRoutes = listOf(JSONObject().put("assistant", "codex").put("model", "fixture").put("account", "test"))
        Model.chatRouteIndex = 0
        // Batch 2: the legacy submit-path tests pin direct submit, so
        // they run in Fast mode; the Auto flow has its own test below.
        Model.chatRouteMode = "Fast"
        Model.chatRouteReason = ""
        Model.chatDraft = "Hello"
        Model.chatConnected = true
    }

    private fun ack(rpc: String) = JSONObject().put("ok", true).put("op", "submit")
        .put("rpc", rpc).put("result", JSONObject())

    @Test fun autoRouteSuggestsThenSubmitsWithPickedRoute() {
        Model.chatRouteMode = "Auto"
        var submitted: JSONObject? = null
        var sentOp = ""
        Model.submitChat { raw ->
            val cmd = JSONObject(raw.toString())
            sentOp = cmd.optString("op")
            if (sentOp == "route_suggest") "suggest-rpc" else { submitted = cmd; "submit-rpc" }
        }
        assertEquals("route_suggest", sentOp)
        assertFalse(Model.chatSending)
        assertNull(submitted)
        val picked = JSONObject().put("assistant", "codex").put("model", "fixture").put("account", "test").put("effort", "high")
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "route_suggest").put("rpc", "suggest-rpc")
            .put("result", JSONObject().put("kind", "smart").put("reason", "complex task").put("route", picked)))
        assertNotNull(submitted)
        assertEquals("codex", submitted!!.optString("assistant"))
        assertEquals("high", submitted!!.optString("effort"))
        assertTrue(Model.chatRouteReason.contains("complex task"))
        assertTrue(Model.chatSending)
    }

    @Test fun autoModeKeepsExplicitMuseSelection() {
        Model.chatRouteMode = "Auto"
        Model.chatRoutes = listOf(JSONObject().put("assistant", "muse").put("model", "muse").put("account", "deskhand"))
        Model.chatRouteIndex = 0
        var sent: JSONObject? = null
        Model.submitChat { sent = JSONObject(it.toString()); "submit-rpc" }
        assertEquals("submit", sent!!.getString("op"))
        assertEquals("muse", sent!!.getString("assistant"))
        assertEquals("deskhand", sent!!.getString("account"))
    }

    @Test fun blocksDuplicateUntilMatchingAcknowledgement() {
        var sends = 0
        Model.submitChat { sends++; "rpc" }
        assertTrue(Model.chatSending)
        assertEquals(1, Model.desktopChat.bubbles.size)
        assertFalse(Model.canSendChat)
        Model.submitChat { sends++; "duplicate" }
        assertEquals(1, sends)
        Model.handleChatResponse(ack("unrelated"))
        assertTrue(Model.chatSending)
        assertEquals("Hello", Model.chatDraft)
        Model.handleChatResponse(ack("rpc"))
        assertFalse(Model.chatSending)
        assertEquals("", Model.chatDraft)
        assertEquals("Queued", Model.desktopChat.bubbles.single().message.status)
    }

    @Test fun acknowledgementPreservesNewDraftAndErrorPreservesOriginal() {
        var submitted: JSONObject? = null
        Model.submitChat { submitted = JSONObject(it.toString()); "rpc" }
        Model.chatDraft = "Next message"
        Model.handleChatResponse(ack("rpc"))
        assertEquals("Next message", Model.chatDraft)
        val finished = JSONObject(submitted.toString()).put("status", "completed")
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "history").put("result", JSONObject().put("turns", org.json.JSONArray().put(finished))))
        Model.submitChat { "rpc" }
        Model.handleChatResponse(JSONObject().put("ok", false).put("rpc", "rpc").put("error", "Unavailable"))
        assertFalse(Model.chatSending)
        assertTrue(Model.canSendChat)
        assertEquals("Next message", Model.chatDraft)
    }

    // 100.020 (C07 fail-fast): a gate refusal is a {ok:false,
    // op:"submit", error:<§5 refusal text>} response. The refusal
    // wording below is the simulated SERVER response (test fixture) —
    // production shows whatever the server sends, verbatim; no §5
    // wording lives in main code. The draft is kept, the wording is
    // shown in the status line and as the assistant bubble, and the
    // turn can be re-sent.
    @Test fun gateRefusalKeepsDraftAndShowsServerWording() {
        val refusal = "Couldn't send — Full PC access is off on the PC (Generation 903). " +
            "Your message is saved as a draft. Turn it back on in the Deskhand app on the PC — I can't do that part from here."
        var submitted: JSONObject? = null
        Model.submitChat { submitted = JSONObject(it.toString()); "rpc" }
        assertTrue(Model.chatSending)
        assertEquals("Hello", submitted!!.optString("text"))
        Model.handleChatResponse(JSONObject().put("ok", false).put("op", "submit").put("rpc", "rpc").put("error", refusal))
        assertFalse(Model.chatSending)
        assertTrue(Model.canSendChat)
        assertEquals("Hello", Model.chatDraft)
        assertEquals(refusal, Model.chatStatus)
        val user = Model.desktopChat.messages.first { it.role == "user" }
        assertEquals("Failed", user.status)
        val reply = Model.desktopChat.messages.first { it.role == "assistant" }
        assertEquals(refusal, reply.text)
        assertEquals("Failed", reply.status)
    }

    // 100.020 (C07): the wording shown is the server's, not an Android
    // string — a second, different §5 row must also come through
    // verbatim. A hardcoded wording could not match both.
    @Test fun gateRefusalShowsWhateverWordingTheServerSends() {
        val refusal = "Couldn't send — the saved sign-in for the PC link was rejected (401). " +
            "Your message is saved as a draft. The credential needs to be re-entered; retrying with the same one won't help."
        Model.submitChat { "rpc" }
        Model.handleChatResponse(JSONObject().put("ok", false).put("op", "submit").put("rpc", "rpc").put("error", refusal))
        assertEquals("Hello", Model.chatDraft)
        assertEquals(refusal, Model.chatStatus)
        assertEquals(refusal, Model.desktopChat.messages.first { it.role == "assistant" }.text)
    }

    // 100.020 (C07): the kept draft stays re-sendable, and the normal
    // acknowledgement still clears it — a refusal must not wedge the
    // submit cycle, and an ack after a refusal still means "accepted".
    @Test fun acknowledgementStillClearsDraftAfterARefusal() {
        Model.submitChat { "rpc" }
        Model.handleChatResponse(JSONObject().put("ok", false).put("op", "submit").put("rpc", "rpc")
            .put("error", "Couldn't send — the PC session dropped at 02:39:00. Your message is saved as a draft."))
        assertEquals("Hello", Model.chatDraft)
        var resent: JSONObject? = null
        Model.submitChat { resent = JSONObject(it.toString()); "rpc2" }
        assertEquals("Hello", resent!!.optString("text"))
        assertTrue(Model.chatSending)
        Model.handleChatResponse(ack("rpc2"))
        assertFalse(Model.chatSending)
        assertEquals("", Model.chatDraft)
    }

    @Test fun reconnectReconcilesWithoutResending() {
        // B2-G: the explicit manual unlock is gone with
        // unlockUncertainChat() (no production caller). Recovery is the
        // automatic history-reconciliation path only: a history that
        // observes the pending turn as queued clears the uncertain flag
        // and the draft.
        var command: JSONObject? = null
        Model.submitChat { command = JSONObject(it.toString()); "rpc" }
        Model.markChatDeliveryUncertain()
        assertTrue(Model.chatDeliveryUncertain)
        assertEquals("Hello", Model.chatDraft)
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "history").put("result", JSONObject().put("turns", org.json.JSONArray())))
        assertTrue(Model.chatSending)
        assertTrue(Model.chatDeliveryUncertain)
        val row = JSONObject(command.toString()).put("status", "queued")
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "history").put("result", JSONObject().put("turns", org.json.JSONArray().put(row))))
        assertFalse(Model.chatSending)
        assertFalse(Model.chatDeliveryUncertain)
        assertEquals("", Model.chatDraft)
    }

    @Test fun disconnectedAndRejectedTransportKeepDraft() {
        Model.chatConnected = false
        Model.submitChat { fail("Must not send while disconnected"); "rpc" }
        assertFalse(Model.chatSending)
        Model.chatConnected = true
        Model.submitChat { null }
        assertFalse(Model.chatSending)
        assertEquals("Hello", Model.chatDraft)
        assertTrue(Model.chatStatus.contains("wasn't sent"))
        assertTrue(Model.desktopChat.bubbles.isEmpty())
    }

    @Test fun stopRemainsUnconfirmedUntilNewerLocalResumeState() {
        Model.requestChatStop { "stop-rpc" }
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "stop_chat")
            .put("rpc", "stop-rpc")
            .put("result", JSONObject().put("status", "Stop unconfirmed").put("admission_epoch", 5)))
        assertFalse(Model.chatActive)
        assertFalse(Model.canSendChat)
        fun history(epoch: Int) = JSONObject().put("ok", true).put("op", "history")
            .put("result", JSONObject().put("turns", org.json.JSONArray())
                .put("chatAdmission", JSONObject().put("stopped", false).put("epoch", epoch)))
        Model.handleChatResponse(history(4))
        assertEquals("Stop unconfirmed", Model.chatStopStatus)
        Model.handleChatResponse(history(6))
        assertNull(Model.chatStopStatus)
        assertFalse(Model.chatActive)
    }

    // 100.012 H9: a successful resume clears the banner directly — the
    // follow-up history is no longer the only clearing path.
    @Test fun resumeChatSuccessClearsStopBannerDirectly() {
        Model.requestChatStop { "stop-rpc" }
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "stop_chat")
            .put("rpc", "stop-rpc")
            .put("result", JSONObject().put("status", "Stop unconfirmed").put("admission_epoch", 5)))
        assertEquals("Stop unconfirmed", Model.chatStopStatus)
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "resume_chat").put("result", JSONObject()))
        assertNull(Model.chatStopStatus)
        // A stale history that could not have cleared the banner on epoch
        // grounds (epoch 4 < stop epoch 5) must not bring it back.
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "history")
            .put("result", JSONObject().put("turns", org.json.JSONArray())
                .put("chatAdmission", JSONObject().put("stopped", false).put("epoch", 4))))
        assertNull(Model.chatStopStatus)
    }

    // 100.012 H9: a stop_chat result without admission_epoch must keep the
    // previous epoch (not wipe it to null), and a stopped=false history at
    // the same epoch must then clear the banner (epoch >= stop epoch).
    @Test fun stopChatWithoutAdmissionEpochKeepsEpochAndEqualEpochHistoryClears() {
        Model.requestChatStop { "stop-rpc" }
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "stop_chat")
            .put("rpc", "stop-rpc")
            .put("result", JSONObject().put("status", "Stop unconfirmed").put("admission_epoch", 5)))
        assertEquals("Stop unconfirmed", Model.chatStopStatus)
        // The follow-up history request has no live link in this test, so
        // the connection flag is off; restore it for the second stop.
        Model.chatConnected = true
        Model.requestChatStop { "stop-rpc-2" }
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "stop_chat")
            .put("rpc", "stop-rpc-2")
            .put("result", JSONObject().put("status", "Stop unconfirmed")))
        assertEquals("Stop unconfirmed", Model.chatStopStatus)
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "history")
            .put("result", JSONObject().put("turns", org.json.JSONArray())
                .put("chatAdmission", JSONObject().put("stopped", false).put("epoch", 5))))
        assertNull(Model.chatStopStatus)
    }

    // 100.012 H11: the manual relay update rejects non-ws(s) addresses
    // before touching stored pairings.
    @Test fun updateRelayRejectsNonWsAddress() {
        val pc = PairedPc("My PC", "wss://old-relay.example", ByteArray(16) { 1 }, true)
        assertFalse(Model.updateRelay(pc, "https://relay.example"))
        assertTrue(Model.relayEditStatus.contains("ws://"))
        assertFalse(Model.pcs.any { it.relay == "https://relay.example" })
    }

    @Test fun stopTimeoutIsUnconfirmedAndIgnoresLateReceipt() {
        Model.requestChatStop { "stop-rpc" }
        assertTrue(Model.chatStopPending)
        Model.markStopUnconfirmed("different-rpc")
        assertTrue(Model.chatStopPending)
        Model.markStopUnconfirmed("stop-rpc")
        assertFalse(Model.chatStopPending)
        assertEquals("Stop unconfirmed: no response from PC", Model.chatStopStatus)
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "stop_chat")
            .put("rpc", "stop-rpc").put("result", JSONObject().put("status", "incorrect success")))
        assertEquals("Stop unconfirmed: no response from PC", Model.chatStopStatus)
    }

    @Test fun reconnectRestoresHostStoppedState() {
        val response = JSONObject().put("ok", true).put("op", "history")
            .put("result", JSONObject().put("turns", org.json.JSONArray())
                .put("chatAdmission", JSONObject().put("stopped", true).put("epoch", 8)))
        Model.handleChatResponse(response)
        assertFalse(Model.chatActive)
        assertEquals("Stop unconfirmed. Resume chat in assistant settings.", Model.chatStopStatus)
        assertFalse(Model.canSendChat)
    }
}
