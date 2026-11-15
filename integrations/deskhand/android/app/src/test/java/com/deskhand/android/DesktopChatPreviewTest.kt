package com.deskhand.android

import android.graphics.Bitmap
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.Text
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.createComposeRule
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class DesktopChatPreviewTest {
    @get:Rule val compose = createComposeRule()
    @Test fun fixturePreviewAndSavedStateRestoreKeepBubbleDeadline() {
        compose.mainClock.autoAdvance = false
        var time = 0L
        val chat = DesktopChatState { time }
        chat.snapshot(emptyList())
        chat.submitted(JSONObject().put("id", "fixture").put("text", "Summarize what is on my desktop."))
        chat.snapshot(listOf(JSONObject().put("id", "fixture").put("text", "Summarize what is on my desktop.")
            .put("assistant", "Demo AI").put("model", "fixture-model").put("status", "completed")
            .put("reply", "This is a fixture preview. Replies rise over the capture and remain in History.")))
        time = 300
        val restore = StateRestorationTester(compose)
        val f = BrowserFixture()
        restore.setContent { DeskhandViewerTheme {
            ViewerLayout("Preview PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { interactive, _, _ ->
                    if (interactive) DesktopStreamOverlay(null,
                        bubbles = { ChatBubbleLane(chat, it) {} }, content = {
                            Box(Modifier.fillMaxSize().background(Color(0xFF14394A)), Alignment.Center) { Text("Desktop capture fixture") }
                        })
                },
                composer = { DesktopChatComposer("", {}, false, false, {}, {}, {}) })
        } }
        compose.onNodeWithTag("bubble-fixture/assistant").assertIsDisplayed()
        val preview = File("build/reports/desktop-chat/portrait-fixture.png")
        preview.parentFile?.mkdirs()
        preview.outputStream().use { compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it) }
        time = 1000
        restore.emulateSavedInstanceStateRestore()
        compose.mainClock.advanceTimeByFrame()
        assertTrue(chat.bubbles.all { it.born == 0L })
        compose.onNodeWithTag("bubble-fixture/assistant").assertIsDisplayed()
    }
}
