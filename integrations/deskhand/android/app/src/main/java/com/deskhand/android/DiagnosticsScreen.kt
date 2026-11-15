package com.deskhand.android

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Diagnostics screen (100.016, E14 — spec §6).
 *
 * A full-screen lazy list of the same 12 rows as the Windows Diagnostics
 * GroupBox (spec §5.2), same order, same five states, same status dots,
 * same last-OK ages — all served by the PC-side `health` op over the
 * existing session. The phone never probes the PC directly and never
 * runs its own health check.
 *
 * Data flow: Model.pollHealth() issues {"op": "health"} on the existing
 * 2.5 s session poll (same loop as pollApprovals, no second poll loop);
 * handleChatResponse ("health") stores the snapshot in
 * Model.healthSnapshot. If the session is down the screen shows the
 * last snapshot with the offline banner — never a fresh-looking board
 * from a dead session.
 *
 * Pure functions (parse/format/labels) live at the top of this file so
 * DiagnosticsScreenTest can cover them without Compose. The composable
 * itself only reads Model state and launches the share sheet.
 *
 * 100.029 (E41 §6.2): sharing a bundle appends a `[logcat]` section —
 * this app's own logcat lines (INFO and above, at most 200), scrubbed
 * on the phone with the same rules as the PC export. A rotated buffer
 * is reported as `[logcat: no lines captured]`, never faked.
 */

/** The five status words of spec §2.2 — and only these. */
enum class HealthStatus { OK, DEGRADED, DOWN, UNKNOWN, IDLE }

/** One component row, in the §5.2 order (see DIAGNOSTIC_COMPONENT_ORDER). */
data class HealthComponentRow(
    val key: String,
    val label: String,
    val status: HealthStatus,
    val lastOkEpochSec: Long,
    val version: String,
    val error: String,
    /**
     * 100.029 (E41 §6.3): the component's configured log level, from the
     * health() snapshot's `level` field (Worker B adds it service-side).
     * Blank when the PC predates it — the footer then says "level
     * unknown" rather than inventing a level.
     */
    val level: String = "",
)

/** One `health` op snapshot: one as_of, 12 rows. */
data class HealthSnapshot(
    val asOfEpochSec: Long,
    val asOfHuman: String,
    val components: List<HealthComponentRow>,
)

/** Spec §5.2 row order — the op's 12 component keys, exactly these. */
val DIAGNOSTIC_COMPONENT_ORDER: List<Pair<String, String>> = listOf(
    "windows_app" to "Windows app",
    "service" to "Service (deskhand_service.py)",
    "gateway" to "Gateway",
    "worker" to "Worker",
    "relay" to "Relay (built-in)",
    "node" to "Node (Tailscale)",
    "agent" to "Laptop agent",
    "bridge" to "Bridge (MuseBridge)",
    "grant" to "Grant (Full PC access)",
    "muse" to "Muse sign-in",
    "lifecycle" to "Lifecycle monitor",
    "phone" to "Paired phone(s)",
)

/** §6.5: the reserved Approvals section shows exactly this until E08 lands. */
const val APPROVALS_EMPTY_TEXT = "no approvals recorded"

/** §6.3: the exact session-down share-button text. */
const val SHARE_SESSION_DOWN_LABEL = "session down — export from the PC"

/** Parse one status word. Anything unrecognised is UNKNOWN — never OK by default. */
fun parseHealthStatus(raw: String): HealthStatus = when (raw.uppercase()) {
    "OK" -> HealthStatus.OK
    "DEGRADED" -> HealthStatus.DEGRADED
    "DOWN" -> HealthStatus.DOWN
    "IDLE" -> HealthStatus.IDLE
    else -> HealthStatus.UNKNOWN
}

/**
 * Parse the `health` op result (§2.1 shape: {as_of, as_of_human,
 * components: {key: {status, last_ok, version, error}}}) into a
 * snapshot. Returns null when there is no components object at all (no
 * snapshot). A key the op omits renders UNKNOWN / "not checked yet" —
 * never OK by default, never a missing row. Extra keys are ignored: the
 * board is always exactly the 12 rows.
 */
