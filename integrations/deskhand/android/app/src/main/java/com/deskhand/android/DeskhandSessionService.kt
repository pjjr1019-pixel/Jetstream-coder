package com.deskhand.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import androidx.core.content.ContextCompat

/**
 * Elapsed-session suffix for the notification text (pure; unit-tested in
 * DeskhandSessionServiceTest). Whole minutes; under one minute, no suffix;
 * past 60 minutes, hours + minutes.
 *
 * Examples: 0 ms -> "", 60_000 -> "· 1m", 23 min -> "· 23m",
 * 60 min -> "· 1h 0m", 72 min -> "· 1h 12m", 120 min -> "· 2h 0m".
 */
fun formatSessionElapsed(elapsedMillis: Long): String {
    val totalMinutes = elapsedMillis / 60_000L
    if (totalMinutes < 1L) return ""
    return if (totalMinutes < 60L) {
        "· ${totalMinutes}m"
    } else {
        "· ${totalMinutes / 60L}h ${totalMinutes % 60L}m"
    }
}

/**
 * Foreground service that keeps Deskhand's live desktop connection alive
 * while the viewer is connected (added 2026-11-08).
 *
 * Why a foreground service: without one, Android can kill the app when it
 * is backgrounded or the phone dozes, which drops the desktop session.
 * The service shows an ongoing notification and is stopped when the
 * viewer disconnects, so it only runs while a session is live.
 *
 * Foreground-service type: DATA_SYNC is the closest fit for keeping a
 * live desktop connection (continuous data transfer). Android 14+
 * requires the type to be declared in AndroidManifest.xml (it is:
 * android:foregroundServiceType="dataSync") and the matching
 * FOREGROUND_SERVICE_DATA_SYNC permission. The only arguably closer type
 * would be "specialUse" with a declared use-case, which carries extra
 * Play review requirements; DATA_SYNC is the safer choice for a
 * side-loaded companion app. Revisit if Play distribution is ever
 * planned.
 *
 * Wake lock (100.026, NEW — the base had none): a partial wake lock
 * "Deskhand:session" keeps the CPU awake with the screen off so the
 * stream doesn't stall. Acquired in startInForeground; released in
 * stopSession (ACTION_STOP path + 60s self-stop) and in onDestroy.
 * There is no path that holds the lock without a live session:
 * every stop path releases it BEFORE stopSelf().
 */
class DeskhandSessionService : Service() {

    // 100.026 (§3.3): set on the first ACTION_START after a stop (or after a
    // sticky restart that re-establishes a session), cleared on stop.
    // The only new state in the service. A refresh ACTION_START (Viewer
    // 5-minute elapsed refresh) rebuilds the notification from this
    // timestamp — it does NOT reset it.
    private var sessionStartedAt = 0L

    // 100.026 (§5): the partial wake lock. Null when not held.
    private var wakeLock: PowerManager.WakeLock? = null

    // 100.026 (§4): main-looper handler for the 60s self-stop.
    private val mainHandler = Handler(Looper.getMainLooper())
    private val selfStopRunnable = Runnable { stopSession() }

    override fun onCreate() {
        super.onCreate()
        isRunning = true
        createChannel()
    }

