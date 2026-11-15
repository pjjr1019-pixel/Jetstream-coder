package com.deskhand.android

import androidx.compose.runtime.*
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

// 100.025 (S4/S13): agent-only stop wiring. Model harness is the same
// shape as ChatSubmissionTest: drive Model.requestAgentStop with a fake
// send lambda and feed handleChatResponse results directly — no PC, no
// assistant, no transport. Every test completes its flow; a test that
// fails mid-flow can leak agent-stop state into the next test, so the
// @Before below also clears it via the production restart_chat path
// (the square button's restart success clears agent-stop state along
// with chat state).
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class AgentStopTest {
    @get:Rule val compose = createComposeRule()

    @Before fun prepare() {
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "restart_chat").put("result", JSONObject()))
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "resume_chat").put("result", JSONObject()))
        Model.chatTurns = emptyList()
        Model.desktopChat.reset()
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "submit").put("rpc", "rpc").put("result", JSONObject()))
        Model.chatRoutes = listOf(JSONObject().put("assistant", "codex").put("model", "fixture").put("account", "test"))
        Model.chatRouteIndex = 0
        Model.chatRouteMode = "Fast"
        Model.chatRouteReason = ""
        Model.chatDraft = "Hello"
        Model.chatConnected = true
    }

    @After fun cleanup() {
        // Leave no agent-stop state behind: later test classes render
        // DesktopChatComposer with its Model defaults, and a leaked
        // status line would change the composer's height under them.
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "restart_chat").put("result", JSONObject()))
    }

    private fun agentStopResult(rpc: String, status: String) = JSONObject().put("ok", true)
        .put("op", "stop_agent_work").put("rpc", rpc)
        .put("result", JSONObject().put("status", status)
            .put("interrupt_requested", true)
            .put("queued_cancelled", org.json.JSONArray())
            .put("drained", true).put("still_running", false))

    @Test fun agentStopSendsStopAgentWorkOp() {
        var sent: JSONObject? = null
        Model.requestAgentStop { sent = JSONObject(it.toString()); "agent-stop-rpc" }
        assertEquals("stop_agent_work", sent!!.optString("op"))
        assertTrue(Model.agentStopPending)
        assertEquals("Requesting agent stop…", Model.agentStopStatus)
    }

    @Test fun agentStopBlocksDuplicateUntilResult() {
        var sends = 0
        Model.requestAgentStop { sends++; "agent-stop-rpc" }
        Model.requestAgentStop { sends++; "duplicate-rpc" }
        assertEquals(1, sends)
        assertTrue(Model.agentStopPending)
        Model.handleChatResponse(agentStopResult("agent-stop-rpc", "Agent work stopped."))
        assertFalse(Model.agentStopPending)
        Model.requestAgentStop { sends++; "agent-stop-rpc-2" }
        assertEquals(2, sends)
    }

    // The service wording is stored verbatim — including the
    // uncancellable-adapter wording from the spec. A hardcoded
    // wording could not match both this and the queued case below.
    @Test fun agentStopResultStoresStatusVerbatim() {
        val status = "STOP will prevent the next action; the running one finishes"
        Model.requestAgentStop { "agent-stop-rpc" }
        Model.handleChatResponse(agentStopResult("agent-stop-rpc", status))
        assertFalse(Model.agentStopPending)
        assertEquals(status, Model.agentStopStatus)
        assertNull(Model.chatStopStatus)
    }

    @Test fun agentStopResultStoresWhateverWordingTheServerSends() {
        val status = "Agent work stopped: 2 queued agent turns cancelled, no running turn."
        Model.requestAgentStop { "agent-stop-rpc" }
        Model.handleChatResponse(agentStopResult("agent-stop-rpc", status))
        assertEquals(status, Model.agentStopStatus)
    }

    @Test fun agentStopFailureResponseIsUnconfirmed() {
        Model.requestAgentStop { "agent-stop-rpc" }
        Model.handleChatResponse(JSONObject().put("ok", false).put("op", "stop_agent_work")
            .put("rpc", "agent-stop-rpc").put("error", "PC link down"))
        assertFalse(Model.agentStopPending)
        assertEquals("Agent stop unconfirmed: PC link down", Model.agentStopStatus)
    }

    @Test fun agentStopTimeoutIsUnconfirmedAndIgnoresLateReceipt() {
        Model.requestAgentStop { "agent-stop-rpc" }
        assertTrue(Model.agentStopPending)
        Model.markAgentStopUnconfirmed("different-rpc")
        assertTrue(Model.agentStopPending)
        Model.markAgentStopUnconfirmed("agent-stop-rpc")
        assertFalse(Model.agentStopPending)
        assertEquals("Agent stop unconfirmed: no response from PC", Model.agentStopStatus)
        Model.handleChatResponse(agentStopResult("agent-stop-rpc", "incorrect success"))
        assertEquals("Agent stop unconfirmed: no response from PC", Model.agentStopStatus)
    }

    @Test fun agentStopWithNoTransportIsUnconfirmed() {
        Model.requestAgentStop { null }
        assertFalse(Model.agentStopPending)
        assertEquals("Agent stop unconfirmed: connection unavailable", Model.agentStopStatus)
    }

    // Agent-only stop must not touch the chat-stop path and must not
    // block sending: chat admissions are untouched by stop_agent_work.
    @Test fun agentStopDoesNotTouchChatStopOrBlockSending() {
        Model.requestAgentStop { "agent-stop-rpc" }
        assertNull(Model.chatStopStatus)
        assertFalse(Model.chatStopPending)
        Model.handleChatResponse(agentStopResult("agent-stop-rpc", "Agent work stopped."))
        assertNull(Model.chatStopStatus)
        assertTrue(Model.canSendChat)
        assertEquals("Hello", Model.chatDraft)
    }

    @Test fun restartChatClearsAgentStopStatus() {
        Model.requestAgentStop { "agent-stop-rpc" }
        Model.handleChatResponse(agentStopResult("agent-stop-rpc", "Agent work stopped."))
        assertEquals("Agent work stopped.", Model.agentStopStatus)
        Model.handleChatResponse(JSONObject().put("ok", true).put("op", "restart_chat").put("result", JSONObject()))
        assertNull(Model.agentStopStatus)
        assertFalse(Model.agentStopPending)
    }

    @Test fun composerShowsStopAgentOnlyWhileActive() {
        var agentStops = 0
        var active by mutableStateOf(true)
        var canStop by mutableStateOf(true)
        var canStopAgent by mutableStateOf(true)
        compose.setContent { DeskhandViewerTheme {
            DesktopChatComposer("", {}, false, false, {}, {}, {},
                active = active, canStop = canStop, onStop = {},
                canStopAgent = canStopAgent, onStopAgent = { agentStops++ }, agentStopStatus = null)
        } }
        compose.onNodeWithContentDescription("Stop agent").assertIsDisplayed()
        active = false
        canStop = false
        canStopAgent = false
        compose.onAllNodesWithContentDescription("Stop agent").assertCountEquals(0)
        assertEquals(0, agentStops)
    }

    @Test fun composerStopAgentCallsHandlerNotSquareButton() {
        var agentStops = 0
        var squareStops = 0
        compose.setContent { DeskhandViewerTheme {
            DesktopChatComposer("", {}, false, false, {}, {}, {},
                active = true, canStop = true, onStop = { squareStops++ },
                canStopAgent = true, onStopAgent = { agentStops++ }, agentStopStatus = null)
        } }
        compose.onNodeWithContentDescription("Stop agent").performClick()
        assertEquals(1, agentStops)
        assertEquals(0, squareStops)
    }

    @Test fun composerShowsAgentStopStatusVerbatim() {
        val status = "STOP will prevent the next action; the running one finishes"
        var agentStopStatus by mutableStateOf<String?>(status)
        compose.setContent { DeskhandViewerTheme {
            DesktopChatComposer("", {}, false, false, {}, {}, {},
                active = true, canStop = true, onStop = {},
                canStopAgent = true, onStopAgent = {}, agentStopStatus = agentStopStatus)
        } }
        compose.onNodeWithText(status).assertIsDisplayed()
        compose.onNodeWithTag("agentStopStatus").assertIsDisplayed()
        agentStopStatus = null
        compose.onAllNodesWithTag("agentStopStatus").assertCountEquals(0)
    }
}