fun parseHealthSnapshot(result: JSONObject): HealthSnapshot? {
    val components = result.optJSONObject("components") ?: return null
    val rows = DIAGNOSTIC_COMPONENT_ORDER.map { (key, label) ->
        val row = components.optJSONObject(key)
        if (row == null) {
            HealthComponentRow(key, label, HealthStatus.UNKNOWN, 0, "", "not checked yet")
        } else {
            HealthComponentRow(
                key = key,
                label = label,
                status = parseHealthStatus(row.optString("status", "")),
                lastOkEpochSec = row.optLong("last_ok", 0),
                version = row.optString("version", ""),
                error = row.optString("error", ""),
                level = row.optString("level", ""),
            )
        }
    }
    return HealthSnapshot(
        asOfEpochSec = result.optLong("as_of", 0),
        asOfHuman = result.optString("as_of_human", ""),
        components = rows,
    )
}

/** Age formatting, mirroring the Windows FormatAge (Xs / Xm / Xh Ym). */
fun formatAgeSeconds(seconds: Long): String {
    val s = seconds.coerceAtLeast(0)
    if (s < 60) return "${s}s"
    val m = s / 60
    if (m < 60) return "${m}m"
    return "${m / 60}h ${m % 60}m"
}

/** Session-duration formatting (same units as formatAgeSeconds). */
fun formatSessionDuration(startedAtMs: Long?, nowMs: Long): String {
    if (startedAtMs == null) return "not connected"
    val seconds = ((nowMs - startedAtMs) / 1000).coerceAtLeast(0)
    if (seconds < 60) return "${seconds}s"
    val minutes = seconds / 60
    if (minutes < 60) return "${minutes}m ${seconds % 60}s"
    return "${minutes / 60}h ${minutes % 60}m"
}

/** HH:MM:SS in the device's local time, for the offline banner. */
fun formatClockHhMmSs(epochSec: Long): String =
    SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(epochSec * 1000))

/**
 * The offline banner (§6.2), verbatim: "last snapshot HH:MM:SS —
 * session down, values may be stale". Null while the session is live
 * (no banner). No snapshot yet (or a snapshot with no as_of) reads
 * "no snapshot yet — session down".
 */
fun offlineBannerText(sessionLive: Boolean, snapshot: HealthSnapshot?): String? {
    if (sessionLive) return null
    val snap = snapshot ?: return "no snapshot yet — session down"
    if (snap.asOfEpochSec <= 0) return "no snapshot yet — session down"
    return "last snapshot ${formatClockHhMmSs(snap.asOfEpochSec)} — session down, values may be stale"
}

/** §6.3 share-button label. The session-down text is the spec's verbatim string. */
fun shareButtonLabel(sessionLive: Boolean, pending: Boolean): String = when {
    !sessionLive -> SHARE_SESSION_DOWN_LABEL
    pending -> "Requesting diagnostics…"
    else -> "Share diagnostics"
}

/** The share button is only usable while the session is live and no request is in flight. */
fun shareButtonEnabled(sessionLive: Boolean, pending: Boolean): Boolean = sessionLive && !pending

/**
 * Phone-connection transport (§6.2): the live transport's label, or
 * UNKNOWN when it cannot be determined. The last-known transport is
 * NOT used here — it is not the transport in use, and this row never
 * guesses.
 */
fun phoneConnectionText(sessionLive: Boolean, transport: TransportKind?): String {
    if (!sessionLive) return "UNKNOWN"
    return transport?.label() ?: "UNKNOWN"
}

/** Foreground-service state line for the Android-app local row. */
fun foregroundServiceText(running: Boolean): String =
    if (running) "foreground service: running" else "foreground service: not running"

/** Notification-permission state line for the Android-app local row. */
fun notificationPermissionText(granted: Boolean, legacy: Boolean): String = when {
    legacy -> "notifications: allowed (Android 12 and earlier)"
    granted -> "notifications: granted"
    else -> "notifications: not granted"
}