    override fun onDestroy() {
        mainHandler.removeCallbacks(selfStopRunnable)
        releaseWakeLock()
        isRunning = false
        super.onDestroy()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopSession()
            }
            ACTION_START -> {
                // A live session re-established within the 60s grace: cancel
                // the pending self-stop and proceed normally.
                mainHandler.removeCallbacks(selfStopRunnable)
                if (sessionStartedAt == 0L) {
                    sessionStartedAt = System.currentTimeMillis()
                }
                val pcName = intent.getStringExtra(EXTRA_PC_NAME).orEmpty()
                startInForeground(buildNotification(pcName))
            }
            else -> {
                // Started without an action (e.g. system restart after a
                // START_STICKY relaunch): stay in the foreground with a
                // generic notification rather than letting the system kill
                // the service for not calling startForeground promptly.
                // If no session is re-established within 60s, stop itself
                // (and release the wake lock) — a sticky service with no
                // live session must not sit there forever holding the lock.
                startInForeground(buildNotification(""))
                mainHandler.postDelayed(selfStopRunnable, SELF_STOP_DELAY_MS)
            }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startInForeground(notification: Notification) {
        acquireWakeLock()
        // startForeground(id, notification, type) needs API 29+; minSdk is
        // 28, so use the 2-arg form on API 28 and the typed form above it.
        // On Android 14+ the type also comes from the manifest declaration.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    /**
     * The one stop path: ACTION_STOP, the 60s self-stop, and (via
     * onDestroy's release) service teardown all funnel here. Releases the
     * wake lock BEFORE stopSelf() — a self-stop that leaks the lock is the
     * same bug as a forgotten lock.
     */
    private fun stopSession() {
        mainHandler.removeCallbacks(selfStopRunnable)
        releaseWakeLock()
        sessionStartedAt = 0L
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "Deskhand:session"
        ).also { it.acquire() }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    // internal (not private) so the Robolectric notification test in
    // DeskhandSessionServiceTest can build the notification directly
    // without going through startForeground's shadow internals.
    internal fun buildNotification(pcName: String): Notification {
        val base = if (pcName.isBlank()) {
            "Deskhand — connected"
        } else {
            "Deskhand — connected to $pcName"
        }
        val elapsed = if (sessionStartedAt == 0L) {
            ""
        } else {
            formatSessionElapsed(System.currentTimeMillis() - sessionStartedAt)
        }
        val text = if (elapsed.isEmpty()) base else "$base $elapsed"
        val openApp = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        // 100.026 (§3.4, wiring A): "Disconnect" opens MainActivity with
        // the disconnect extra; MainActivity handles it by calling the
        // existing disconnect path (Model.leaveViewer). It is NOT wired to
        // ACTION_STOP — that would kill the notification and wake lock
        // while leaving the stream running headless.
        val disconnect = PendingIntent.getActivity(
            this,
            1,
            Intent(this, MainActivity::class.java).putExtra(EXTRA_DISCONNECT, true),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Deskhand")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(openApp)
            .setOngoing(true)
            .addAction(
                Notification.Action.Builder(0, "Disconnect", disconnect).build()
            )
            .build()
    }

    private fun createChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Deskhand session",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Shows while Deskhand is connected to your PC"
        }
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(channel)
    }

    companion object {
        const val ACTION_START = "com.deskhand.android.START_SESSION"
        const val ACTION_STOP = "com.deskhand.android.STOP_SESSION"
        const val EXTRA_PC_NAME = "pc_name"

        /**
         * 100.026 (§3.4, contract with Worker C — fixed, do not renegotiate):
         * the Disconnect notification action's intent extra. MainActivity
         * handles it in onCreate + onNewIntent by calling
         * Model.leaveViewer(). Boolean, always true when present.
         */
        const val EXTRA_DISCONNECT = "deskhand_disconnect"

        /**
         * 100.026 (§4): sticky restart with no session re-established
         * within this long stops itself (and releases the wake lock).
         */
        const val SELF_STOP_DELAY_MS = 60_000L

        // 100.016 (E14 diagnostics): true while the service is actually
        // running (same process, so this flag is the ground truth — the
        // Diagnostics screen reads it instead of guessing from the
        // connection state). Set in onCreate/onDestroy.
        @Volatile
        var isRunning = false
            private set

        private const val CHANNEL_ID = "deskhand-session"
        private const val NOTIFICATION_ID = 1

        /** Start (or update) the session service; pcName shows in the notification. */
        fun start(context: Context, pcName: String = "") {
            val intent = Intent(context, DeskhandSessionService::class.java).apply {
                action = ACTION_START
                putExtra(EXTRA_PC_NAME, pcName)
            }
            ContextCompat.startForegroundService(context, intent)
        }

        /** Stop the session service (removes the notification). */
        fun stop(context: Context) {
            val intent = Intent(context, DeskhandSessionService::class.java).apply {
                action = ACTION_STOP
            }
            // A plain startService is enough to deliver STOP: the service
            // answers it with stopForeground + stopSelf. The app counts as
            // foreground while the service is running, so this is allowed;
            // the try/catch covers the edge case where the service is
            // already gone and the app is backgrounded.
            try {
                context.startService(intent)
            } catch (_: IllegalStateException) {
                // Service already gone; nothing to stop.
            }
        }
    }
}
