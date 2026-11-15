package com.deskhand.android

import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows
import org.robolectric.annotation.Config
import java.util.concurrent.TimeUnit

/**
 * 100.040 (§7): the chat-freeze fix — the submitted-turn wait has a
 * deadline. Before this build, a submit locked Send (chatActive) until
 * a history push showed the turn resolved, and there was no timeout:
 * with the relay degraded and no history arriving, the chat stayed on
 * "Sending…" forever. Now a one-off history re-fetch goes out at ~5 s
 * and, at 45 s (the same no-response timeout as the agent-stop path),
 * a still-waiting turn is marked "unknown", the "Delivery unconfirmed.
 * Check before resending." notice surfaces, and Send re-enables.
 *
 * Negative control: on the base code these tests FAIL — after 45 s the
 * gate is still locked (no timeout exists) and no history re-fetch is
 * ever sent. The history-resolution test is the positive control: it
 * passes on the base code too (a turn that resolves in time must never
 * be marked unknown).
 *
 * What this file does NOT prove (needs the PC, Gate 4): the re-fetch
 * reaching a real PC over a real relay, or the 2–30 s tunnel-leg
 * timing that made the freeze visible in the first place.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ChatFreezeTimeoutTest {
    private fun idleMain() {
        Shadows.shadowOf(Looper.getMainLooper()).idle()
    }

    private fun ack(rpc: String) = JSONObject().put("ok", true).put("op", "submit")
        .put("rpc", rpc).put("result", JSONObject())

    @Before fun prepare() {
        // Same isolation as ChatSubmissionTest.prepare: a resume_chat
        // success clears any leftover stop banner, and submit acks clear
        // a leftover pending submit. Every test here completes its flow,
        // and @After drains the main looper so no 5/45 s task leaks.
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "resume_chat").put("result", JSONObject()))
        Model.chatTurns = emptyList()
        Model.desktopChat.reset()
        Model.handleChatResponse(ack("rpc"))
        Model.handleChatResponse(ack("submit-rpc"))
        Model.handleChatResponse(ack("rpc2"))
        Model.chatRoutes = listOf(JSONObject().put("assistant", "codex").put("model", "fixture").put("account", "test"))
        Model.chatRouteIndex = 0
        Model.chatRouteMode = "Fast"
        Model.chatRouteReason = ""
        Model.chatDraft = "Hello"
        Model.chatConnected = true
        idleMain()
    }

    @After fun resetFreezeState() {
        idleMain()
        Model.desktopChat.reset()
        Model.chatTurns = emptyList()
        Model.chatStatus = "Ready"
        Model.chatConnected = true
    }

    // A fake send that records every op. The submit returns "rpc" and
    // the history re-fetch returns "history-rpc"; no response is ever
    // fed back unless the test does so explicitly.
    private class RecordingSend {
        val ops = mutableListOf<String>()
        var submitted: JSONObject? = null
        val send: (JSONObject) -> String? = { raw ->
            val op = raw.optString("op")
            ops.add(op)
            if (op == "submit") submitted = JSONObject(raw.toString())
            if (op == "history") "history-rpc" else "rpc"
        }
    }

    private fun historyWith(turn: JSONObject) = JSONObject().put("ok", true).put("op", "history")
        .put("result", JSONObject().put("turns", JSONArray().put(turn)))

    private fun assertUnconfirmedShown() {
        val notice = Model.desktopChat.messages.firstOrNull {
            it.role == "assistant" && it.text == "Delivery unconfirmed. Check before resending."
        }
        assertNotNull("the unconfirmed notice must surface", notice)
        assertEquals("unknown", notice!!.status)
        assertEquals("Delivery unconfirmed. Check before resending.", Model.chatStatus)
    }

    @Test fun submittedTurnWithNoHistoryUpdateIsUnknownAndGateClearsAfter45s() {
        val rec = RecordingSend()
        Model.submitChat(rec.send)
        assertTrue(Model.chatSending)
        assertFalse(Model.canSendChat)
        assertEquals(listOf("submit"), rec.ops)

        // The ~5 s re-fetch goes out; the gate is still locked — the
        // timeout has not fired yet.
        Shadows.shadowOf(Looper.getMainLooper()).idleFor(5, TimeUnit.SECONDS)
        assertEquals(listOf("submit", "history"), rec.ops)
        assertTrue(Model.chatSending)
        assertFalse(Model.canSendChat)

        // The 45 s timeout fires: the turn is unknown, the gate clears.
        Shadows.shadowOf(Looper.getMainLooper()).idleFor(40, TimeUnit.SECONDS)
        assertFalse(Model.chatSending)
        assertFalse(Model.chatActive)
        assertTrue(Model.canSendChat)
        assertEquals("unknown", Model.desktopChat.messages.first { it.role == "user" }.status)
        assertUnconfirmedShown()
    }

    @Test fun historyRefetchIsOneOffNotAPoll() {
        val rec = RecordingSend()
        Model.submitChat(rec.send)

        // Nothing before 5 s: no early re-fetch, no standing poll tick.
        Shadows.shadowOf(Looper.getMainLooper()).idleFor(4, TimeUnit.SECONDS)
        assertEquals(listOf("submit"), rec.ops)

        // Exactly one re-fetch at 5 s …
        Shadows.shadowOf(Looper.getMainLooper()).idleFor(1, TimeUnit.SECONDS)
        assertEquals(listOf("submit", "history"), rec.ops)

        // … and no second one after the timeout, even a minute later.
        Shadows.shadowOf(Looper.getMainLooper()).idleFor(60, TimeUnit.SECONDS)
        assertEquals(listOf("submit", "history"), rec.ops)
        assertFalse(Model.chatActive)
        assertTrue(Model.canSendChat)
    }

    @Test fun historyResolvingBeforeTimeoutClearsGateWithoutUnknown() {
        val rec = RecordingSend()
        Model.submitChat(rec.send)
        val submitted = rec.submitted!!
        // The submit ack ("Queued") clears the pending command; the
        // history below clears the submitted turn. The gate has two
        // halves (Model.kt L251/L276 chatSending, L965 submittedTurnId)
        // — without the ack it stays locked via chatSending even after
        // a resolving history, so this test as written fails on the
        // base code too, against the file header's positive-control
        // claim. Feed the ack here, as submitAckAloneDoesNotStopTheTimeout does.
        Model.handleChatResponse(ack("rpc"))
        // The ack also clears the draft (the message was accepted); the
        // user types again, as submitAckAloneDoesNotStopTheTimeout does.
        // Without a draft, canSendChat is false no matter what the gate
        // does (Model.kt L277) — round 1 fed the ack but not the draft.
        Model.chatDraft = "Hello"
        assertFalse(Model.canSendChat)

        // The history arrives at ~10 s with the turn completed.
        Shadows.shadowOf(Looper.getMainLooper()).idleFor(10, TimeUnit.SECONDS)
        val finished = JSONObject(submitted.toString()).put("status", "completed").put("reply", "Done")
        Model.handleChatResponse(historyWith(finished))
        assertFalse(Model.chatActive)
        assertTrue(Model.canSendChat)

        // Past both deadlines: no second re-fetch — the one at ~5 s
        // already fired, before the resolving history — and no unknown
        // marking: a resolved turn must never be unconfirmed.
        Shadows.shadowOf(Looper.getMainLooper()).idleFor(40, TimeUnit.SECONDS)
        assertEquals(listOf("submit", "history"), rec.ops)
        assertFalse(Model.desktopChat.messages.any { it.text == "Delivery unconfirmed. Check before resending." })
        assertTrue(Model.desktopChat.messages.any { it.role == "assistant" && it.text == "Done" })
    }

    @Test fun submitAckAloneDoesNotStopTheTimeout() {
        val rec = RecordingSend()
        Model.submitChat(rec.send)
        Model.handleChatResponse(ack("rpc"))
        // The ack clears the pending command ("Queued") but NOT the
        // submitted turn — the gate is still locked (§7.6).
        assertFalse(Model.chatSending)
        assertTrue(Model.chatActive)
        assertFalse(Model.canSendChat)
        // The ack also kept the draft cleared; the user types again.
        Model.chatDraft = "Hello"

        Shadows.shadowOf(Looper.getMainLooper()).idleFor(45, TimeUnit.SECONDS)
        assertEquals(listOf("submit", "history"), rec.ops)
        assertFalse(Model.chatActive)
        assertTrue(Model.canSendChat)
        assertUnconfirmedShown()
    }

    @Test fun markSubmittedTurnUnconfirmedIgnoresOtherTurnIds() {
        val rec = RecordingSend()
        Model.submitChat(rec.send)
        val turnId = rec.submitted!!.getString("id")
        Model.markSubmittedTurnUnconfirmed("someone-else")
        assertTrue(Model.chatActive)
        assertFalse(Model.canSendChat)
        Model.markSubmittedTurnUnconfirmed(turnId)
        assertFalse(Model.chatActive)
        assertTrue(Model.canSendChat)
        assertUnconfirmedShown()
    }

    @Test fun refreshHistoryForSubmittedTurnSendsHistoryForTheSubmittedTurnOnly() {
        val rec = RecordingSend()
        Model.submitChat(rec.send)
        val turnId = rec.submitted!!.getString("id")
        Model.refreshHistoryForSubmittedTurn("someone-else", rec.send)
        assertEquals(listOf("submit"), rec.ops)
        Model.refreshHistoryForSubmittedTurn(turnId, rec.send)
        assertEquals(listOf("submit", "history"), rec.ops)
        // Still locked: the re-fetch alone never clears the gate — only
        // a resolving history or the 45 s timeout does.
        assertTrue(Model.chatActive)
    }

    @Test fun unconfirmedSurfacesDeliveryUnconfirmedString() {
        val state = DesktopChatState { 0L }
        state.submitted(JSONObject().put("id", "t1").put("text", "Hello").put("assistant", "codex").put("model", "fixture"))
        state.unconfirmed("t1")
        assertEquals("unknown", state.messages.first { it.turnId == "t1" && it.role == "user" }.status)
        val notice = state.messages.firstOrNull { it.role == "assistant" && it.turnId == "t1" }
        assertNotNull(notice)
        assertEquals("Delivery unconfirmed. Check before resending.", notice!!.text)
        assertEquals("unknown", notice.status)
    }

    @Test fun serverSideUnknownStillSurfacesSameString() {
        // The two paths must never diverge: a server-side unknown turn
        // (snapshot) and a locally timed-out turn (unconfirmed) show the
        // same wording.
        val state = DesktopChatState { 0L }
        val turn = JSONObject().put("id", "t2").put("text", "Q").put("status", "unknown")
        state.snapshot(listOf(turn))
        assertEquals("Delivery unconfirmed. Check before resending.",
            state.messages.first { it.role == "assistant" }.text)
    }
}