/** Status-dot colour, per the polish §5 dot language (DeskhandTheme tokens). */
fun healthStatusColor(status: HealthStatus): Color = when (status) {
    HealthStatus.OK -> DeskhandSuccess
    HealthStatus.DEGRADED -> DeskhandWarningText
    HealthStatus.DOWN -> DeskhandError
    HealthStatus.UNKNOWN, HealthStatus.IDLE -> DeskhandTextSecondary
}

/** Open the Android share sheet with received diagnostics text. No file is written. */
fun shareDiagnosticsText(context: Context, text: String) {
    val intent = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_TEXT, text)
    }
    context.startActivity(Intent.createChooser(intent, "Share diagnostics"))
}

// ---------------------------------------------------------------------------
// 100.029 (E41 structured logging, spec §6.2): Android logcat joins the
// diagnostics bundle. Pure functions at the top of this file so the new
// test can cover them without Compose; captureAppLogcat() is the only
// impure piece (it runs the logcat process).
// ---------------------------------------------------------------------------

/** E41 §6.2 / D5: at most 200 logcat lines in a share. */
const val LOGCAT_MAX_LINES = 200

/**
 * The logcat tags the capture filters to. These are the tags the VM
 * tree actually logs under — "Deskhand" (PcLink.kt, PcStore.kt) and
 * "DeskhandVideo" (AdaptiveVideoDecoder.kt).
 *
 * TAG VERIFICATION NOTE (spec §6.2): the spec's "no Log.* calls were
 * found in the VM android/ tree" is stale — the calls exist, with the
 * tags above. Even so, the live PC Android tree is the authority on
 * the real tag set: the VM tree does not faithfully mirror it, so
 * Gate 4 must confirm the tags there before this filter is trusted.
 * A tag this filter misses is a silent hole in the [logcat] section.
 */
val LOGCAT_TAGS: Set<String> = setOf("Deskhand", "DeskhandVideo")

/**
 * The section shown when the buffer rotated (or the capture failed)
 * and no lines were captured. Never silently omitted, never faked
 * from an older capture.
 */
const val LOGCAT_EMPTY_SECTION = "[logcat: no lines captured]"

/** A line dropped because it cannot be scrubbed confidently (PC scrub rule 6, same marker text). */
const val LOGCAT_DROPPED_MARKER = "[1 line dropped — could not scrub]"

/** Priorities at INFO and above (spec §6.2). V and D never reach a share — there is no "include debug" toggle on the phone (Windows only). */
private val LOGCAT_KEPT_PRIORITIES = setOf('I', 'W', 'E', 'F')

/** Token-shaped values (PC scrub rule 1): 32+ hex, then 32+ base64-ish. */
private val LOGCAT_HEX_TOKEN_RE = Regex("[0-9a-fA-F]{32,}")
private val LOGCAT_B64_TOKEN_RE = Regex("[A-Za-z0-9_\\-+/]{32,}")

/** Credential field values (PC scrub rule 2): the value runs to end-of-line. */
private val LOGCAT_FIELD_NAMES = listOf("token", "secret", "password", "api_key", "apikey", "credential", "private_key", "bearer")
private val LOGCAT_FIELD_RE = Regex(
    """(?i)\b(${LOGCAT_FIELD_NAMES.joinToString("|")})\s*([=:])\s*.+"""
)

/** Token-bearing URLs (PC scrub rule 4): keep the URL up to ?, query -> [REDACTED]. */
private val LOGCAT_URL_QUERY_RE = Regex("""([A-Za-z][A-Za-z0-9+.\-]*://[^\s"'<>?]+)\?([^\s"'<>]*)""")
private val LOGCAT_SENSITIVE_QUERY_RE = Regex(
    """(?i)(token|secret|password|api_key|apikey|credential|private_key|bearer|auth|key)="""
)

/** Config-file contents are never included (PC scrub rule 3): cut at the path. */
private val LOGCAT_CONFIG_FILES = listOf("agent_config.json", "musebridge.config.json")

