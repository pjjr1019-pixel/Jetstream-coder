package com.deskhand.android

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusManager
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.sp
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import kotlin.math.abs
import kotlin.math.roundToInt

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp")
class DesktopChatUiTest {
    @get:Rule val compose = createComposeRule()

    @Test fun bubblesLeaveDesktopAndArrowTouchableAndOpenFullMessage() {
        compose.mainClock.autoAdvance = false
        var time = 0L
        val state = DesktopChatState { time }
        val longText = "Full message line\n".repeat(30)
        state.submitted(JSONObject().put("id", "one").put("text", longText))
        time = 300
        var desktopClicks = 0; var selected: ChatMessage? = null
        compose.setContent { DeskhandViewerTheme {
            DesktopStreamOverlay("Host notice",
                content = { Box(Modifier.fillMaxSize().testTag("image").clickable { desktopClicks++ }) },
                bubbles = { ChatBubbleLane(state, it) { message -> selected = message } })
        } }
        compose.onNodeWithTag("image").performTouchInput { click(Offset(10f, 250f)) }
        assertEquals(1, desktopClicks)
        compose.onNodeWithTag("closeLastWindow").assertDoesNotExist()
        compose.onNodeWithTag("bubble-one/user").assertIsDisplayed().performClick()
        assertEquals(longText, selected!!.text)
        val bubble = compose.onNodeWithTag("bubble-one/user").fetchSemanticsNode().boundsInRoot
        val notice = compose.onNodeWithText("Host notice").fetchSemanticsNode().boundsInRoot
        assertTrue(bubble.bottom <= notice.top)
        compose.runOnIdle { time = 10000; state.expire() }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("bubble-one/user").assertDoesNotExist()
        assertEquals(longText, state.messages.single().text)
    }

    @Test fun viewerTypographyIsBumped() {
        assertEquals(17.sp, DeskhandViewerTypography.bodyMedium.fontSize)
        assertEquals(14.sp, DeskhandViewerTypography.bodySmall.fontSize)
        assertEquals(14.sp, DeskhandViewerTypography.labelMedium.fontSize)
        assertEquals(13.sp, DeskhandViewerTypography.labelSmall.fontSize)
        assertEquals(19.sp, DeskhandViewerTypography.titleMedium.fontSize)
        assertEquals(26.sp, DeskhandViewerTypography.titleLarge.fontSize)
        assertEquals(34.sp, DeskhandViewerTypography.headlineMedium.fontSize)
    }

    @Test fun settingsRetainModelEffortStatusAndRefresh() {
        Model.chatConnected = true; Model.chatStatus = "Ready"; Model.chatDraft = ""
        Model.chatRoutes = listOf(
            JSONObject().put("name", "First AI").put("effort", "low").put("efforts", org.json.JSONArray(listOf("low", "high"))),
            JSONObject().put("name", "Second AI").put("effort", "medium").put("efforts", org.json.JSONArray(listOf("medium", "high"))))
        Model.chatRouteIndex = 0; Model.chatEffort = "low"
        compose.setContent { DeskhandViewerTheme { AssistantSettings(Modifier.fillMaxSize()) } }
        compose.onNodeWithText("Chat ready").assertIsDisplayed()
        compose.onNodeWithText("Computer tasks are unavailable for this assistant.").assertIsDisplayed()
        compose.onNodeWithText("First AI").performClick()
        compose.onNodeWithText("Second AI").performClick()
        assertEquals(1, Model.chatRouteIndex); assertEquals("medium", Model.chatEffort)
        compose.onNodeWithText("medium").performClick()
        compose.onNodeWithText("high").performClick()
        assertEquals("high", Model.chatEffort)
        compose.onNodeWithText("Refresh assistants").assertIsEnabled()
    }



