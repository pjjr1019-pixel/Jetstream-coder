package com.deskhand.android

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DesktopChatStateTest {
    private var time = 0L
    private fun state() = DesktopChatState { time }
    private fun turn(id: String, status: String = "completed", body: String = "Answer") = JSONObject()
        .put("id", id).put("text", "Question $id").put("assistant", "codex").put("model", "original-model")
        .put("status", status).put("reply", if (status == "completed") body else JSONObject.NULL)
    private fun command(id: String) = JSONObject().put("id", id).put("text", "Question $id")

    @Test fun initialHistoryDoesNotReplayAndRepeatedSnapshotsDoNotResetTimers() {
        val s = state(); s.snapshot(listOf(turn("old")))
        assertTrue(s.bubbles.isEmpty()); assertEquals(2, s.messages.size)
        s.snapshot(listOf(turn("old"), turn("new")))
        assertEquals(listOf("new/user", "new/assistant"), s.bubbles.map { it.message.key })
        time = 2000; s.snapshot(listOf(turn("old"), turn("new")))
        assertTrue(s.bubbles.all { it.born == 0L })
        time = DesktopChatState.LifetimeMs; s.expire()
        assertTrue(s.bubbles.isEmpty()); assertEquals(4, s.messages.size)
    }

    @Test fun newestThreeOnlyAndAckUpdatesWithoutRebirth() {
        val s = state(); s.snapshot(emptyList())
        repeat(4) { time = it * 100L; s.submitted(command("$it")) }
        assertEquals(listOf("1/user", "2/user", "3/user"), s.bubbles.map { it.message.key })
        val born = s.bubbles.last().born; time = 2500; s.acknowledged("3")
        assertEquals(born, s.bubbles.last().born)
        assertEquals("Queued", s.bubbles.last().message.status)
    }

    @Test fun localSubmissionAndTerminalReplyDeduplicateAndKeepActualModel() {
        val s = state(); s.snapshot(emptyList()); s.submitted(command("one"))
        s.snapshot(listOf(turn("one", "queued"))); s.snapshot(listOf(turn("one", "completed")))
        s.snapshot(listOf(turn("one", "completed")))
        assertEquals(2, s.bubbles.size)
        assertEquals("codex · original-model", s.bubbles.last().message.label)
    }

    @Test fun reconnectSuppressesOldMessagesButSurfacesPendingReplyOnce() {
        val s = state(); s.snapshot(emptyList()); s.submitted(command("pending"))
        time = 10000; s.expire(); s.reconnect()
        s.snapshot(listOf(turn("offline"), turn("pending")))
        assertEquals(listOf("pending/assistant"), s.bubbles.map { it.message.key })
        s.reconnect(); s.snapshot(listOf(turn("offline"), turn("pending")))
        assertEquals(1, s.bubbles.size)
    }

    @Test fun failurePreviewAndFullHistoryRetainLongText() {
        val s = state(); s.snapshot(emptyList()); s.submitted(command("one"))
        val longError = "Failure details\n".repeat(100)
        s.rejected("one", longError); s.rejected("one", longError)
        assertEquals(2, s.bubbles.size)
        assertEquals(longError, s.messages.last().text)
        assertEquals("Failed", s.messages.first().status)
        time = 10000; s.expire(); assertTrue(s.bubbles.isEmpty()); assertEquals(longError, s.messages.last().text)
    }

    @Test fun pcSwitchClearsBubblesHistoryAndBaseline() {
        val s = state(); s.submitted(command("one")); s.reset()
        s.snapshot(listOf(turn("other-pc")))
        assertTrue(s.bubbles.isEmpty()); assertFalse(s.messages.any { it.turnId == "one" })
    }

    @Test fun terminalUnknownAndFailedRepliesOnlyAppearOnce() {
        val s = state(); s.snapshot(emptyList())
        val failed = turn("bad", "failed").put("error", "Request failed")
        s.snapshot(listOf(failed)); s.snapshot(listOf(failed))
        assertEquals(1, s.bubbles.count { it.message.role == "assistant" })
        s.snapshot(listOf(failed, turn("uncertain", "unknown")))
        assertTrue(s.messages.last().text.contains("unconfirmed"))
    }

    @Test fun dispatchingPartialShowsInProgressAndFinalReplyReplacesIt() {
        val s = state(); s.snapshot(emptyList()); s.submitted(command("one"))
        val dispatching = turn("one", "dispatching").put("partial", "Draft ans").put("partial_seq", 3)
        s.snapshot(listOf(dispatching))
        assertEquals("Draft ans …", s.messages.single { it.key == "one/assistant" }.text)
        assertEquals("dispatching", s.messages.single { it.key == "one/assistant" }.status)
        s.snapshot(listOf(turn("one", "completed", "Final answer")))
        assertEquals(1, s.messages.count { it.key == "one/assistant" })
        assertEquals("Final answer", s.messages.single { it.key == "one/assistant" }.text)
    }

    @Test fun restartChatResponseClearsLocalChatState() {
        // 100.013 (1b): the restart_chat response clears the phone-side
        // chat state the same way a PC switch does — turns, stop banner,
        // draft, and the desktop-chat bubbles/history.
        Model.chatConnected = true
        Model.chatDraft = "half-typed draft"
        Model.chatTurns = listOf(turn("old"))
        Model.requestChatStop { "stop-rpc" }
        assertTrue(Model.chatStopPending)
        assertNotNull(Model.chatStopStatus)
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "restart_chat").put("result", JSONObject()))
        assertFalse(Model.chatStopPending)
        assertFalse(Model.restartPending)
        assertNull(Model.chatStopStatus)
        assertEquals("", Model.chatDraft)
        assertTrue(Model.chatTurns.isEmpty())
        assertTrue(Model.desktopChat.messages.isEmpty())
        assertEquals("Chat restarted", Model.chatStatus)
        Model.chatConnected = false
    }

    @Test fun newTerminalReplyFiresVoiceCallbackExactlyOnce() {
        val s = state(); s.snapshot(emptyList())
        val spoken = mutableListOf<String>()
        s.snapshot(listOf(turn("one", "completed", "Final answer"))) { spoken.add(it.text) }
        assertEquals(listOf("Final answer"), spoken)
        // The history poll re-delivers the same turn: never spoken twice.
        s.snapshot(listOf(turn("one", "completed", "Final answer"))) { spoken.add(it.text) }
        s.snapshot(listOf(turn("one", "completed", "Final answer"))) { spoken.add(it.text) }
        assertEquals(listOf("Final answer"), spoken)
    }

    @Test fun historicalBaselineAndPartialsDoNotFireVoiceCallback() {
        val s = state()
        val spoken = mutableListOf<String>()
        // First snapshot is the baseline: historical replies stay silent.
        s.snapshot(listOf(turn("old"))) { spoken.add(it.text) }
        assertTrue(spoken.isEmpty())
        // A dispatching partial is not a reply: silent, even on re-delivery.
        s.snapshot(listOf(turn("old"), turn("new", "dispatching").put("partial", "Draft"))) { spoken.add(it.text) }
        assertTrue(spoken.isEmpty())
        // The completed reply for the same turn fires once (the earlier
        // partial must not consume the exactly-once slot).
        s.snapshot(listOf(turn("old"), turn("new", "completed", "Done"))) { spoken.add(it.text) }
        assertEquals(listOf("Done"), spoken)
    }

    @Test fun rejectedTurnErrorFiresVoiceCallbackOnce() {
        val s = state(); s.snapshot(emptyList()); s.submitted(command("one"))
        val spoken = mutableListOf<String>()
        s.rejected("one", "Request failed") { spoken.add(it.text) }
        assertEquals(listOf("Request failed"), spoken)
        s.rejected("one", "Request failed") { spoken.add(it.text) }
        assertEquals(listOf("Request failed"), spoken)
    }

    @Test fun animationTimingAndDisabledAnimationsHaveDeterministicDeadlines() {
        assertEquals(0f, bubbleMotion(0, true).alpha, 0f)
        assertEquals(1f, bubbleMotion(220, true).alpha, 0f)
        assertEquals(1f, bubbleMotion(6220, true).alpha, 0f)
        assertEquals(0.5f, bubbleMotion(6520, true).alpha, 0.01f)
        assertEquals(-12f, bubbleMotion(6520, true).riseDp, 0.01f)
        assertEquals(0f, bubbleMotion(6820, true).alpha, 0f)
        assertEquals(BubbleMotion(1f, 0f), bubbleMotion(100, false))
        assertEquals(0f, bubbleMotion(6820, false).alpha, 0f)
    }
}