/**
 * Keep only this app's own logcat lines from a threadtime dump
 * (`MM-DD HH:MM:SS.mmm  PID  TID P TAG: message`): the pid must be
 * ours, the tag must be one of [tags], and the priority must be INFO
 * or above. Header lines ("--------- beginning of …") and lines that
 * do not parse as threadtime are dropped. The result is capped at
 * LOGCAT_MAX_LINES, newest last. The priority/tag check here is
 * defence in depth on top of the logcat invocation's own `-s …:I`
 * filter, so a DEBUG line can never slip in if the invocation changes.
 */
fun filterLogcatLines(lines: List<String>, tags: Set<String> = LOGCAT_TAGS, pid: Int): List<String> {
    val kept = lines.mapNotNull { line ->
        val parts = line.split(Regex("\\s+"))
        if (parts.size < 6) return@mapNotNull null
        val linePid = parts[2].toIntOrNull() ?: return@mapNotNull null
        if (linePid != pid) return@mapNotNull null
        if (parts[4].length != 1 || parts[4][0] !in LOGCAT_KEPT_PRIORITIES) return@mapNotNull null
        val tag = parts[5].removeSuffix(":")
        if (tag !in tags) return@mapNotNull null
        line
    }
    return kept.takeLast(LOGCAT_MAX_LINES)
}

/**
 * Scrub one logcat line with the same rules as the PC export
 * (deskhand_diagnostics.py scrub_line, rules 1–4, same order):
 * token-bearing URL queries collapse to `?[REDACTED]`, config-file
 * contents are cut at the path, credential field values become
 * `[REDACTED]`, token-shaped values (32+ hex / base64) become
 * `[REDACTED-TOKEN]`. Returns null when the line carries control
 * characters and cannot be scrubbed confidently — the caller emits
 * LOGCAT_DROPPED_MARKER for it. The phone never shares an unscrubbed
 * tail. A scrubbed line is never re-scrubbed: `[REDACTED-TOKEN]` holds
 * no 32-char run.
 */
fun scrubLogcatLine(line: String): String? {
    if (line.any { it.code in 0x00..0x08 || it.code in 0x0B..0x0C || it.code in 0x0E..0x1F }) return null
    var work = line
    work = LOGCAT_URL_QUERY_RE.replace(work) { match ->
        if (LOGCAT_SENSITIVE_QUERY_RE.containsMatchIn(match.groupValues[2])) "${match.groupValues[1]}?[REDACTED]"
        else match.value
    }
    for (name in LOGCAT_CONFIG_FILES) {
        val idx = work.indexOf(name)
        if (idx >= 0) {
            work = work.substring(0, idx + name.length)
            break
        }
    }
    work = LOGCAT_FIELD_RE.replace(work) { match ->
        "${match.groupValues[1]}${match.groupValues[2]}[REDACTED]"
    }
    work = LOGCAT_HEX_TOKEN_RE.replace(work, "[REDACTED-TOKEN]")
    work = LOGCAT_B64_TOKEN_RE.replace(work, "[REDACTED-TOKEN]")
    return work
}

/** Scrub a list of logcat lines; unscrubbable lines become LOGCAT_DROPPED_MARKER (PC scrub rule 6). */
fun scrubLogcatLines(lines: List<String>): List<String> =
    lines.map { scrubLogcatLine(it) ?: LOGCAT_DROPPED_MARKER }

/**
 * The `[logcat]` section for the share text: this app's INFO-and-above
 * lines, scrubbed, newest last, at most LOGCAT_MAX_LINES. An empty
 * capture — the buffer rotated, or the capture itself failed — is
 * reported as LOGCAT_EMPTY_SECTION, never silently omitted and never
 * faked from an older capture.
 */
fun buildLogcatSection(rawLines: List<String>, pid: Int): String {
    val lines = scrubLogcatLines(filterLogcatLines(rawLines, pid = pid))
    if (lines.isEmpty()) return LOGCAT_EMPTY_SECTION
    return "[logcat]\n" + lines.joinToString("\n")
}

