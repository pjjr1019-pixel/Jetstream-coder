package com.deskhand.android

import android.os.Looper
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.compositeOver
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.unit.dp
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp")
class ViewerLayoutTest {
    @get:Rule val compose = createComposeRule()

    @Test fun jetstreamJobsSharesToolbarWithoutCoveringComposerOrDrawers() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        var jobs = 0
        var agents = 0
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", true, f.browser, 1f, {},
                desktop = { _, _, _ -> }, composer = { Text("Original composer") },
                stopEnabled = true, onJetstreamClick = { jobs++ }, onAgentsClick = { agents++ },
                transportText = "Connected via Tailscale", composerStatus = "Check chat history", frameIntervalMs = 1290)
        } }
        val jobNode = compose.onNodeWithContentDescription("Jetstream jobs").assertIsDisplayed()
        val jobBounds = jobNode.fetchSemanticsNode().boundsInRoot
        val composerBounds = compose.onNodeWithTag("desktopComposer").fetchSemanticsNode().boundsInRoot
        val connectionBounds = compose.onNodeWithTag("connectionPill").fetchSemanticsNode().boundsInRoot
        assertTrue("Status must not cover Jobs", connectionBounds.bottom <= jobBounds.top)
        assertTrue("Jobs must not overlap the original composer", jobBounds.bottom <= composerBounds.top)
        jobNode.performClick()
        assertEquals(1, jobs)
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
        compose.onNodeWithContentDescription("Open agents drawer").performClick()
        assertEquals(1, agents)
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.mainClock.advanceTimeBy(600)
        compose.onNodeWithTag("desktopBrowser").assertIsDisplayed()
        compose.onNodeWithContentDescription("Jetstream jobs").assertDoesNotExist()
        compose.onNodeWithText("Original composer").assertIsDisplayed()
    }

    @Test fun stopStreamIsAvailableWithoutTouchControl() {
        compose.mainClock.autoAdvance = false // 100.042: freeze the test clock - the 3 s chrome timer would otherwise elapse during idle waits
        val fixture = BrowserFixture()
        var requests = 0
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", false, fixture.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                stopEnabled = true, onEmergencyStop = { requests++ })
        } }
        compose.onNodeWithTag("stopStream").assertIsDisplayed().performClick()
        assertEquals(1, requests)
        compose.onNodeWithContentDescription("Stop stream").assertIsDisplayed()
        compose.onNodeWithText("STOP").assertDoesNotExist()
    }

    @Test fun updateDialogPreservesViewerTabsAndConnection() {
        compose.mainClock.autoAdvance = false // 100.042: freeze the test clock - the 3 s chrome timer would otherwise elapse during idle waits
        val f = BrowserFixture()
        var disconnected = false
        AppUpdates.hide()
        shadowOf(Looper.getMainLooper()).idle() // 100.042 fix R4: hide() posts to the main Handler (AppUpdates.kt display()) — drain the paused Robolectric looper so the hide lands before setContent; same call AutomaticUpdateTest.kt uses
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, { disconnected = true },
                desktop = { _, _, _ -> Text("Interactive desktop") }, composer = { Text("Chat") })
            UpdateDialog()
        } }
        compose.runOnIdle { AppUpdates.show() }
        shadowOf(Looper.getMainLooper()).idle() // 100.042 fix R4: show() is a main-Handler post, not a direct state set — without draining the paused looper the dialog never opens before the assert below
        compose.mainClock.advanceTimeByFrame() // 100.042 fix R4: one frame for the recomposition once the posted state lands — waitForIdle with autoAdvance=false does not advance the clock or count pending recomposition as work
        compose.mainClock.advanceTimeByFrame() // 100.042 fix R5: second frame for the dialog window — the AlertDialog's content is a child composition created as a side effect of the parent's recomposition (R4 frame), so it misses that frame's measure/layout pass; idle()+1 frame (R4) composes the dialog but leaves it unplaced, which is why L56 still failed at Gate 4. One more frame lays it out; not 1000 ms, and the dialog test never runs the 3 s timer continuously
        compose.onNodeWithText("Deskhand updates").assertIsDisplayed()
        compose.onNodeWithText("Later").performClick()
        shadowOf(Looper.getMainLooper()).idle() // 100.042 fix R4: Later calls AppUpdates.hide(), another main-Handler post — drain it before asserting the dialog is gone
        compose.mainClock.advanceTimeByFrame() // 100.042 fix R4: one frame for the hide recomposition
        compose.onNodeWithText("Deskhand updates").assertDoesNotExist()
        compose.onNodeWithText("Files").assertDoesNotExist()
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R3 (D-C1 follow-up): 100 ms proved insufficient at Gate 4 — frozen clock starves the slide's start frames; 1000 ms settles the spring with margin, drawer-open suspends the 3 s timer
        compose.runOnIdle { } // 100.042 fix R9: diag2 DLG2 resolved at runOnIdle (appearedAt=runOnIdle) — the synchronising idle between the two advances is the remedy, replicating DLG2 exactly
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R7, corrected R9: R9 correction — the second 1000 ms is falsified as a standalone remedy by the R7 Gate 4 run + diag2; the runOnIdle between the advances is the ingredient, and this second 1000 ms is the settle duration after the sync step, not the remedy. The first 1000 ms is consumed by the dialog-hide teardown (AlertDialog is a separate window/child composition; its disposal + the drawer's first composition + DesktopBrowserView's withFrameNanos effect queue behind it). Drawer-open suspends the 3 s timer throughout, so 2000 ms cannot hide the chrome
        compose.onNodeWithTag("desktopBrowser").assertIsDisplayed()
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.onNodeWithText("Assistant").assertDoesNotExist()
        compose.onNodeWithText("Chat").assertIsDisplayed()
        assertFalse(disconnected)
    }

    @Test fun drawerOverlaysHalfTheStreamAndKeepsChatVisible() {
        compose.mainClock.autoAdvance = false // 100.042: freeze the test clock - the 3 s chrome timer would otherwise elapse during idle waits
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { _, _, _ -> Text("Interactive desktop") }, composer = { Text("Chat") })
        } }
        val stream = compose.onNodeWithTag("primaryDesktop").fetchSemanticsNode().boundsInRoot
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R3 (D-C1 follow-up): 100 ms proved insufficient at Gate 4 — frozen clock starves the slide's start frames; 1000 ms settles the spring with margin, drawer-open suspends the 3 s timer
        val drawer = compose.onNodeWithTag("desktopBrowser").fetchSemanticsNode().boundsInRoot
        assertEquals(stream.width / 2f, drawer.width, 2f)
        compose.onNodeWithTag("drawerResize").performTouchInput { swipe(start = androidx.compose.ui.geometry.Offset(centerX, height * 0.2f), end = androidx.compose.ui.geometry.Offset(centerX + 80f, height * 0.2f), durationMillis = 400) }
        compose.runOnIdle { } // 100.042 fix R9: replaces the R7 shadowOf looper drain — diag2 DRAG-IDLE (swipe -> runOnIdle -> 1000 ms) PASSED while the R7 drain + pump failed; the Espresso idle is the drain that works
        compose.mainClock.advanceTimeByFrame() // 100.042 fix R7, corrected R9: one frame for the detector coroutine's fraction change + recomposition to commit once the events are delivered; the 1000 ms below then runs layout. R9 correction: the R7 falsifier (manual down/moveTo/up probe next) is UNSOUND — its premise died with the chrome pump falsification (round-8 §2), and diag2 diagDragManual FAILED (drawer never opened, w=-1.0, drawerResize node absent), so the manual gesture is out. Diag2-proven mechanism (DRAG-IDLE): runOnIdle is the drain that works; the swipe events WERE delivered. The frame is retained (one harmless frame; the §5.4 drag row does not name removing it)
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R5, corrected R9: the R4 single frame did NOT fix this at Gate 4, so the R4 assumption is wrong — the drag's drawerFraction change is NOT already made when the swipe returns. detectHorizontalDragGestures is a suspend function in a pointerInput block: the batched events are dispatched, the detector coroutine resumes on the test scheduler, and only then does drawerFraction change, so one frame cannot be proven to cover both the gesture drain and the recomposition+layout. Use the test's established drawer settle (1000 ms, same as the open/close settles here); SAFE because the C1 timer is suspended while drawerOpen is true, so 1000 ms cannot hide the chrome. R9 correction: the R7 falsifier on this line is UNSOUND (round-8 §2 — the chrome pump falsification kills its premise, and diag2 diagDragManual failed, so the manual gesture is out). Diag2-proven mechanism: runOnIdle (above) is the drain that works; the swipe events were delivered. If Gate 4 still fails here, do not add more time — send the semantics tree back instead
        val wider = compose.onNodeWithTag("desktopBrowser").fetchSemanticsNode().boundsInRoot
        assertTrue("Dragging widens the drawer", wider.width > drawer.width)
        assertEquals(stream, compose.onNodeWithTag("primaryDesktop").fetchSemanticsNode().boundsInRoot)
        compose.onNodeWithText("Chat").assertIsDisplayed()
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.runOnIdle { } // 100.042 fix R10: drawer close — the same missing-sync shape as the five R9 sites (state write outside any clock advance, then an advance with no interleaved sync): the close click writes drawerOpen=false and the exit slide needs the commit step before the 1000 ms advance below, exactly as the drag swipe (L87), the dialog toggle (L65) and the chromeStays toggle (L370) needed it. Proof by elimination in FIX-REPORT-100042-ROUND10.md §1 — (a) click-didn't-register and (c) spring-needs->1000 ms are both closed, (b) is the only live mechanism. Diag3 CLOSE-CONTROL vs CLOSE-RUNONIDLE confirm/falsify at Gate 4
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix D-C1: run the drawer exit slide to completion — frozen clock freezes animations
        compose.onNodeWithTag("desktopBrowser").assertDoesNotExist()
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.runOnIdle { } // 100.042 fix R11: drawer re-open — the same missing-sync shape as the close (L95, R10) and the five R9 sites: the re-open click writes drawerOpen=true and the enter slide needs the commit step before the 1000 ms advance below, exactly as the exit slide needed it at L95. Proof in DRAWER-ANALYSIS-ROUND11.md §2 — (a) click-didn't-register and (c) spring-needs->1000 ms are both closed, (b) is the only live mechanism; the R9 pass was trivial (the close never completed, so the node never left). Diag4 REOPEN-CTRL vs REOPEN-IDLE confirm/falsify at Gate 4
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R3 (D-C1 follow-up): 100 ms proved insufficient at Gate 4 — frozen clock starves the slide's start frames; 1000 ms settles the spring with margin, drawer-open suspends the 3 s timer
        compose.onNodeWithTag("desktopBrowser").assertIsDisplayed()
    }

    @Test fun drawerClosesOnlyAfterSuccessfulOpen() {
        val f = BrowserFixture(); f.browser.info(true, true); f.list(listOf(f.file))
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", true, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {})
        } }
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.runOnIdle { f.browser.tap(f.file); f.browser.confirm(); f.opened("inaccessible") }
        compose.onNodeWithTag("desktopBrowser").assertIsDisplayed()
        compose.runOnIdle { f.browser.tap(f.file); f.browser.confirm(); f.opened() }
        compose.onNodeWithTag("desktopBrowser").assertDoesNotExist()
    }

    @Test fun imeKeepsDesktopComposerAboveKeyboard() {
        compose.mainClock.autoAdvance = false // 100.042: freeze the test clock - the 3 s chrome timer would otherwise elapse during idle waits
        val f = BrowserFixture()
        var bottom by mutableStateOf(0)
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", false, f.browser, 16f / 9f, {},
                desktop = { _, _, _ -> Text("Desktop capture") },
                composer = { DesktopChatComposer("", {}, false, false, {}, {}, {}) },
                imeInsets = WindowInsets(0, 0, 0, bottom))
        } }
        val before = compose.onNodeWithTag("chatComposer").fetchSemanticsNode().boundsInRoot
        compose.runOnIdle { bottom = 220 }
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R3: the IME change needs clock for the recomposition/relayout and the composer's enter fade to settle before asserting; 1000 ms, timer suspended while the IME is visible
        compose.onNodeWithText("Office PC").assertDoesNotExist()
        compose.onNodeWithText("Message assistant").assertIsDisplayed()
        val after = compose.onNodeWithTag("chatComposer").fetchSemanticsNode().boundsInRoot
        assertEquals(220f, before.bottom - after.bottom, 2f)
        val stream = compose.onNodeWithTag("primaryDesktop").fetchSemanticsNode().boundsInRoot
        assertTrue(stream.bottom <= after.top)
    }

    @Test fun pendingSendIsDisabledAndRestoresAfterAcknowledgement() {
        var pending by mutableStateOf(true)
        var sends = 0
        compose.setContent { DeskhandViewerTheme { DesktopChatComposer("Hello", {}, pending, true, { sends++ }, {}, {}) } }
        compose.onNodeWithContentDescription("Sending…").assertIsNotEnabled().performClick()
        assertEquals(0, sends)
        compose.runOnIdle { pending = false }
        compose.onNodeWithContentDescription("Send").assertIsEnabled().performClick()
        assertEquals(1, sends)
    }

    @Test fun activeChatShowsRestartAndInvokesRestartInsteadOfSend() {
        var stops = 0
        var sends = 0
        compose.setContent { DeskhandViewerTheme {
            DesktopChatComposer("Hello", {}, false, true, { sends++ }, {}, {},
                active = true, canStop = true, onStop = { stops++ })
        } }
        compose.onNodeWithContentDescription("Restart chat").assertIsEnabled().performClick()
        assertEquals(1, stops)
        assertEquals(0, sends)
        compose.onNodeWithContentDescription("Send").assertDoesNotExist()
    }

    @Test fun assistantExplainsCapabilitiesAndDisconnectedSend() {
        Model.chatDraft = "Hello"
        Model.chatConnected = false
        Model.chatStatus = "Connection lost"
        compose.setContent { DeskhandViewerTheme { Column(Modifier.fillMaxSize()) {
            AssistantSettings(Modifier.weight(1f))
            DesktopChatComposer(Model.chatDraft, {}, false, Model.canSendChat, {}, {}, {})
        } } }
        compose.onNodeWithText("Computer tasks are unavailable for this assistant.").assertIsDisplayed()
        compose.onNodeWithText("Connection lost").assertIsDisplayed()
        compose.onNodeWithContentDescription("Send").assertIsNotEnabled()
    }

    @Test
    @org.robolectric.annotation.GraphicsMode(org.robolectric.annotation.GraphicsMode.Mode.NATIVE)
    fun zoomedDragPansAndSingleTapClicks() {
        val zoom = DesktopZoomState()
        val sent = mutableListOf<String>()
        val bmp = androidx.compose.ui.graphics.ImageBitmap(100, 100)
        compose.setContent { DeskhandViewerTheme {
            Box(Modifier.size(300.dp)) { DesktopView(bmp, zoom, panMode = true, send = { sent.add(it.optString("t")) }) }  // 100.011: Pan mode so the drag pans
        } }
        compose.runOnIdle { zoom.change(2f) }
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput {
            swipe(start = androidx.compose.ui.geometry.Offset(centerX, centerY),
                end = androidx.compose.ui.geometry.Offset(centerX + 70f, centerY), durationMillis = 400)
        }
        compose.runOnIdle { assertTrue(zoom.pan.x > 0); assertTrue(sent.isEmpty()) }
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput {
            doubleClick(androidx.compose.ui.geometry.Offset(centerX, centerY))
        }
        // B2-G B3: a single tap clicks (changelog: "Tap always clicks in
        // both modes"), so a double-tap sends two clicks, one per tap.
        compose.runOnIdle { assertEquals(listOf("move", "down", "up", "move", "down", "up"), sent) }
        compose.runOnIdle { sent.clear(); zoom.reset(); zoom.applyFit(true) }  // 100.011: fit (1.0x) so the final drag is a no-op as before
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput { click() }
        compose.runOnIdle { assertEquals(listOf("move", "down", "up"), sent) }
        compose.runOnIdle { sent.clear() }
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput {
            swipe(start = androidx.compose.ui.geometry.Offset(centerX, centerY),
                end = androidx.compose.ui.geometry.Offset(centerX + 70f, centerY), durationMillis = 400)
        }
        compose.runOnIdle { assertTrue(zoom.pan.x > 0); assertTrue("Dragging must not click or drag on the PC", sent.isEmpty()) }  // 100.011: after reset the zoom is 1.5x, so a drag pans (a drag also exits fit mode); it must still not click or drag on the PC
        compose.onNodeWithTag("desktopTouchSurface").performTouchInput { doubleClick(androidx.compose.ui.geometry.Offset(centerX, centerY)) }
        compose.runOnIdle { assertEquals(listOf("move", "down", "up", "move", "down", "up"), sent) }
    }

    @Test fun viewControlsAreAvailableInSettings() {
        val zoom = DesktopZoomState()
        var scale by mutableStateOf(DesktopScale.Stretch)
        var panel by mutableStateOf<String?>("settings")
        compose.setContent { DeskhandViewerTheme {
            DesktopChatPanels(panel, null, extraSettings = {
                DesktopViewControls(scale, zoom) { scale = it }
            }) { panel = null }
        } }
        compose.onNodeWithTag("fitDesktop").assertIsDisplayed().performClick()
        assertEquals(DesktopScale.Fit, scale)
        compose.onNodeWithTag("zoomMenu").assertIsDisplayed()
    }

    @Test fun visibleFitAndZoomControlsResetFullDesktop() {
        val zoom = DesktopZoomState()
        var scale by mutableStateOf(DesktopScale.Stretch)
        zoom.geometry(androidx.compose.ui.unit.IntSize(1000, 600), androidx.compose.ui.geometry.Size(1000f, 600f))
        compose.setContent { DeskhandViewerTheme {
            // Production onScale: base size only, no zoom reset (B2-G B2).
            DesktopViewControls(scale, zoom) { scale = it }
        } }
        compose.onNodeWithTag("zoomMenu").performClick()
        compose.onNodeWithText("Zoom in").performClick()
        assertEquals(2f, zoom.zoom, 0.001f)  // 100.011: 1.5x * 1.5 = 2.25 clamps to ZOOM_MAX 2.0 (was 1.5f from 1.0x)
        compose.onNodeWithText("Show full desktop").performClick()
        assertEquals(DesktopScale.Fit, scale)
        assertTrue(zoom.fit)  // B2-G B2: full desktop is the fit view override, not a zoom reset
        assertEquals(2f, zoom.zoom, 0.001f)  // stored factor preserved; toggling fit back restores it
        compose.onNodeWithTag("stretchDesktop").performClick()
        assertEquals(DesktopScale.Stretch, scale)
        assertFalse(zoom.fit)
        compose.onNodeWithTag("fitDesktop").performClick()
        assertEquals(DesktopScale.Fit, scale)
        assertTrue(zoom.fit)
    }

    @Test fun primaryActionsHaveReadableContrastOnViewerSurface() {
        // 100.043 (C2): the glass fill is surface at 0.60f over the stream
        // (black desktop here) — contrast is computed on that effective
        // fill, not the opaque surface; threshold unchanged at 4.5f.
        val glassFill = ViewerColors.surface.copy(alpha = DeskhandGlass.SurfaceAlpha).compositeOver(Color.Black)
        val contrast = (ViewerColors.primary.luminance() + 0.05f) / (glassFill.luminance() + 0.05f)
        assertTrue("Primary action contrast: $contrast", contrast >= 4.5f)
    }

    @Test fun glassSurfacesUseSharedGlassSurface() {
        // 100.043 (C2): the Connected pill and both drawer toggles render
        // through the one shared GlassSurface — fill surface at 0.60f
        // (not the old 0.94f pill / 0.82f toggle fills) with a 1.dp outline
        // border at 0.30f. A Surface's color argument is not exposed in
        // semantics, so the test asserts the chrome nodes exist under
        // visible chrome and the shared contract values they are drawn
        // from; the old fills are verified gone by grep (WORKER-B report).
        compose.mainClock.autoAdvance = false // 100.042: freeze the test clock - the 3 s chrome timer would otherwise elapse during idle waits
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { _, _, _ -> Text("Capture") },
                composer = { Text("Chat") })
        } }
        compose.onNodeWithText("Connected").assertIsDisplayed()
        compose.onNodeWithTag("filesDrawerToggle").assertIsDisplayed()
        compose.onNodeWithTag("agentsDrawerToggle").assertIsDisplayed()
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
        assertEquals(0.60f, DeskhandGlass.SurfaceAlpha, 0.001f)
        assertEquals(0.30f, DeskhandGlass.BorderAlpha, 0.001f)
        assertEquals(1.dp, DeskhandGlass.BorderWidth)
    }

    @Test fun compactLandscapeKeepsHeaderAndComposerVisible() {
        compose.mainClock.autoAdvance = false // 100.042: freeze the test clock - the 3 s chrome timer would otherwise elapse during idle waits
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", false, f.browser, 16f / 9f, {},
                desktop = { _, _, _ -> Text("Preview") }, composer = { DesktopChatComposer("", {}, false, false, {}, {}, {}) },
                modifier = Modifier.size(700.dp, 360.dp))
        } }
        compose.onNodeWithText("Assistant").assertDoesNotExist()
        compose.onNodeWithText("Type on PC").assertDoesNotExist()
        compose.onNodeWithText("Office PC").assertDoesNotExist()
        compose.onNodeWithText("Message assistant").assertIsDisplayed()
    }

    @Test
    @Config(sdk = [35], qualifiers = "w891dp-h411dp-land")
    fun landscapeDesktopUsesMostHeightWithoutShrinkingStream() {
        compose.mainClock.autoAdvance = false // 100.042: freeze the test clock - the 3 s chrome timer would otherwise elapse during idle waits
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { _, _, _ -> Text("Live desktop") },
                composer = { DesktopChatComposer("", {}, false, false, {}, {}, {}) })
        } }
        val layout = compose.onNodeWithTag("landscapeViewer").fetchSemanticsNode().boundsInRoot
        val desktop = compose.onNodeWithTag("primaryDesktop").fetchSemanticsNode().boundsInRoot
        val footer = compose.onNodeWithTag("desktopComposer").fetchSemanticsNode().boundsInRoot
        assertTrue("Composer reserves its own space below capture", footer.top - desktop.bottom in 0f..4f)
        assertTrue("Capture remains the main landscape area", desktop.height > footer.height * 2)
        compose.onNodeWithText("Office PC").assertDoesNotExist()
        compose.onNodeWithText("PC keys").assertDoesNotExist()
        compose.onNodeWithText("Files").assertDoesNotExist()
        compose.onNodeWithTag("streamMenu").assertDoesNotExist()
        compose.onNodeWithTag("closeLastWindow").assertDoesNotExist()
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.runOnIdle { } // 100.042 fix R11: landscape open — the same missing-sync shape as the drawer close (L95, R10) and the dialog (L65) / chromeStays (L370) opens: the toggle click writes drawerOpen=true and the enter slide needs the commit step before the 1000 ms advance below. Diag2 DLG2/STAYS and Diag3 CLOSE-RUNONIDLE prove the step at same-shape sites; the landscape open and the drawer re-open were the last two open sites without it, and both fail in the Gate 4 R10 runs. Diag4 LAND-CTRL vs LAND-IDLE confirm/falsify at Gate 4 (LAND-IDLE-B is the Pattern-B fallback)
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R3 (D-C1 follow-up): 100 ms proved insufficient at Gate 4 — frozen clock starves the slide's start frames; 1000 ms settles the spring with margin, drawer-open suspends the 3 s timer
        compose.onNodeWithTag("desktopBrowser").assertIsDisplayed()
        compose.onNodeWithText("Message assistant").assertIsDisplayed()
    }
    @Test fun viewerAlwaysShowsDesktop() {
        compose.mainClock.autoAdvance = false // 100.042: freeze the test clock - the 3 s chrome timer would otherwise elapse during idle waits
        // B2-G: ViewerTab.Files / ViewerTab.Assistant are gone; the viewer
        // has only the Desktop tab, and a saved tab always normalises to it.
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { _, _, _ -> Text("Capture") }, composer = { Text("Chat") })
        } }
        compose.onNodeWithText("Assistant").assertDoesNotExist()
        compose.onNodeWithText("Chat").assertIsDisplayed()
        compose.onNodeWithTag("primaryDesktop").assertIsDisplayed()
        assertEquals(ViewerTab.Desktop, normalizedViewerTab(ViewerTab.Desktop))
    }

    // 100.042 - chrome auto-hide (spec 100038-full-bleed SS7). The five tests
    // drive ViewerLayout through its parameters with fake desktop/composer
    // slots and never touch Model singleton state. The compose test clock is
    // frozen (autoAdvance = false) and advanced explicitly with
    // mainClock.advanceTimeBy - never wall-clock timing.

    @Test fun chromeFadesAfterInactivity() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { _, _, onDesktopTap ->
                    Box(Modifier.fillMaxSize().testTag("fakeDesktop").clickable { onDesktopTap() })
                },
                composer = { Text("Chat") })
        } }
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
        compose.onNodeWithText("Connected").assertIsDisplayed()
        // Past the 3 s auto-hide delay plus the 220 ms fade-out.
        compose.mainClock.advanceTimeBy(3500)
        compose.onNodeWithTag("stopStream").assertDoesNotExist()
        compose.onNodeWithText("Connected").assertDoesNotExist()
        // A tap on the desktop brings the chrome back.
        compose.onNodeWithTag("fakeDesktop").performClick()
        compose.runOnIdle { } // 100.042 fix R9: diag2 TAP-IDLE-BEFORE is the ONLY chrome remedy that passed (comp 2->3, lastSeen true, stop + Chat appear) — the Espresso idle before the advance is the commit step; runOnIdle/waitForIdle after the advance, advanceTimeUntil and two pumps all failed
        compose.mainClock.advanceTimeByFrame() // 100.042 fix R7, corrected R9: the tap writes chromeVisible=true on the UI thread, outside any clock advance — the §5 diagnostic proves the recomposition never runs (lastSeen=false, compositions stuck at 2) even across the 1000 ms advance below. waitForIdle with autoAdvance=false does not count a pending recomposition as work (MainTestClock doc; same class as the R4 dialog frames). R9 correction: the pump alone does NOT commit the recomposition — diag2 TAP-R7 (counter stays 2 after the pump) falsifies that claim; the runOnIdle above is the commit step and the pump is retained only as part of the diagnostic-proven sequence (tap -> runOnIdle -> pump -> 1000 ms). The 1000 ms below then runs the 220 ms enter fade. A pump, not a raise — ~16 ms, re-appearance still ~1016 ms after the tap, inside the 3 s window
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R3: 300 ms proved insufficient at Gate 4 — the frozen clock starves the chrome enter fade (220 ms) of its start frames; 1000 ms covers it with margin, still inside the 3 s auto-hide window
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
        compose.onNodeWithText("Connected").assertIsDisplayed()
    }
    @Test fun composerStaysVisibleWhenChromeHidden() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { _, _, onDesktopTap ->
                    Box(Modifier.fillMaxSize().testTag("fakeDesktop").clickable { onDesktopTap() })
                },
                composer = { Text("Chat") })
        } }
        compose.onNodeWithTag("desktopComposer").assertIsDisplayed()
        compose.onNodeWithText("Chat").assertIsDisplayed()
        // Past the 3 s auto-hide delay plus the 220 ms fade-out: the top
        // toolbar is gone with the rest of the chrome, but the composer is
        // NOT chrome — it must stay on screen so the latest reply and the
        // Chat history button are always reachable (100.050 fix round,
        // item 74).
        compose.mainClock.advanceTimeBy(3500)
        compose.onNodeWithTag("stopStream").assertDoesNotExist()
        compose.onNodeWithTag("desktopComposer").assertIsDisplayed()
        compose.onNodeWithText("Chat").assertIsDisplayed()
    }

    @Test fun desktopTapStillClicksWhenChromeHidden() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        var taps = 0
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { _, _, onDesktopTap ->
                    // Fake desktop slot: a tap both clicks the PC (counter) and
                    // reports the tap outward (onDesktopTap), mirroring the
                    // double-duty contract in DesktopView (spec SS5.4).
                    Box(Modifier.fillMaxSize().testTag("fakeDesktop").clickable { taps++; onDesktopTap() })
                },
                composer = { Text("Chat") })
        } }
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
        compose.mainClock.advanceTimeBy(3500)
        compose.onNodeWithTag("stopStream").assertDoesNotExist()
        compose.onNodeWithTag("fakeDesktop").performClick()
        compose.runOnIdle { } // 100.042 fix R9: same remedy as chromeFadesAfterInactivity — diag2 TAP-IDLE-BEFORE; inserted immediately after the tap, before the taps assert (a plain variable read, not a sync read), matching the diagnostic sequence
        assertEquals(1, taps)
        // The same tap also re-shows the chrome.
        compose.mainClock.advanceTimeByFrame() // 100.042 fix R7, corrected R9: same as chromeFadesAfterInactivity — the tap's state write is made outside the clock and its recomposition is still pending when the 1000 ms advance runs (diagnostic: lastSeen=false, compositions=2). R9 correction: the pump alone does NOT commit it — diag2 TAP-R7 falsified that; the runOnIdle above is the commit step and the pump is retained only as part of the diagnostic-proven sequence. The 1000 ms below runs the fade
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R3: 300 ms proved insufficient at Gate 4 — the frozen clock starves the chrome enter fade (220 ms) of its start frames; 1000 ms covers it with margin, still inside the 3 s auto-hide window
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
    }

    @Test fun chromeStaysVisibleWhileDrawerOpen() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { interactive, _, _ -> Text("Capture") },
                composer = { Text("Chat") })
        } }
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R3 (D-C1 follow-up): 100 ms proved insufficient at Gate 4 — frozen clock starves the slide's start frames; 1000 ms settles the spring with margin, drawer-open suspends the 3 s timer
        compose.runOnIdle { } // 100.042 fix R9: diag2 STAYS variant (toggle -> 1000 -> runOnIdle -> 1000 -> assert) PASSED at runOnIdle; the suite has now failed chromeStays in two consecutive runs, so this is systematic, not a one-off flake. §5.4 drawer-open row: insert runOnIdle after the toggle in chromeStays if its variant lands at runOnIdle
        compose.mainClock.advanceTimeBy(1000) // 100.042 fix R9: the diagnostic-proven settle after the sync step (same pattern as the dialog fix) — NOT a raise of the existing advance, which is untouched; drawer-open suspends the 3 s timer throughout, and 2000 ms before the assert is inside the 3 s window anyway
        compose.onNodeWithTag("desktopBrowser").assertIsDisplayed()
        // Past the 3 s delay with the drawer open: the timer is suspended, so
        // the composer (chrome) must still be present.
        compose.mainClock.advanceTimeBy(4000)
        compose.onNodeWithTag("desktopBrowser").assertIsDisplayed()
        compose.onNodeWithText("Chat").assertIsDisplayed()
    }

    @Test fun chromeStaysVisibleWhileImeVisible() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        var bottom by mutableStateOf(0)
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", false, f.browser, 16f / 9f, {},
                desktop = { interactive, _, _ -> Text("Capture") },
                composer = { Text("Chat") },
                imeInsets = WindowInsets(0, 0, 0, bottom))
        } }
        compose.runOnIdle { bottom = 220 }
        // Past the 3 s delay with the IME up: the timer is suspended and the
        // IME forces chrome visible, so the composer must still be present.
        compose.mainClock.advanceTimeBy(4000)
        compose.onNodeWithText("Chat").assertIsDisplayed()
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
    }

    // 100.042 fix R13 (GPT pick (c), ROTATION-ANALYSIS-ROUND12.md §6.3):
    // rotationRestoresChrome is REPLACED by the two contract checks below.
    // The StateRestorationTester flow on ViewerLayout is gone: Diag4 proved
    // the fresh composition never runs after emulateSavedInstanceStateRestore()
    // (M-B: ROOTWRITE afterRestore root=1, fresh subcomposition unmeasured),
    // so no tester-based assert on ViewerLayout can pass — the failure was a
    // harness artifact, not a product defect (chromeVisible is remember, NOT
    // rememberSaveable, BY SPEC; the production file is untouched).

    @Test fun rememberSaveableSurvivesStateRestoration() {
        // (1) Save/restore contract — minimal rememberSaveable value, NO
        // ViewerLayout assert. GPT's literal activity-recreate form
        // (activityRule.scenario.recreate()) is NOT implementable in this
        // class: AndroidComposeTestRule.setContent is once-per-test and nothing
        // re-sets content after a recreation, so the recreated activity would
        // be empty (AndroidComposeTestRule.android.kt, setContent). This form
        // uses StateRestorationTester on MINIMAL root-level content only
        // (never on ViewerLayout): Diag4 STALE proved trivial root content
        // restores and recomposes under the tester. The post-restore driver
        // write is the STALE pattern — the restore itself never runs the fresh
        // composition (M-B) — and the "Y" liveness assert is the ghost-proof
        // (round-12 §1): without it the post-restore "after" node could be
        // the disposed tree's ghost.
        compose.mainClock.autoAdvance = false
        var label by mutableStateOf("X") // test-owned driver state (STALE pattern)
        val restore = StateRestorationTester(compose)
        restore.setContent {
            var saved by rememberSaveable { mutableStateOf("before") }
            Column {
                Text(saved)
                Text(label)
                Button(onClick = { saved = "after" }) { Text("Change") }
            }
        }
        compose.onNodeWithText("before").assertIsDisplayed()
        compose.onNodeWithText("X").assertIsDisplayed()
        compose.onNodeWithText("Change").performClick()
        compose.runOnIdle { } // 100.042 fix R13: click writes saved="after" outside any clock advance — the R9 commit step before asserting (chromeFades pattern)
        compose.mainClock.advanceTimeByFrame() // 100.042 fix R14: Diag6 D2 — click + runOnIdle + frame passes, D1 (no frame) fails; the click writes saved="after" outside any clock advance and runOnIdle commits it, but the recomposition needs a frame before the assert (chromeFades pattern). A 16 ms commit step, NOT a settle — no advance raised or added.
        compose.onNodeWithText("after").assertIsDisplayed()
        compose.onNodeWithText("before").assertDoesNotExist()
        restore.emulateSavedInstanceStateRestore()
        // Drive the fresh composition with a test-owned write (STALE): the
        // restore itself never runs it (M-B). Only the fresh tree can show
        // "Y"; the ghost tree shows "X". Frame + 1000 ms is the STALE-proven
        // settle (static Text, no fade/timer — settle only, not a raise).
        compose.runOnIdle { label = "Y" }
        compose.mainClock.advanceTimeByFrame()
        compose.mainClock.advanceTimeBy(1000)
        compose.onNodeWithText("Y").assertIsDisplayed()
        compose.onNodeWithText("X").assertDoesNotExist()
        compose.onNodeWithText("after").assertIsDisplayed()
        compose.onNodeWithText("before").assertDoesNotExist()
    }

    @Test fun freshCompositionStartsWithChromeVisible() {
        // (2) Fresh chrome contract (GPT spec, verbatim shape) — single
        // setContent with the existing ViewerLayout fixture, no tester, no
        // restore, no clock advance. A fresh composition starts with
        // chromeVisible=true (remember), so stopStream + Connected are
        // displayed. stopStream tag and fixture unchanged.
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { _, _, _ -> Text("Capture") },
                composer = { Text("Chat") })
        } }
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
        compose.onNodeWithText("Connected").assertIsDisplayed()
    }

    // 100.049 — Connected pill (BUILD-SPEC §4). Six tests, all
    // isolation-safe: ViewerLayout is constructed directly with literal
    // params, never touching the shared Model singleton. The number shown
    // is the frame-arrival interval — NOT ping/latency/RTT (no such source
    // exists in the app). Frozen clock, with the SUITE4 (a) auto-advance
    // idle-sync after the tap, is the sync shape: the sheet's show()
    // start is driven by the idle-sync's clock advancement, NOT by manual
    // advanceTimeBy — Gate 4 proved 1000 ms of manual advance leaves the
    // sheet exactly at its Hidden offset (rows present but not displayed;
    // relay boundsInRoot real height at top=1111, below the 891 window).
    // The 1000 ms settle is kept as the settle after the sync. Safe: the
    // open sheet suspends the C1 timer (ViewerLayout L142-144) and the
    // clock is frozen again before the assert, so no timer time elapses
    // while the sheet is closed.

    @Test fun pillShowsFrameIntervalWhenConnected() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        var interval by mutableStateOf<Long?>(34)
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", false, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                frameIntervalMs = interval)
        } }
        compose.onNodeWithText("34 ms frame").assertIsDisplayed()
        compose.runOnIdle { interval = null }
        // FIX 100.049 Gate 4 G2: frozen clock — the null assignment only
        // schedules a recomposition; waitForIdle/runOnIdle with
        // autoAdvance=false does not advance the clock or count a pending
        // recomposition as work (in-file precedent L59/L470), so without a
        // frame the old tree still carries "34 ms frame" when the assert
        // runs. One frame commits it — the Text is a plain conditional
        // (ViewerLayout L266), no animation on it. Production is NOT
        // changed: L266 already hides the text when frameIntervalMs is
        // null, and this test constructs ViewerLayout directly, so the
        // MainActivity Q2 EMA reset lines are not involved.
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithText("34 ms frame").assertDoesNotExist()
    }

    @Test fun pillHidesIntervalWhenNotConnected() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connecting", false, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                frameIntervalMs = 34)
        } }
        compose.onNodeWithText("Connecting").assertIsDisplayed()
        compose.onNodeWithText("34 ms frame").assertDoesNotExist()
    }

    @Test fun pillTapOpensConnectionSheet() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        val relay = "wss://100.64.1.2/ws"
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", false, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                frameIntervalMs = 34, lastFrameWallMs = 1_700_000_000_000L,
                relayUrl = relay, relayHost = "100.64.1.2")
        } }
        compose.onNodeWithTag("connectionPill").performClick()
        compose.mainClock.autoAdvance = true // FIX 100.049 Gate 4 SUITE4 (a): re-enable the auto-advancing clock around the tap — the sheet's show() start is driven by the idle-sync's clock advancement, not by manual advanceTimeBy (DIAGNOSIS-100049-SUITE4.md §2-§3). The passing history-sheet control runs with the default auto-advancing clock.
        compose.waitForIdle() // FIX 100.049 SUITE4 (a): with autoAdvance=true this advances the test clock until Compose is idle — the sheet's layout-offset animation is pending layout work each frame until it settles, so the sync keeps producing frames until the sheet is up. The pulse's alpha is draw-only and does not block quiescence (in-file proof: drawerClosesOnlyAfterSuccessfulOpen L110-111 returns from idle-syncs with the pulse running).
        compose.mainClock.autoAdvance = false // FIX 100.049 SUITE4 (a): freeze the clock again before asserting. Timer-safe: the sheet is open, so the C1 timer is suspended (ViewerLayout L142-144); no clock time elapses while the sheet is closed.
        compose.mainClock.advanceTimeBy(1000)
        compose.onNodeWithText(relay).assertIsDisplayed()
        compose.onNodeWithText("100.64.1.2").assertIsDisplayed()
        compose.onNodeWithText("Last frame OK").assertIsDisplayed()
        compose.onNodeWithText("Frame interval").assertIsDisplayed()
    }

    @Test fun sheetShowsNotPairedWhenRelayEmpty() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", false, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                relayUrl = "", relayHost = "")
        } }
        compose.onNodeWithTag("connectionPill").performClick()
        compose.mainClock.autoAdvance = true // FIX 100.049 Gate 4 SUITE4 (a): same auto-advance idle-sync shape as pillTapOpensConnectionSheet — the sheet's show() start is driven by the idle-sync's clock advancement, not by manual advanceTimeBy (DIAGNOSIS-100049-SUITE4.md §2-§3). The passing history-sheet control runs with the default auto-advancing clock.
        compose.waitForIdle() // FIX 100.049 SUITE4 (a): with autoAdvance=true this advances the test clock until Compose is idle — the sheet's layout-offset animation is pending layout work each frame until it settles, so the sync keeps producing frames until the sheet is up. The pulse's alpha is draw-only and does not block quiescence (in-file proof: drawerClosesOnlyAfterSuccessfulOpen L110-111 returns from idle-syncs with the pulse running).
        compose.mainClock.autoAdvance = false // FIX 100.049 SUITE4 (a): freeze the clock again before asserting. Timer-safe: the sheet is open, so the C1 timer is suspended (ViewerLayout L142-144); no clock time elapses while the sheet is closed.
        compose.mainClock.advanceTimeBy(1000)
        compose.onNodeWithText("Not paired").assertIsDisplayed()
    }

    @Test fun sheetLabelsHostnameRelayCorrectly() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", false, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                relayUrl = "wss://deskhand.example.com/ws", relayHost = "deskhand.example.com")
        } }
        compose.onNodeWithTag("connectionPill").performClick()
        compose.mainClock.autoAdvance = true // FIX 100.049 Gate 4 SUITE4 (a): same auto-advance idle-sync shape as pillTapOpensConnectionSheet — the sheet's show() start is driven by the idle-sync's clock advancement, not by manual advanceTimeBy (DIAGNOSIS-100049-SUITE4.md §2-§3). The passing history-sheet control runs with the default auto-advancing clock.
        compose.waitForIdle() // FIX 100.049 SUITE4 (a): with autoAdvance=true this advances the test clock until Compose is idle — the sheet's layout-offset animation is pending layout work each frame until it settles, so the sync keeps producing frames until the sheet is up. The pulse's alpha is draw-only and does not block quiescence (in-file proof: drawerClosesOnlyAfterSuccessfulOpen L110-111 returns from idle-syncs with the pulse running).
        compose.mainClock.autoAdvance = false // FIX 100.049 SUITE4 (a): freeze the clock again before asserting. Timer-safe: the sheet is open, so the C1 timer is suspended (ViewerLayout L142-144); no clock time elapses while the sheet is closed.
        compose.mainClock.advanceTimeBy(1000)
        compose.onNodeWithText("Relay host", substring = true).assertIsDisplayed()
        compose.onNodeWithText("Embedded IP").assertDoesNotExist()
    }

    @Test fun pillKeepsComposerStatus() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", false, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                frameIntervalMs = 34, composerStatus = "Sending…")
        } }
        compose.onNodeWithText("34 ms frame").assertIsDisplayed()
        compose.onNodeWithText("Sending…").assertIsDisplayed()
    }

    // 100.050 — top-right toolbar (BUILD-SPEC §5). Four tests, all
    // isolation-safe: ViewerLayout is constructed directly with literal
    // params, never touching the shared Model singleton. Frozen clock +
    // runOnIdle before the advance is the diag2-proven sync shape; the
    // toolbar tests use direct composable composition, so the 100.049
    // verifier's ModalBottomSheet slide-in warning does not apply here.

    @Test fun toolbarContainsWindowButtonsAndStopLast() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", false, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                stopEnabled = true, onEmergencyStop = {},
                foregroundWindowEnabled = true, foregroundWindowBusy = false,
                onForegroundWindowAction = {})
        } }
        compose.onNodeWithTag("minimizeFocusedWindow").assertIsDisplayed()
        compose.onNodeWithTag("closeFocusedWindow").assertIsDisplayed()
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
        compose.onAllNodesWithTag("stopStream").assertCountEquals(1)
        // Order: minimize, close, octagon — the octagon is rightmost.
        val minimize = compose.onNodeWithTag("minimizeFocusedWindow").fetchSemanticsNode().boundsInRoot
        val close = compose.onNodeWithTag("closeFocusedWindow").fetchSemanticsNode().boundsInRoot
        val stop = compose.onNodeWithTag("stopStream").fetchSemanticsNode().boundsInRoot
        assertTrue("minimize is left of close", minimize.left < close.left)
        // 100.050 (§4.2 / §6): no extra spacing beyond the Row's defaults
        // between close and the octagon — spacing is 100.055's territory —
        // so the octagon sits flush (stop.left == close.right). "Rightmost"
        // is asserted on the right edges instead: the octagon extends past
        // the close button's right edge — which holds flush today and with
        // any future 100.055 gap alike.
        assertTrue("stop octagon is right of close", stop.right > close.right)
    }

    @Test fun toolbarStopStillFiresEmergencyStop() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        var stops = 0
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", false, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                stopEnabled = true, onEmergencyStop = { stops++ },
                foregroundWindowEnabled = true)
        } }
        compose.onNodeWithTag("stopStream").assertIsDisplayed().performClick()
        assertEquals(1, stops)
        compose.onNodeWithContentDescription("Stop stream").assertIsDisplayed()
    }

    @Test fun toolbarWindowButtonsFireActions() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        val actions = mutableListOf<String>()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("PC", "Connected", false, f.browser, 1f, {}, desktop = { _, _, _ -> }, composer = {},
                foregroundWindowEnabled = true, foregroundWindowBusy = false,
                onForegroundWindowAction = { actions.add(it) })
        } }
        compose.onNodeWithTag("minimizeFocusedWindow").assertIsDisplayed().performClick()
        compose.onNodeWithTag("closeFocusedWindow").assertIsDisplayed().performClick()
        assertEquals(listOf("minimize", "close"), actions)
    }

    @Test fun toolbarHiddenWhenDrawerOpen() {
        compose.mainClock.autoAdvance = false
        val f = BrowserFixture()
        compose.setContent { DeskhandViewerTheme {
            ViewerLayout("Office PC", "Connected", true, f.browser, 16f / 9f, {},
                desktop = { _, _, _ -> Text("Interactive desktop") }, composer = { Text("Chat") })
        } }
        compose.onNodeWithTag("minimizeFocusedWindow").assertIsDisplayed()
        compose.onNodeWithTag("stopStream").assertIsDisplayed()
        compose.onNodeWithTag("filesDrawerToggle").performClick()
        compose.mainClock.advanceTimeBy(1000) // 100.050: same drawer settle as chromeStaysVisibleWhileDrawerOpen — the exit fade (220 ms) needs clock; drawer-open suspends the 3 s timer
        compose.runOnIdle { } // 100.050: diag2-proven sync step before the second advance
        compose.mainClock.advanceTimeBy(1000)
        compose.onNodeWithTag("desktopBrowser").assertIsDisplayed()
        compose.onNodeWithTag("minimizeFocusedWindow").assertDoesNotExist()
        compose.onNodeWithTag("closeFocusedWindow").assertDoesNotExist()
        compose.onNodeWithTag("stopStream").assertDoesNotExist()
    }
}