    @Test fun historySheetShowsFullTextAndOpeningPanelsDoesNotRebirthBubbles() {
        Model.desktopChat.reset()
        Model.desktopChat.submitted(JSONObject().put("id", "history-test").put("text", "Whole text\n".repeat(30)))
        val born = Model.desktopChat.bubbles.single().born
        var panel by mutableStateOf<String?>(null)
        compose.setContent { DeskhandViewerTheme {
            DesktopChatComposer("", {}, false, false, {}, { panel = "settings" }, { panel = "history" })
            DesktopChatPanels(panel, "history-test/user") { panel = null }
        } }
        compose.onNodeWithContentDescription("Chat history").performClick()
        compose.mainClock.advanceTimeBy(1000)
        compose.onNodeWithTag("history-history-test/user").assertIsDisplayed()
        assertEquals(born, Model.desktopChat.bubbles.single().born)
        compose.onNodeWithText("Done").performClick()
        compose.onNodeWithContentDescription("Assistant settings").performClick()
        compose.mainClock.advanceTimeBy(1000)
        compose.onNodeWithText("Refresh assistants").assertIsDisplayed()
        assertEquals(born, Model.desktopChat.bubbles.single().born)
    }

    @Test fun composerVoiceToggleShowsStateAndCallsToggle() {
        var voiceOn by mutableStateOf(false)
        var toggles = 0
        compose.setContent { DeskhandViewerTheme {
            DesktopChatComposer("", {}, false, false, {}, {}, {},
                voiceOn = voiceOn, onVoiceToggle = { toggles++; voiceOn = !voiceOn })
        } }
        compose.onNodeWithTag("voiceRepliesToggle").assertIsDisplayed()
        compose.onNodeWithContentDescription("Voice replies off").assertIsDisplayed()
        compose.onNodeWithTag("voiceRepliesToggle").performClick()
        assertEquals(1, toggles)
        compose.onNodeWithContentDescription("Voice replies on").assertIsDisplayed()
        compose.onNodeWithTag("voiceRepliesToggle").performClick()
        assertEquals(2, toggles)
        compose.onNodeWithContentDescription("Voice replies off").assertIsDisplayed()
    }

    // 100.043 (C2): pixel helpers — a Surface's color / border arguments
    // are not exposed in semantics, so the glass values are asserted from
    // the rendered pixels (captureToImage, NATIVE graphics mode — the
    // same mode the zoomed-drag test already uses). The capture is
    // self-calibrating: the background the capture actually sees is
    // sampled just outside the pill (x = 3.dp, inside the node's h6
    // margin) and the expected fill / border are composited over it, so
    // the assertions hold whether the capture background is transparent
    // or an opaque window colour.
    private data class ComposerPixels(val background: Color, val fill: Color, val border: Color)

    private fun captureComposerPixels(): ComposerPixels {
        val bitmap = compose.onNodeWithTag("chatComposer").captureToImage().asAndroidBitmap()
        val bounds = compose.onNodeWithTag("chatComposer").fetchSemanticsNode().boundsInRoot
        val density = bitmap.width / bounds.width
        fun at(xDp: Float, yDp: Float): Color {
            val x = (xDp * density).roundToInt().coerceIn(0, bitmap.width - 1)
            val y = (yDp * density).roundToInt().coerceIn(0, bitmap.height - 1)
            return Color(bitmap.getPixel(x, y))
        }
        // Background: the node's top-left corner pixel (0, 0). Do NOT
        // sample inside the node: the h6 padding is on GlassSurface's
        // own modifier, so the node's bounds ARE the pill and its fill
        // covers the full width — the old 3.dp sample read the fill
        // itself. Only the corners are bare: the 28.dp rounded shape
        // leaves the corner outside its arc (and clear of the 1.dp
        // border, which follows the shape edge). Fill: top strip of the
        // pill at 5.dp — below the 1.dp border, above the centred
        // icons and hint text. Border: the pill's top edge, centre x
        // (a straight edge, and the stroke is drawn inside the bounds).
        return ComposerPixels(
            background = at(0f, 0f),
            fill = at(bounds.width / 2f, 5f),
            border = at(bounds.width / 2f, 0f)
        )
    }