/**
 * Runs `logcat -d -t 200` filtered to the app's tags at INFO and above,
 * in threadtime form (so filterLogcatLines can match the pid). The
 * dump is bounded at 2 s on a reader thread, so a stuck logcat can
 * never hang the share sheet; the wait uses only API-1 calls
 * (Thread.join / Process.destroy), never waitFor(timeout). Any failure
 * — no binary, timeout, unreadable output — returns an empty list,
 * which buildLogcatSection reports honestly as no lines captured.
 */
fun captureAppLogcat(): List<String> {
    return try {
        val proc = Runtime.getRuntime().exec(
            arrayOf(
                "logcat", "-d", "-t", LOGCAT_MAX_LINES.toString(), "-v", "threadtime",
                "Deskhand:I", "DeskhandVideo:I",
            )
        )
        var lines: List<String> = emptyList()
        val reader = Thread {
            lines = runCatching { proc.inputStream.bufferedReader().readLines() }.getOrDefault(emptyList())
        }
        reader.isDaemon = true
        reader.start()
        reader.join(2000)
        if (reader.isAlive) {
            proc.destroy()
            return emptyList()
        }
        runCatching { proc.waitFor() }
        lines
    } catch (_: Exception) {
        emptyList()
    }
}

/**
 * Appends the `[logcat]` section to a PC export bundle, for the share
 * sheet. The capture runs at share time, so the section is a snapshot
 * of right now — never a stale capture from when the bundle was
 * requested. Only ever called with a PC bundle in hand: the logcat
 * section alone is not a bundle, and a bundle is never sent without
 * the PC tails. If the session is down the button is already disabled
 * (shareButtonEnabled), so this never sends a stale or partial bundle.
 */
fun appendLogcatSection(bundleText: String): String {
    val section = buildLogcatSection(captureAppLogcat(), android.os.Process.myPid())
    return "$bundleText\n\n$section"
}

/** Shares a PC diagnostics bundle with the `[logcat]` section appended. */
fun shareDiagnosticsBundle(context: Context, bundleText: String) {
    shareDiagnosticsText(context, appendLogcatSection(bundleText))
}

