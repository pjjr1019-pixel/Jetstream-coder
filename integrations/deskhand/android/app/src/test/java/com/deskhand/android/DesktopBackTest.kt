package com.deskhand.android

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Text
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DesktopBackTest {
    @get:Rule val compose = createComposeRule()
    @Test fun oneClickOneRequestAndMatchingResult() {
        val requests = mutableListOf<JSONObject>()
        val back = DesktopBackController({ type, bytes -> assertEquals(Msg.DESKTOP_BACK, type); requests.add(JSONObject(bytes.toString(Charsets.UTF_8))); true }, { _, _ -> })
        back.info(true, true)
        back.closeLatest()
        back.closeLatest()
        assertEquals(1, requests.size)
        back.message(JSONObject().put("requestId", "unrelated").put("error", "empty").toString().toByteArray())
        assertTrue(back.busy)
        back.message(JSONObject().put("requestId", requests.single().getString("requestId")).put("error", JSONObject.NULL).put("windowTitle", "Folder").toString().toByteArray())
        assertTrue(back.enabled)
        assertEquals("Close requested: Folder. Check for a save prompt.", back.notice)
    }

    @Test fun unsupportedViewOnlyAndSessionLossNeverSend() {
        var sends = 0
        val back = DesktopBackController({ _, _ -> sends++; true }, { _, _ -> })
        back.info(false, true); back.closeLatest()
        back.info(true, false); back.closeLatest()
        assertEquals(0, sends)
        back.info(true, true); back.closeLatest()
        assertEquals(1, sends)
        back.sessionLost(); back.closeLatest()
        assertFalse(back.enabled); assertFalse(back.busy)
        assertEquals(1, sends)
    }

    @Test fun timeoutDoesNotRepeatCloseAndLateAnswerCanResolve() {
        val timers = mutableListOf<() -> Unit>(); var id = ""; var sends = 0
        val back = DesktopBackController({ _, bytes -> sends++; id = JSONObject(bytes.toString(Charsets.UTF_8)).getString("requestId"); true }, { _, action -> timers.add(action) })
        back.info(true, true); back.closeLatest(); timers.first()()
        assertFalse(back.enabled); back.closeLatest(); assertEquals(1, sends)
        back.message(JSONObject().put("requestId", id).put("error", "empty").toString().toByteArray())
        assertTrue(back.enabled)
        assertEquals("No windows opened from Files to close.", back.notice)
    }

    @Test fun refusedTransportAndUncertainReplyPreserveSafeState() {
        val back = DesktopBackController({ _, _ -> false }, { _, _ -> })
        back.info(true, true); back.closeLatest()
        assertFalse(back.busy); assertTrue(back.enabled)
        var id = ""
        val unknown = DesktopBackController({ _, bytes -> id = JSONObject(bytes.toString(Charsets.UTF_8)).getString("requestId"); true }, { _, _ -> })
        unknown.info(true, true); unknown.closeLatest()
        unknown.message(JSONObject().put("requestId", id).put("error", "unknown").toString().toByteArray())
        assertFalse(unknown.enabled)
        assertTrue(unknown.notice!!.contains("reconnect"))
    }

    @Test
    @org.robolectric.annotation.GraphicsMode(org.robolectric.annotation.GraphicsMode.Mode.NATIVE)
    fun foregroundWindowButtonsRenderGlassBorderAndKeepActions() {
        // 100.050 (§4.3 / §2 Anchor 4): the Surface wrapper is gone —
        // ForegroundWindowButtons renders transparent buttons with no
        // background of its own; the toolbar's GlassSurface (100.043) is
        // the only surface. The 100.043 glass-border pixel expectation is
        // therefore stale for the isolated buttons: there is no surface
        // left here to tint, and the red backdrop shows straight through
        // (that is what the Gate 4 failure observed: Actual -65536 is
        // Color.Red). Transparency is observed by rendering, same method
        // as the old check: pixels inside each button, clear of its glyph,
        // must equal the backdrop red. The glass tint itself stays
        // asserted at the toolbar/ViewerLayout level
        // (glassSurfacesUseSharedGlassSurface: shared DeskhandGlass
        // contract — SurfaceAlpha 0.60f, BorderAlpha 0.30f, 1.dp border —
        // PASSED in the same Gate 4 run). No Model state is touched; the
        // actions are local. NATIVE graphics mode follows the
        // zoomedDragPansAndSingleTapClicks precedent in ViewerLayoutTest.
        // Test name kept from 100.043 so Gate 4 tracks the same test.
        val actions = mutableListOf<String>()
        compose.setContent { DeskhandViewerTheme {
            Box(Modifier.size(200.dp, 100.dp).background(Color.Red), contentAlignment = Alignment.Center) {
                ForegroundWindowButtons(true, false) { actions.add(it) }
            }
        } }
        compose.runOnIdle { }
        compose.onNodeWithTag("minimizeFocusedWindow").assertIsDisplayed()
        compose.onNodeWithTag("closeFocusedWindow").assertIsDisplayed()
        compose.onNodeWithTag("minimizeFocusedWindow").performClick()
        compose.onNodeWithTag("closeFocusedWindow").performClick()
        assertEquals(listOf("minimize", "close"), actions)

        // Button bounds from the measured nodes (the Row wraps them
        // exactly). Each button is sampled at a corner inset 10% from its
        // top-left — clear of the centred "−" / "×" glyph — where no
        // surface of its own exists, so the pixel must be the red
        // backdrop showing straight through.
        val minBounds = compose.onNodeWithTag("minimizeFocusedWindow").fetchSemanticsNode().boundsInRoot
        val closeBounds = compose.onNodeWithTag("closeFocusedWindow").fetchSemanticsNode().boundsInRoot
        val minSampleX = (minBounds.left + minBounds.width * 0.1f).roundToInt()
        val minSampleY = (minBounds.top + minBounds.height * 0.1f).roundToInt()
        val closeSampleX = (closeBounds.left + closeBounds.width * 0.1f).roundToInt()
        val closeSampleY = (closeBounds.top + closeBounds.height * 0.1f).roundToInt()
        val image = compose.onRoot().captureToImage()
        val minPixel = IntArray(1)
        val closePixel = IntArray(1)
        image.readPixels(minPixel, minSampleX, minSampleY, 1, 1)
        image.readPixels(closePixel, closeSampleX, closeSampleY, 1, 1)
        assertEquals("window buttons render transparent — red backdrop shows through the minimize button", -65536, minPixel[0])
        assertEquals("window buttons render transparent — red backdrop shows through the close button", -65536, closePixel[0])
    }
}
