package com.deskhand.android

import android.content.Intent
import android.net.Uri
import android.os.Build
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 100.026 (foreground service, spec §6 + §3.4): the notification-permission
 * ask-once / denied-banner / Connect context-line rules and the Disconnect
 * intent-extra check — all pure functions in MainActivity.kt, so they run
 * here without a device.
 *
 * Harness: the established Robolectric + @Config(sdk = [35]) pattern
 * (DiagnosticsScreenTest, ViewerLayoutTest). The Intent tests need
 * Robolectric (a plain-JUnit android.jar stub throws on getBooleanExtra);
 * the pure-boolean tests would also run as plain JUnit.
 *
 * What this file does NOT prove (needs the PC, Gate 4 live tests T2/T3/T9):
 * the system prompt actually appearing on first connect, the banner's
 * settings deep link opening the app's notification settings, a tap on the
 * notification's Disconnect action ending a live session, or the Compose
 * wiring (launcher registration inside ViewerScreen, banner Row placement
 * above ViewerLayout). A ViewerScreen compose test was deliberately NOT
 * written: composing ViewerScreen drives the real LaunchedEffect hook,
 * which calls ContextCompat.startForegroundService on connect — a live
 * service start the Robolectric harness cannot stand in for. The VM has
 * no Android SDK/Gradle, so these tests were written here, not run.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class NotificationPermissionTest {

    private val tiramisu = Build.VERSION_CODES.TIRAMISU // 33
    private val preTiramisu = Build.VERSION_CODES.S // 31

    // --- shouldAskNotificationPermission: ask once, on first connect only ---

    @Test fun ask_firstConnect_android13_notGranted_notAsked() {
        assertTrue(
            shouldAskNotificationPermission(
                askedBefore = false, granted = false, requestInFlight = false, sdkInt = tiramisu,
            )
        )
    }

    @Test fun ask_neverAfterDenial() {
        // Denied once: asked=true, granted=false — no second prompt, ever.
        // Settings link is the only path after this (spec §6.3).
        assertFalse(
            shouldAskNotificationPermission(
                askedBefore = true, granted = false, requestInFlight = false, sdkInt = tiramisu,
            )
        )
    }

    @Test fun ask_neverWhenAlreadyGranted() {
        assertFalse(
            shouldAskNotificationPermission(
                askedBefore = false, granted = true, requestInFlight = false, sdkInt = tiramisu,
            )
        )
    }

    @Test fun ask_neverWhenRequestInFlight() {
        // A connection flap while the prompt is open must not launch twice.
        assertFalse(
            shouldAskNotificationPermission(
                askedBefore = false, granted = false, requestInFlight = true, sdkInt = tiramisu,
            )
        )
    }

    @Test fun ask_neverBelowAndroid13() {
        assertFalse(
            shouldAskNotificationPermission(
                askedBefore = false, granted = false, requestInFlight = false, sdkInt = preTiramisu,
            )
        )
    }

    // --- shouldShowNotificationDeniedBanner: Viewer only, while connected, denied ---

    @Test fun banner_showsWhenConnectedAskedAndDenied() {
        // Also the revoke-in-Settings case: asked stays true, the live
        // check reads denied — the banner must appear.
        assertTrue(
            shouldShowNotificationDeniedBanner(
                connected = true, askedBefore = true, granted = false, sdkInt = tiramisu,
            )
        )
    }

    @Test fun banner_hiddenBeforeFirstAsk() {
        assertFalse(
            shouldShowNotificationDeniedBanner(
                connected = true, askedBefore = false, granted = false, sdkInt = tiramisu,
            )
        )
    }

    @Test fun banner_hiddenWhenDisconnected() {
        assertFalse(
            shouldShowNotificationDeniedBanner(
                connected = false, askedBefore = true, granted = false, sdkInt = tiramisu,
            )
        )
    }

    @Test fun banner_hiddenWhenGranted() {
        assertFalse(
            shouldShowNotificationDeniedBanner(
                connected = true, askedBefore = true, granted = true, sdkInt = tiramisu,
            )
        )
    }

    @Test fun banner_hiddenBelowAndroid13() {
        assertFalse(
            shouldShowNotificationDeniedBanner(
                connected = true, askedBefore = true, granted = false, sdkInt = preTiramisu,
            )
        )
    }

    // --- shouldShowConnectContextLine ---

    @Test fun contextLine_shownOnFirstUseOnly() {
        assertTrue(shouldShowConnectContextLine(askedBefore = false))
        assertFalse(shouldShowConnectContextLine(askedBefore = true))
    }

    // --- isDisconnectIntent (spec §3.4 wiring A) ---

    @Test fun disconnectExtra_trueTriggersDisconnect() {
        val intent = Intent().putExtra(DESKHAND_DISCONNECT_EXTRA, true)
        assertTrue(isDisconnectIntent(intent))
    }

    @Test fun disconnectExtra_falseDoesNotTrigger() {
        val intent = Intent().putExtra(DESKHAND_DISCONNECT_EXTRA, false)
        assertFalse(isDisconnectIntent(intent))
    }

    @Test fun disconnectExtra_absentDoesNotTrigger() {
        assertFalse(isDisconnectIntent(Intent()))
        assertFalse(isDisconnectIntent(null))
    }

    @Test fun disconnectExtra_unrelatedIntentDoesNotTrigger() {
        // A pair deep link (data URI, no extra) must not disconnect.
        val intent = Intent(Intent.ACTION_VIEW)
            .setData(Uri.parse("deskhand://pair?s=abc"))
        assertFalse(isDisconnectIntent(intent))
    }

    @Test fun disconnectExtra_keyIsContractValue() {
        // Fixed contract with Worker B (service side builds the
        // PendingIntent with this key): never rename silently.
        assertEquals("deskhand_disconnect", DESKHAND_DISCONNECT_EXTRA)
    }

    // --- refresh cadence (spec §3.3) ---

    @Test fun refreshInterval_isFiveMinutes() {
        assertEquals(5 * 60 * 1000L, DESKHAND_SESSION_REFRESH_INTERVAL_MS)
    }
}