@Composable
private fun HealthRow(row: HealthComponentRow, nowEpochSec: Long) {
    val ageText = if (row.lastOkEpochSec <= 0) "last OK never"
    else "last OK ${formatAgeSeconds((nowEpochSec - row.lastOkEpochSec).coerceAtLeast(0))} ago"
    val detail = buildString {
        append(ageText)
        if (row.version.isNotEmpty()) {
            append(" — ")
            append(row.version)
        }
    }
    Row(
        Modifier.fillMaxWidth().padding(vertical = 6.dp),
        verticalAlignment = Alignment.Top,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        StatusDot(healthStatusColor(row.status), Modifier.padding(top = 4.dp))
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(row.label, style = MaterialTheme.typography.bodyMedium)
                Text(row.status.name, style = MaterialTheme.typography.bodySmall, color = healthStatusColor(row.status))
            }
            Text(detail, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (row.error.isNotEmpty()) {
                Text(
                    row.error,
                    style = MaterialTheme.typography.bodySmall,
                    color = DeskhandError,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

@Composable
private fun LocalRow(label: String, lines: List<String>) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 6.dp),
        verticalAlignment = Alignment.Top,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        StatusDot(DeskhandTextSecondary, Modifier.padding(top = 4.dp))
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(label, style = MaterialTheme.typography.bodyMedium)
                Text("local", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            lines.forEach { line ->
                Text(line, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
fun DiagnosticsScreen() {
    val context = LocalContext.current
    BackHandler { Model.screen = Screen.Home }
    // 1 s UI ticker (recompose only — no network): keeps the "last OK …
    // ago" ages and the session duration counting while the screen is
    // open, between 2.5 s polls. Cancelled when the screen leaves
    // composition. This is not a second poll loop: no request is issued.
    var tick by remember { mutableStateOf(0L) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(1000)
            tick++
        }
    }
    val nowMs = remember(tick) { System.currentTimeMillis() }
    val nowEpochSec = nowMs / 1000

    val sessionLive = Model.diagnosticsSessionLive
    val snapshot = Model.healthSnapshot
    val banner = offlineBannerText(sessionLive, snapshot)

    // Share-on-arrival: when a requested bundle lands (nonce bump), open
    // the share sheet exactly once. The baseline is the nonce on entry,
    // so re-opening the screen never re-shares a previous bundle.
    var lastSharedNonce by remember { mutableStateOf(Model.diagnosticsExportNonce) }
    val exportNonce = Model.diagnosticsExportNonce
    LaunchedEffect(exportNonce) {
        if (exportNonce > 0 && exportNonce != lastSharedNonce) {
            lastSharedNonce = exportNonce
            Model.diagnosticsExportText?.let { shareDiagnosticsBundle(context, it) }
        }
    }

    val versionCodeText = remember {
        runCatching {
            val info = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                context.packageManager.getPackageInfo(context.packageName, PackageManager.PackageInfoFlags.of(0))
            } else {
                @Suppress("DEPRECATION")
                context.packageManager.getPackageInfo(context.packageName, 0)
            }
            info.longVersionCode.toString()
        }.getOrNull() ?: "unknown"
    }
    val notificationsGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    Column(
        Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            TextButton(onClick = { Model.screen = Screen.Home }) { Text("Back") }
            Text("Diagnostics", style = MaterialTheme.typography.headlineMedium)
        }
        Button(
            onClick = {
                // A bundle already received shares immediately; otherwise
                // request one from the PC. The request carries this
                // phone's version ("100.016 (code 42)" shape) so the
                // export header names the Android build that asked for
                // it (Worker A, PATCH-REPORT-A §0).
                val text = Model.diagnosticsExportText
                if (text != null) shareDiagnosticsBundle(context, text)
                else Model.requestDiagnosticsExport("${BuildConfig.VERSION_NAME} (code $versionCodeText)")
            },
            enabled = shareButtonEnabled(sessionLive, Model.diagnosticsExportPending),
            modifier = Modifier.heightIn(min = 48.dp),
            colors = ButtonDefaults.buttonColors(containerColor = DeskhandPrimary, contentColor = Color.White),
        ) { Text(shareButtonLabel(sessionLive, Model.diagnosticsExportPending)) }
        Model.diagnosticsExportError?.let { error ->
            Text(error, style = MaterialTheme.typography.bodySmall, color = DeskhandError)
        }

        LazyColumn(Modifier.weight(1f).fillMaxWidth()) {
            if (banner != null) {
                stickyHeader {
                    Text(
                        banner,
                        style = MaterialTheme.typography.bodySmall,
                        color = DeskhandError,
                        modifier = Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.errorContainer).padding(8.dp),
                    )
                }
            }
            if (snapshot == null) {
                item {
                    Text(
                        if (sessionLive) "Waiting for the first snapshot…" else "No snapshot received yet.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            } else {
                items(snapshot.components) { row -> HealthRow(row, nowEpochSec) }
            }
            // §6.5 Approvals — reserved, not a 14th row. Until E08 lands
            // it shows the honest empty state and nothing else. The
            // offline banner above covers this section too when the
            // session is down: it never shows a fresh-looking empty state
            // from a dead session.
            item {
                Text("Approvals", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 16.dp))
                Text(APPROVALS_EMPTY_TEXT, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            // §6.2 phone-local rows, labelled local, with no PC equivalent.
            item {
                Text("This phone (local)", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 16.dp))
                LocalRow(
                    "Android app",
                    listOf(
                        "v${BuildConfig.VERSION_NAME} (code $versionCodeText)",
                        foregroundServiceText(DeskhandSessionService.isRunning),
                        notificationPermissionText(notificationsGranted, Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU),
                    ),
                )
                LocalRow(
                    "Phone connection",
                    listOf(
                        "transport: ${phoneConnectionText(sessionLive, Model.transport)}",
                        "session: ${formatSessionDuration(Model.sessionConnectedAtMs, nowMs)}",
                        // Data used this session: omitted — E25's counter
                        // has not landed; never estimated (spec §6.2).
                    ),
                )
            }
        }
    }
}
