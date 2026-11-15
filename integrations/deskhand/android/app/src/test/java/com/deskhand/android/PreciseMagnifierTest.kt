package com.deskhand.android

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class PreciseMagnifierTest {
    @get:Rule val compose = createComposeRule()
    private val sent = mutableListOf<JSONObject>()
    private fun setup(canControl: Boolean = true, imageHeight: Int = 100) {
        val image = ImageBitmap(100, imageHeight)
        val zoom = DesktopZoomState()
        zoom.applyFit(true)  // 100.011: test the magnifier at 1.0x fit (default zoom is now 1.5x)
        compose.setContent { DeskhandViewerTheme {
            Box(Modifier.size(300.dp)) { DesktopView(image, zoom, canControl = canControl, send = { sent.add(it) }) }
        } }
        compose.mainClock.autoAdvance = false
    }
    private fun holdAt(point: Offset) {
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput { down(point) }
        compose.mainClock.advanceTimeBy(700)
    }
    @Test fun longPressShowsLensAndReleaseClicksAdjustedPoint() {
        setup()
        holdAt(Offset(120f, 120f))
        compose.onNodeWithTag("preciseMagnifier").assertExists()
        compose.runOnIdle { assertTrue(sent.isEmpty()) }
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput { moveTo(Offset(180f, 210f)); up() }
        compose.mainClock.advanceTimeBy(100)
        compose.runOnIdle {
            assertEquals(listOf("move", "down", "up"), sent.map { it.getString("t") })
            assertEquals(0.6, sent.first().getDouble("x"), 0.01)
            assertEquals(0.7, sent.first().getDouble("y"), 0.01)
            assertFalse(sent[1].has("b"))
        }
        compose.mainClock.advanceTimeBy(100)
        compose.onNodeWithTag("preciseMagnifier").assertDoesNotExist()
    }
    @Test fun holdingInLetterboxDoesNotShowLensOrClick() {
        setup(imageHeight = 50)
        holdAt(Offset(100f, 20f))
        compose.onNodeWithTag("preciseMagnifier").assertDoesNotExist()
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput { up() }
        compose.runOnIdle { assertTrue(sent.isEmpty()) }
    }
    @Test fun canceledTouchNeverClicks() {
        setup()
        holdAt(Offset(120f, 120f))
        compose.onNodeWithTag("preciseMagnifier").assertExists()
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput { cancel() }
        compose.runOnIdle { assertTrue("Canceled touch must not click", sent.isEmpty()) }
        compose.mainClock.advanceTimeBy(100)
        compose.onNodeWithTag("preciseMagnifier").assertDoesNotExist()
    }
    @Test fun anotherFingerCancelsEvenWhenControlEnabled() {
        setup()
        holdAt(Offset(120f, 120f))
        compose.onNodeWithTag("preciseMagnifier").assertExists()
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput { down(1, Offset(160f, 160f)); up(1); up(0) }
        compose.runOnIdle { assertTrue("Interrupted gesture must not click", sent.isEmpty()) }
        compose.mainClock.advanceTimeBy(100)
        compose.onNodeWithTag("preciseMagnifier").assertDoesNotExist()
    }
    @Test fun anotherFingerCancelsAndViewOnlyCannotClick() {
        setup(canControl = false)
        holdAt(Offset(120f, 120f))
        compose.onNodeWithTag("preciseMagnifier").assertExists()
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput { up() }
        compose.runOnIdle { assertTrue(sent.isEmpty()) }
        holdAt(Offset(120f, 120f))
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput { down(1, Offset(160f, 160f)); up(1); up(0) }
        compose.runOnIdle { assertTrue(sent.isEmpty()) }
        compose.mainClock.advanceTimeBy(100)
        compose.onNodeWithTag("preciseMagnifier").assertDoesNotExist()
    }
}
