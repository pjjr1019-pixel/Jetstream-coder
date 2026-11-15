package com.deskhand.android

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import java.io.File
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.json.JSONObject

data class UpdateUi(val release: UpdateRelease? = null, val status: String = "Connect to your PC to check for updates.",
    val downloading: Boolean = false, val progress: Int = 0, val ready: Boolean = false, val show: Boolean = false)

object AppUpdates {
    var ui by mutableStateOf(UpdateUi()); private set
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadScheduledExecutor()
    private lateinit var context: Context
    private var send: ((Int, ByteArray) -> Boolean)? = null
    private var requestId = ""
    private var timer = 0L
    private var release: UpdateRelease? = null
    private var download: UpdateDownload? = null
    private val prompted = mutableSetOf<Long>()
    private var permissionPending = false
    private var confirmation: Intent? = null
    private var installBusy = false
    private var installTicket = 0
    // B2-G B8: consecutive bad chunks while downloading. A single bad chunk
    // no longer wipes the download — the same offset is re-requested, up
    // to MAX_CHUNK_RETRIES times; only then is the download discarded
    // (the partial file is still kept for a manual resume).
    private var chunkRetries = 0
    private const val MAX_CHUNK_RETRIES = 3
    private val prefs get() = context.getSharedPreferences("android-updates", Context.MODE_PRIVATE)
    private val apk get() = File(context.filesDir, "deskhand-update.apk")
    private val partial get() = File(context.filesDir, "deskhand-update.part")
    private fun display(change: (UpdateUi) -> UpdateUi) { main.post { ui = change(ui) } }
    private fun ownInfo() = context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_SIGNING_CERTIFICATES)

    fun init(ctx: Context) {
        if (::context.isInitialized) return
        context = ctx.applicationContext
        worker.execute {
            // B2-G B8: do NOT delete the partial on launch — a partial
            // download resumes from its offset (see UpdateDownload). Drop
            // it only when it belongs to no known release.
            val partialRelease = prefs.getString("partial-release", null)?.let { runCatching { UpdateRelease.parse(JSONObject(it)) }.getOrNull() }
            if (partial.exists() && partialRelease == null) {
                partial.delete()
                prefs.edit().remove("partial-sha256").apply()
            }
            val saved = prefs.getString("release", null)
            if (saved != null) runCatching {
                val r = UpdateRelease.parse(JSONObject(saved))
                if (ownInfo().longVersionCode >= r.versionCode) {
                    apk.delete(); prefs.edit().remove("release").apply()
                    display { it.copy(status = "Deskhand is up to date.") }
                } else {
                    verifyApk(r)
                    release = r
                    display { it.copy(release = r, ready = true, status = "Update downloaded. Tap Install to continue.") }
                }
            }.onFailure { apk.delete(); prefs.edit().remove("release").apply() }
            // A partial for a known release survives the restart; keep its
            // release in memory so a fresh check can resume it.
            if (release == null) release = partialRelease
        }
    }

    fun unsupported() = worker.execute {
        send = null
        display { it.copy(status = "This PC needs a newer Deskhand build to provide updates.") }
    }
    fun opened() = worker.execute {
        prompted.clear()
        val saved = release
        if (saved != null && apk.exists() && prompted.add(saved.versionCode)) {
            display { it.copy(show = true, release = saved, ready = true, status = "Downloaded update is ready to install.") }
        }
        if (send != null) checkNow(false)
    }
    fun connected(sender: (Int, ByteArray) -> Boolean) = worker.execute { send = sender; checkNow(false) }
    fun disconnected() = worker.execute {
        send = null; requestId = ""; timer++
        if (download != null) { discard(); display { it.copy(downloading = false, status = "PC disconnected. Reconnect and retry.") } }
    }
    fun show() = display { it.copy(show = true) }
    fun hide() = display { it.copy(show = false) }
    fun check() = worker.execute { checkNow(true) }
    private fun checkNow(manual: Boolean) {
        if (download != null || installBusy) return
        if (send == null) { display { it.copy(show = manual || it.show, status = "Connect to your PC to check for updates.") }; return }
        requestId = UUID.randomUUID().toString()
        display { it.copy(show = manual || it.show, status = "Checking your PC for updates…") }
        if (send?.invoke(Msg.UPDATE_CHECK, JSONObject().put("requestId", requestId).toString().toByteArray()) != true) {
            display { it.copy(status = "PC disconnected. Reconnect and retry.") }; return
        }
        deadline("This PC did not answer. It may need the newer Deskhand host.", false)
    }
    private fun deadline(message: String, transfer: Boolean) {
        val ticket = ++timer
        worker.schedule({
            if (ticket == timer) {
                requestId = ""
                if (transfer) discard()
                display { it.copy(downloading = false, status = message) }
            }
        }, if (transfer) 30 else 15, TimeUnit.SECONDS)
    }
    fun message(type: Int, bytes: ByteArray) = worker.execute {
        try {
            when (type) {
                Msg.UPDATE_INFO -> {
                    require(bytes.size <= 8192)
                    val o = JSONObject(String(bytes, Charsets.UTF_8))
                    if (requestId.isEmpty() || o.getString("requestId") != requestId) return@execute
                    timer++; requestId = ""
                    if (!o.getBoolean("available")) {
                        display { it.copy(status = if (it.ready) "Downloaded update is ready to install." else "No update is published on this PC.") }; return@execute
                    }
                    val r = UpdateRelease.parse(o.getJSONObject("release"))
                    if (r.versionCode <= ownInfo().longVersionCode) {
                        display { it.copy(status = "Deskhand is up to date.") }; return@execute
                    }
                    require(r.minSdk <= Build.VERSION.SDK_INT) { "This update requires a newer Android version." }
                    // A validated downloaded release remains usable without the original PC.
                    if (apk.exists() && release?.sha256 == r.sha256) {
                        val prompt = prompted.add(r.versionCode)
                        display { it.copy(release = r, ready = true, show = prompt || it.show, status = "Downloaded update is ready to install.") }; return@execute
                    }
                    apk.delete(); prefs.edit().remove("release").apply()
                    // B2-G B8: a partial for a different release is stale;
                    // a partial for THIS release is kept and resumed.
                    if (prefs.getString("partial-sha256", null) != r.sha256) {
                        partial.delete()
                        prefs.edit().remove("partial-release").remove("partial-sha256").apply()
                    }
                    release = r
                    val prompt = prompted.add(r.versionCode)
                    display { it.copy(release = r, ready = false, progress = 0, show = prompt || it.show, status = "A Deskhand update is available.") }
                }
                Msg.UPDATE_CHUNK -> {
                    val d = download ?: return@execute
                    timer++
                    try {
                        if (d.accept(bytes)) {
                            download = null
                            chunkRetries = 0
                            check(partial.renameTo(apk)) { "Couldn't save the downloaded update." }
                            verifyApk(d.release)
                            prefs.edit().putString("release", d.release.json()).remove("partial-release").remove("partial-sha256").apply()
                            display { it.copy(downloading = false, progress = 100, ready = true, status = "Download verified. Tap Install to continue.") }
                        } else {
                            chunkRetries = 0
                            display { it.copy(progress = (d.offset * 100 / d.release.size).toInt()) }
                            requestChunk(d)
                        }
                    } catch (e: Exception) {
                        // B2-G B8: one bad chunk no longer wipes the download
                        // or the saved release. The partial stays on disk and
                        // the same offset is re-requested, bounded — the
                        // transfer deadline still applies to each request.
                        if (download == null) throw e
                        chunkRetries++
                        if (chunkRetries > MAX_CHUNK_RETRIES) {
                            timer++; requestId = ""; discard()
                            display { it.copy(downloading = false, status = "The download kept failing. Reconnect and retry — the partial download is kept.") }
                        } else {
                            display { it.copy(status = "A bad chunk arrived. Retrying…") }
                            requestChunk(d)
                        }
                    }
                }
                Msg.UPDATE_ERROR -> {
                    if (download == null && requestId.isEmpty()) return@execute
                    require(bytes.size <= 8192)
                    val error = JSONObject(String(bytes, Charsets.UTF_8))
                    if (error.has("request")) {
                        val request = JSONObject(String(android.util.Base64.decode(error.getString("request"), android.util.Base64.NO_WRAP), Charsets.UTF_8))
                        val d = download
                        if (d != null) {
                            if (request.optString("sha256") != d.release.sha256 || request.optLong("offset", -1) != d.offset) return@execute
                        } else if (request.optString("requestId") != requestId) return@execute
                    }
                    timer++; requestId = ""; discard()
                    display { it.copy(downloading = false, status = "The PC couldn't provide this update. Check for updates and retry.") }
                }
            }
        } catch (e: Exception) {
            // B2-G B8: a transient error (bad chunk, lost reply) no longer
            // wipes the downloaded APK or the saved release — the partial
            // stays on disk for resume and a verified APK stays installable.
            timer++; requestId = ""; discard()
            display { it.copy(downloading = false, ready = false, status = e.message ?: "Couldn't download this update. Retry.") }
        }
    }
    fun startDownload() = worker.execute {
        try {
            if (download != null || installBusy) return@execute
            val r = release ?: error("Check for updates first.")
            check(send != null) { "Connect to your PC, then check for updates and retry." }
            check(context.filesDir.usableSpace > r.size * 2 + 16 * 1024 * 1024) { "Not enough storage. Free some space and retry." }
            // B2-G B8: resume only when the partial belongs to this release;
            // otherwise drop the stale bytes first. Record the release so a
            // restart can keep the partial (see init()).
            if (partial.exists() && prefs.getString("partial-sha256", null) != r.sha256) {
                partial.delete()
                prefs.edit().remove("partial-release").remove("partial-sha256").apply()
            }
            chunkRetries = 0
            download = UpdateDownload(r, partial)
            val d = download!!
            prefs.edit().putString("partial-release", r.json()).putString("partial-sha256", r.sha256).apply()
            display { it.copy(downloading = true, progress = (d.offset * 100 / r.size).toInt(), ready = false, status = "Downloading from your PC…") }
            requestChunk(d)
        } catch (e: Exception) { discard(); display { it.copy(downloading = false, status = e.message ?: "Couldn't start the download.") } }
    }
    private fun requestChunk(d: UpdateDownload) {
        check(send?.invoke(Msg.UPDATE_GET, JSONObject().put("sha256", d.release.sha256).put("offset", d.offset).toString().toByteArray()) == true) { "PC disconnected. Reconnect and retry." }
        deadline("The download timed out. Retry when your PC is reachable.", true)
    }
    // B2-G B8: discarding the in-memory download no longer deletes the
    // partial file — it is the resume point. Only an explicit user cancel
    // deletes it.
    private fun discard(deletePartial: Boolean = false) { runCatching { download?.close() }; download = null; if (deletePartial) { partial.delete(); prefs.edit().remove("partial-release").remove("partial-sha256").apply() } }
    fun cancel() = worker.execute { timer++; discard(deletePartial = true); display { it.copy(downloading = false, progress = 0, status = "Download cancelled. You can retry.") } }

    private fun verifyApk(r: UpdateRelease) {
        check(apk.length() == r.size) { "Update size mismatch." }
        val digest = java.security.MessageDigest.getInstance("SHA-256")
        apk.inputStream().use { input -> val b = ByteArray(65536); while (true) { val n = input.read(b); if (n < 0) break; digest.update(b, 0, n) } }
        check(digest.digest().joinToString("") { "%02x".format(it) } == r.sha256) { "Update checksum mismatch." }
        val archive = context.packageManager.getPackageArchiveInfo(apk.path, PackageManager.GET_SIGNING_CERTIFICATES) ?: error("Invalid Android APK.")
        val own = ownInfo()
        // B2-G low (B14): explicit null checks instead of !! — a malformed
        // APK can yield a null applicationInfo / signingInfo, which used to
        // throw NPE and (via the message() catch) wipe the download.
        val appInfo = archive.applicationInfo ?: error("Invalid Android APK.")
        val ownSigners = own.signingInfo?.apkContentsSigners ?: error("Couldn't read this app's signing certificate.")
        val archiveSigners = archive.signingInfo?.apkContentsSigners ?: error("Invalid Android APK.")
        check(archive.packageName == own.packageName && archive.longVersionCode == r.versionCode && archive.longVersionCode > own.longVersionCode &&
            appInfo.minSdkVersion == r.minSdk && r.minSdk <= Build.VERSION.SDK_INT) { "This APK is not a compatible newer Deskhand update." }
        val installedSigners = ownSigners.map { it.toCharsString() }.toSet()
        check(archiveSigners.map { it.toCharsString() }.toSet() == installedSigners) { "Signing certificate differs. This update cannot replace your installed app." }
    }
    fun resume(activity: Activity) {
        if (!permissionPending) return
        permissionPending = false
        if (activity.packageManager.canRequestPackageInstalls()) install(activity)
        else display { it.copy(status = "Install permission wasn't granted. Tap Install to try again.") }
    }
    fun install(activity: Activity) {
        confirmation?.let { intent ->
            confirmation = null
            runCatching { activity.startActivity(intent) }.onFailure {
                display { it.copy(status = "Couldn't open Android's confirmation. Tap Install to retry.") }
            }
            return
        }
        if (!activity.packageManager.canRequestPackageInstalls()) {
            permissionPending = true
            runCatching { activity.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${activity.packageName}"))) }.onFailure {
                permissionPending = false
                display { it.copy(status = "Couldn't open install permission settings. Enable installs for Deskhand in Android settings.") }
            }
            return
        }
        worker.execute {
            if (installBusy) return@execute
            installBusy = true
            var sessionId: Int? = null
            try {
                val r = release ?: error("Download an update first.")
                verifyApk(r)
                val installer = context.packageManager.packageInstaller
                val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
                params.setAppPackageName(context.packageName); params.setSize(r.size)
                if (Build.VERSION.SDK_INT >= 31) params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_REQUIRED)
                sessionId = installer.createSession(params)
                installer.openSession(sessionId).use { session ->
                    session.openWrite("base.apk", 0, r.size).use { output -> apk.inputStream().use { it.copyTo(output) }; session.fsync(output) }
                    val callback = Intent(context, UpdateInstallReceiver::class.java)
                    val flags = PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
                    val pending = PendingIntent.getBroadcast(context, sessionId, callback, flags)
                    session.commit(pending.intentSender)
                    // B2-G B7: bounded wait — installBusy used to stick
                    // forever if the installer callback was lost, silently
                    // disabling update checks/downloads until restart. The
                    // ticket keeps a stale timeout from clearing a newer
                    // install's flag; installResult() bumps the ticket too.
                    val ticket = ++installTicket
                    worker.schedule({ if (ticket == installTicket) installBusy = false }, 120, TimeUnit.SECONDS)
                }
                display { it.copy(status = "Preparing Android's install confirmation…") }
            } catch (e: Exception) {
                sessionId?.let { runCatching { context.packageManager.packageInstaller.abandonSession(it) } }
                installBusy = false
                display { it.copy(status = e.message ?: "Couldn't start installation. Try again.") }
            }
        }
    }
    fun installResult(status: Int, intent: Intent?) {
        worker.execute {
            installBusy = false
            installTicket++
            if (status == PackageInstaller.STATUS_PENDING_USER_ACTION && intent != null) {
                main.post { confirmation = intent; ui = ui.copy(show = true, status = "Tap Install to open Android's confirmation.") }
            } else {
                display { it.copy(show = true, status = when (status) {
                    PackageInstaller.STATUS_SUCCESS -> "Update installed. Reopen Deskhand."
                    PackageInstaller.STATUS_FAILURE_ABORTED -> "Installation cancelled. Tap Install to try again."
                    else -> "Android couldn't install the update (status $status). Tap Install to retry."
                }) }
            }
        }
    }
}

class UpdateInstallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        AppUpdates.init(context)
        @Suppress("DEPRECATION")
        val confirmation = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
        AppUpdates.installResult(intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE), confirmation)
    }
}