    /** Composite [top] over [bottom], as Bitmap.getPixel reports it. */
    private fun over(top: Color, bottom: Color): Color {
        val at = top.alpha; val ab = bottom.alpha
        val a = at + ab * (1f - at)
        if (a <= 0f) return Color.Transparent
        return Color(
            (top.red * at + bottom.red * ab * (1f - at)) / a,
            (top.green * at + bottom.green * ab * (1f - at)) / a,
            (top.blue * at + bottom.blue * ab * (1f - at)) / a,
            a
        )
    }

    private fun assertPixelClose(actual: Color, expected: Color, tolerance: Float, message: String) {
        assertTrue("$message expected $expected but was $actual",
            abs(actual.red - expected.red) <= tolerance &&
                abs(actual.green - expected.green) <= tolerance &&
                abs(actual.blue - expected.blue) <= tolerance &&
                abs(actual.alpha - expected.alpha) <= tolerance)
    }

    @Test
    @org.robolectric.annotation.GraphicsMode(org.robolectric.annotation.GraphicsMode.Mode.NATIVE)
    fun composerGlassFillIsTranslucent() {
        // 100.043 (C2): the composer fill is surface at 0.60f — not the
        // old surfaceVariant at 0.45f. The old fill would read an alpha
        // of ~115 over transparency (or the lighter surfaceVariant blend
        // over an opaque background); the C2 fill reads 153.
        compose.setContent { DeskhandViewerTheme {
            DesktopChatComposer("", {}, false, false, {}, {}, {},
                agentStopStatus = null, onStopAgent = {})
        } }
        compose.runOnIdle { }
        val pixels = captureComposerPixels()
        assertPixelClose(pixels.fill,
            over(ViewerColors.surface.copy(alpha = DeskhandGlass.SurfaceAlpha), pixels.background),
            0.12f, "composer fill should be surface at 0.60f: ")
        if (pixels.background.alpha < 0.05f) {
            assertEquals(0.60f, pixels.fill.alpha, 0.05f)
        }
    }

    @Test
    @org.robolectric.annotation.GraphicsMode(org.robolectric.annotation.GraphicsMode.Mode.NATIVE)
    fun composerKeepsFocusBorderOnGlass() {
        // 100.043 (C2) §5.4 option (a): the composer passes its focused /
        // unfocused stroke through GlassSurface's optional border
        // parameter, so the focus signal survives the glass swap —
        // focused -> primary at full strength; focus cleared -> the glass
        // border (outline at 0.45f) over the glass fill.
        var focusManager: FocusManager? = null
        compose.setContent { DeskhandViewerTheme {
            focusManager = LocalFocusManager.current
            DesktopChatComposer("", {}, false, false, {}, {}, {},
                agentStopStatus = null, onStopAgent = {})
        } }
        compose.runOnIdle { }
        val initial = captureComposerPixels()
        val glassBorder = over(ViewerColors.outline.copy(alpha = 0.45f),
            over(ViewerColors.surface.copy(alpha = DeskhandGlass.SurfaceAlpha),
                initial.background))
        assertPixelClose(initial.border, glassBorder, 0.12f,
            "unfocused border should be the glass border: ")
        compose.onNodeWithContentDescription("Message assistant").performClick()
        compose.onNodeWithContentDescription("Message assistant").assertIsFocused()
        compose.runOnIdle { }
        assertPixelClose(captureComposerPixels().border, ViewerColors.primary, 0.12f,
            "focused border should be primary: ")
        compose.runOnIdle { focusManager?.clearFocus() }
        compose.runOnIdle { }
        compose.onNodeWithContentDescription("Message assistant").assertIsNotFocused()
        assertPixelClose(captureComposerPixels().border, glassBorder, 0.12f,
            "border should return to the glass border after focus is cleared: ")
    }
}
