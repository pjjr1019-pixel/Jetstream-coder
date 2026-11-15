package com.deskhand.android

import android.content.Intent
import android.os.Looper
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows
import org.robolectric.annotation.Config
import java.util.concurrent.TimeUnit

/**
 * 100.026 foreground-service build (spec §3, §4): unit tests for the pure
 * elapsed-time formatter, the 60s self-stop constant, the Disconnect
 * intent-extra key, and (Robolectric) the Disconnect notification action
 * and the 60s self-stop scheduling.
 *
 * WRITTEN on the VM, NOT RUN here — no Android SDK / Gradle / Java on the
 * VM. The PC runs them at Gate 4 (see REPORT-B.md). What this file does
 * NOT prove (needs the phone, spec §8.3): the wake lock actually keeping
 * the stream alive while dozing (T4/T5), the lock release visible in
 * `dumpsys power` after disconnect (T6) or after a sticky restart (T7/T11),
 * and the Disconnect tap ending the real session end-to-end (T9).
 *
 * Harness note: pure-function tests are plain JUnit (the ZoomMathTest /
 * CryptoTest pattern — no runner). Framework tests use Robolectric
 * 4.16.1 + JUnit 4.13.2, the established pattern in this tree
 * (DiagnosticsScreenTest: @RunWith(RobolectricTestRunner::class),
 * @Config(sdk = [35])). No Compose test rule is involved, so the 100.025
 * fixed Compose patterns (onAllNodes + assertCountEquals(0), one
 * setContent + mutableStateOf) do not apply here.
 */
class DeskhandSessionServiceTest {

    @Test
    fun formatSessionElapsed_underOneMinute_hasNoSuffix() {
        assertEquals("", formatSessionElapsed(0L))
        assertEquals("", formatSessionElapsed(1_000L))
        assertEquals("", formatSessionElapsed(59_999L))
        // Negative input (clock skew) never shows a suffix either.
        assertEquals("", formatSessionElapsed(-60_000L))
    }

    @Test
    fun formatSessionElapsed_oneMinute() {
        assertEquals("· 1m", formatSessionElapsed(60_000L))
        assertEquals("· 1m", formatSessionElapsed(119_999L))
    }

    @Test
    fun formatSessionElapsed_twentyThreeMinutes() {
        assertEquals("· 23m", formatSessionElapsed(23L * 60_000L))
    }

    @Test
    fun formatSessionElapsed_fiftyNineMinutes() {
        assertEquals("· 59m", formatSessionElapsed(59L * 60_000L))
    }

    @Test
    fun formatSessionElapsed_sixtyMinutes_showsHours() {
        assertEquals("· 1h 0m", formatSessionElapsed(60L * 60_000L))
    }

    @Test
    fun formatSessionElapsed_seventyTwoMinutes() {
        assertEquals("· 1h 12m", formatSessionElapsed(72L * 60_000L))
    }

    @Test
    fun formatSessionElapsed_twoHours() {
        assertEquals("· 2h 0m", formatSessionElapsed(120L * 60_000L))
    }

    @Test
    fun selfStopDelay_isSixtySeconds() {
        assertEquals(60_000L, DeskhandSessionService.SELF_STOP_DELAY_MS)
    }

    @Test
    fun disconnectExtraKey_isFixedContractValue() {
        // Contract with Worker C (fixed — do not renegotiate): MainActivity
        // reads this exact extra key in onCreate + onNewIntent.
        assertEquals("deskhand_disconnect", DeskhandSessionService.EXTRA_DISCONNECT)
    }
}

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeskhandSessionServiceNotificationTest {

    @Test
    fun disconnectAction_presentWithLabelAndDisconnectExtra() {
        val controller = Robolectric.buildService(DeskhandSessionService::class.java)
        val service = controller.create().get()
        try {
            val notification = service.buildNotification("TestPC")
            assertEquals(1, notification.actions.size)
            val action = notification.actions[0]
            assertEquals("Disconnect", action.title.toString())
            // The PendingIntent must open MainActivity with the disconnect
            // extra — NOT ACTION_STOP (forbidden wiring C, spec §3.4).
            val saved = Shadows.shadowOf(action.actionIntent).savedIntent
            assertEquals(MainActivity::class.java.name, saved.component?.className)
            assertTrue(saved.getBooleanExtra(DeskhandSessionService.EXTRA_DISCONNECT, false))
            // It is an explicit MainActivity intent, not a service
            // ACTION_STOP intent: no action string, and the component is
            // the activity, not DeskhandSessionService.
            assertEquals(null, saved.action)
        } finally {
            controller.destroy()
        }
    }

    @Test
    fun noActionStart_selfStopsWithinSixtySeconds() {
        val controller = Robolectric.buildService(DeskhandSessionService::class.java)
        val service = controller.create().get()
        try {
            // System restart: no intent action.
            controller.withIntent(Intent())
            controller.startCommand(0, 0)
            // Advance the main looper past the 60s self-stop delay.
            Shadows.shadowOf(Looper.getMainLooper()).idleFor(61, TimeUnit.SECONDS)
            // FLAG — PC verification needed: `isStoppedBySelf` is the
            // Robolectric ShadowService API for Service.stopSelf(). If the
            // PC Gradle's Robolectric 4.16.1 does not expose it, drop this
            // one assertion; the SELF_STOP_DELAY_MS constant test above +
            // live tests T7/T11 (spec §8.3) still cover the behaviour.
            assertTrue(Shadows.shadowOf(service).isStoppedBySelf)
        } finally {
            controller.destroy()
        }
    }

    @Test
    fun actionStartWithinGrace_cancelsPendingSelfStop() {
        val controller = Robolectric.buildService(DeskhandSessionService::class.java)
        val service = controller.create().get()
        try {
            // Sticky restart first, then a real session within the 60s.
            controller.withIntent(Intent())
            controller.startCommand(0, 0)
            val start = Intent().apply {
                action = DeskhandSessionService.ACTION_START
                putExtra(DeskhandSessionService.EXTRA_PC_NAME, "TestPC")
            }
            controller.withIntent(start)
            controller.startCommand(0, 0)
            // Advance well past the original 60s deadline: the service
            // must NOT have stopped itself (see the isStoppedBySelf FLAG
            // in noActionStart_selfStopsWithinSixtySeconds).
            Shadows.shadowOf(Looper.getMainLooper()).idleFor(61, TimeUnit.SECONDS)
            assertFalse(Shadows.shadowOf(service).isStoppedBySelf)
        } finally {
            controller.destroy()
        }
    }
}
