package com.deskhand.android

import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ForegroundWindowTest {
    @get:Rule val compose = createComposeRule()
    @Test fun buttonsRespectPermissionAndOnlySendOnce() {
        val sent = mutableListOf<JSONObject>()
        val controller = ForegroundWindowController({ type, bytes ->
            assertEquals(Msg.FOREGROUND_WINDOW, type); sent.add(JSONObject(String(bytes))); true
        }, { _, _ -> })
        compose.setContent { DeskhandViewerTheme { ForegroundWindowButtons(controller.enabled, controller.busy, controller::act) } }
        compose.onNodeWithTag("minimizeFocusedWindow").assertIsNotEnabled()
        controller.info(true, true)
        compose.onNodeWithTag("minimizeFocusedWindow").assertIsEnabled().performClick()
        compose.onNodeWithTag("closeFocusedWindow").assertIsNotEnabled()
        assertEquals("minimize", sent.single().getString("action"))
        controller.message(JSONObject().put("requestId", "wrong").toString().toByteArray())
        assertTrue(controller.busy)
        controller.message(JSONObject().put("requestId", sent.single().getString("requestId")).put("error", JSONObject.NULL).toString().toByteArray())
        compose.onNodeWithTag("closeFocusedWindow").assertIsEnabled().performClick()
        assertEquals("close", sent.last().getString("action"))
        controller.sessionLost()
        compose.onNodeWithTag("closeFocusedWindow").assertIsNotEnabled()
    }
    @Test fun timeoutNeverRetriesOrAllowsAnotherAction() {
        val timers = mutableListOf<() -> Unit>(); var count = 0
        val c = ForegroundWindowController({ _, _ -> count++; true }, { _, action -> timers.add(action) })
        c.info(true, true); c.act("close"); timers.single()(); c.act("minimize")
        assertEquals(1, count); assertTrue(c.busy); assertTrue(c.notice!!.contains("unconfirmed"))
    }
}
