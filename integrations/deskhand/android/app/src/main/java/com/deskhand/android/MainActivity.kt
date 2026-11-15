package com.deskhand.android

import android.content.res.Configuration
import android.graphics.ColorMatrix
import android.graphics.ColorMatrixColorFilter
import android.graphics.RenderEffect
import android.os.Build
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.testTag
import androidx.compose.runtime.saveable.rememberSaveable
import android.content.Intent
import androidx.compose.ui.unit.sp
import android.net.Uri
import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.asComposeRenderEffect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipPath
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.positionChange
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import org.json.JSONObject
import kotlinx.coroutines.delay
import kotlin.math.abs

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        Model.init(this)
        if (getPreferences(MODE_PRIVATE).getBoolean("embedded-tailscale", false)) EmbeddedTailscale.start(this)
        handleIntent(intent)
        Model.autoConnect()
        setContent {
            MaterialTheme(typography = DeskhandViewerTypography) {
                Surface(modifier = Modifier.fillMaxSize()) {
                    // Agents drawer state lives at the activity level (100.004
                    // fix): the app auto-connects into the Viewer on launch,
                    // so a Home-only button is never seen. Hoisting the state
                    // here makes the drawer reachable from Home AND Viewer.
                    var showJetstream by remember { mutableStateOf(false) }
                    var showAgentsDrawer by remember { mutableStateOf(false) }
                    // 100.017 (100.018 design §B1): the drawer snapshot
                    // lives in Model now — the 2.5 s loop and the drawer
                    // poll both feed the same state; no local copies.
                    Box(Modifier.fillMaxSize()) {
                        // 100.016 (E14 diagnostics): the 2.5 s session poll
                        // lives here, not in ViewerScreen — it keeps running
                        // while the Diagnostics screen is open, on the same
                        // loop, with no second timer. pollApprovals() and
                        // pollHealth() each guard their own in-flight
                        // request, so a slow PC never stacks requests.
                        LaunchedEffect(Model.viewerConnection) {
                            while (Model.viewerConnection == "Connected") {
                                Model.pollApprovals()
                                Model.pollHealth()
                                kotlinx.coroutines.delay(2500)
                            }
                        }
                        when (Model.screen) {
                            Screen.Home -> HomeScreen(
                                onScan = ::scanQr,
                                agentsRunningCount = Model.agentsSnapshot.runningCount,
                                onAgentsClick = { showAgentsDrawer = true },
                            )
                            Screen.Pair -> PairScreen(onScan = ::scanQr)
                            Screen.Diagnostics -> DiagnosticsScreen()
                            Screen.Viewer -> DeskhandViewerTheme {
                                ViewerScreen(
                                    agentsRunningCount = Model.agentsSnapshot.runningCount,
                                    onAgentsClick = { showAgentsDrawer = true },
                                    onJetstreamClick = { showJetstream = true },
                                )
                            }
                        }
                        if (showJetstream) JetstreamPanel(onClose = { showJetstream = false })
                        // Right-edge swipe zone (100.005): only on non-Viewer
                        // screens - the Viewer has its own zone inside the desktop
                        // box, below the ">" tab, so the tab wins taps.
                        if (Model.screen != Screen.Viewer) DrawerEdgeZone(edge = DrawerEdge.Right, onOpenDrawer = { showAgentsDrawer = true })
                        AgentsRightSheet(
                            visible = showAgentsDrawer,
                            snapshot = Model.agentsSnapshot,
                            boardPosts = Model.boardPosts,
                            isLoading = Model.agentsLoading,
                            onClose = { showAgentsDrawer = false },
                            // 100.033 (message board): one Refresh
                            // re-fetches both tabs' data in one pass —
                            // fetchBoardPosts() rides the same
                            // runtime_stream fetch as the Agents tab
                            // (Model.fetchAgentsSnapshot()); no second
                            // fetch for the same data (spec §12 #10).
                            onRefresh = { Model.fetchBoardPosts() },
                        )
                    }
                }
                UpdateDialog()
            }
        }
    }

    override fun onStart() {
        super.onStart()
        AppUpdates.opened()
    }

    override fun onResume() {
        super.onResume()
        AppUpdates.resume(this)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        // 100.026 (§3.4, wiring A): the notification's Disconnect action
        // re-opens this singleTask activity with the disconnect extra (a tap
        // while the app is alive arrives via onNewIntent; a cold start via
        // onCreate — both funnel through here). End the session through the
        // existing path; never stop the service directly, which would leave
        // a headless stream. The LaunchedEffect hook then sees the
        // connection leave "Connected" and stops the service, which removes
        // the notification and releases the wake lock.
        if (isDisconnectIntent(intent)) {
            Model.leaveViewer()
            return
        }
        val uri = intent?.data ?: return
        handlePairUri(uri)
    }

    private fun handlePairUri(uri: Uri): Boolean {
        if (uri.scheme != "deskhand" || uri.host != "pair") return false
        val serial = uri.getQueryParameter("s") ?: return false
        // 100.015 (§11.2): the QR may carry t= (Tailscale) and l= (LAN)
        // alongside r=; Model builds the ordered candidate list [t, r, l].
        Model.openPair(serial, uri.getQueryParameter("r") ?: "", uri.getQueryParameter("t"), uri.getQueryParameter("l"))
        return true
    }

    private fun scanQr() {
        val options = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
        GmsBarcodeScanning.getClient(this, options).startScan()
            .addOnSuccessListener { code ->
                val uri = runCatching { Uri.parse(code.rawValue) }.getOrNull()
                if (uri == null || !handlePairUri(uri)) Model.pairStatus = "That QR code isn't a Deskhand pairing code."
            }
            .addOnFailureListener {
                Model.screen = Screen.Pair
                Model.pairStatus = "Couldn't scan. Type the code instead."
            }
    }
}

// 100.015 (§11.5): Home chip text for one paired PC, or null when no
// transport is known for it. Connected → "Connected via Tailscale";
// disconnected → the last-known transport ("Last connected via …").
private fun transportChipFor(pc: PairedPc): String? {
    val connected = Model.viewerPc?.id == pc.id && Model.viewerConnection == "Connected"
    if (connected && Model.transport != null) return Model.transport!!.connectedLabel()
    if (pc.id == Model.lastTransportPcId && Model.lastTransport != null) return "Last connected via ${Model.lastTransport!!.label()}"
    return null
}

@Composable
private fun HomeScreen(onScan: () -> Unit, agentsRunningCount: Int, onAgentsClick: () -> Unit) {
    val context = androidx.compose.ui.platform.LocalContext.current
    // 100.026 (§6.2): whether the notification permission has been asked
    // once — persisted, so the context line below (and the prompt) appear
    // on first use only, across restarts. Re-read on each Home entry.
    val notificationPermissionAsked = remember(context) {
        context.getSharedPreferences(
            DESKHAND_NOTIFICATION_PREFS,
            android.content.Context.MODE_PRIVATE,
        ).getBoolean(DESKHAND_NOTIFICATION_ASKED_KEY, false)
    }
    var confirmForget by remember { mutableStateOf<PairedPc?>(null) }
    var showChangelog by remember { mutableStateOf(false) }
    // 100.012 H11: manual relay-URL update dialog state.
    var relayEditPc by remember { mutableStateOf<PairedPc?>(null) }
    var relayEditText by remember { mutableStateOf("") }
    Column(
        Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("Deskhand", style = MaterialTheme.typography.headlineMedium)
        Text("v${BuildConfig.VERSION_NAME}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.clickable { showChangelog = true })
        // 100.016 (E14 diagnostics): full-screen Diagnostics board —
        // same navigation pattern as the version label opening the
        // changelog (a tap navigates to a full screen), not a sheet.
        // Never in the Agents drawer: the drawer is a half-screen sheet
        // for agents, and the rows need the full width for the error text.
        Row(
            Modifier.fillMaxWidth().clickable { Model.screen = Screen.Diagnostics }.testTag("diagnosticsRow").padding(vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text("Diagnostics", style = MaterialTheme.typography.bodyLarge)
            Text(">", style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        AgentsButton(runningCount = agentsRunningCount, onClick = onAgentsClick)
        TextButton(onClick = { AppUpdates.show() }) { Text("App updates") }
        val embeddedStatusColor = when {
            EmbeddedTailscale.status == "Running" -> DeskhandSuccess
            EmbeddedTailscale.status == "NeedsLogin" || EmbeddedTailscale.signInAvailable -> DeskhandWarningText
            EmbeddedTailscale.status == "Unavailable" -> DeskhandError
            else -> DeskhandTextSecondary
        }
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            StatusDot(embeddedStatusColor)
            Text("Embedded Tailscale: ${EmbeddedTailscale.status}", color = embeddedStatusColor)
        }
        TextButton(onClick = {
            (context as? android.app.Activity)?.getPreferences(android.content.Context.MODE_PRIVATE)?.edit()?.putBoolean("embedded-tailscale", true)?.apply()
            EmbeddedTailscale.start(context)
        }) { Text("Start embedded Tailscale") }
        if (EmbeddedTailscale.signInAvailable) EmbeddedTailscale.authUrl()?.let { url ->
            TextButton(onClick = { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }) { Text("Sign in to Tailscale") }
        }
        Text("Your PCs", style = MaterialTheme.typography.titleMedium)
        Model.pcsLoadError?.let { Text("Couldn't read paired PCs ($it). The saved file was kept, not deleted.", style = MaterialTheme.typography.bodySmall, color = DeskhandWarningText) }
        Model.pcsCorruptNote?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = DeskhandWarningText) }
        if (Model.pcs.isEmpty()) {
            Text("No PC paired yet. On your PC open Deskhand → Pair a phone, then scan the QR code here.")
        }
        Model.pcs.forEach { pc ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(pc.name, style = MaterialTheme.typography.titleMedium)
                    Text(if (pc.control) "View and control" else "View only", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    // 100.015 (§11.5): transport chip — the live transport
                    // for the connected PC, else the last-known one.
                    transportChipFor(pc)?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.testTag("transportChip")) }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = { Model.connect(pc) }, modifier = Modifier.heightIn(min = 48.dp), colors = ButtonDefaults.buttonColors(containerColor = DeskhandPrimary, contentColor = Color.White)) { Text("Connect") }
                        TextButton(onClick = { confirmForget = pc }) { Text("Unpair") }
                    }
                    // 100.026 (§6.2): one line of context before the
                    // first-connect permission prompt. Shown on first use
                    // only — until the permission has been asked once.
                    if (shouldShowConnectContextLine(notificationPermissionAsked)) {
                        Text(
                            "Deskhand shows a notification while connected so you can see the session is alive.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        // 100.012 H11: manual relay-URL refresh for a rotated tunnel.
                        TextButton(onClick = { relayEditPc = pc; relayEditText = pc.relay; Model.relayEditStatus = "" }) { Text("Update relay URL") }
                    }
                }
            }
        }
        Button(onClick = { Model.openPair(); onScan() }, modifier = Modifier.heightIn(min = 48.dp), colors = ButtonDefaults.buttonColors(containerColor = DeskhandPrimary, contentColor = Color.White)) { Text("Pair a PC (scan QR)") }
        OutlinedButton(onClick = { Model.openPair() }, modifier = Modifier.heightIn(min = 48.dp)) { Text("Pair with a typed code") }
        Text(
            "Connections are end-to-end encrypted. Forgetting a PC here does not revoke this phone on the PC — also remove it in Deskhand on the PC.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
    confirmForget?.let { pc ->
        AlertDialog(
            onDismissRequest = { confirmForget = null },
            title = { Text("Unpair ${pc.name}?") },
            text = { Text("You will need to pair again to use this PC.") },
            confirmButton = { TextButton(onClick = { Model.forget(pc); confirmForget = null }) { Text("Unpair") } },
            dismissButton = { TextButton(onClick = { confirmForget = null }) { Text("Cancel") } },
        )
    }
    relayEditPc?.let { pc ->
        AlertDialog(
            onDismissRequest = { relayEditPc = null },
            title = { Text("Update relay URL") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Paste the PC's current relay address (ws://… or wss://…). Use this when the PC's tunnel address changed and the phone keeps failing to connect.", style = MaterialTheme.typography.bodySmall)
                    OutlinedTextField(
                        value = relayEditText,
                        onValueChange = { relayEditText = it },
                        label = { Text("Relay address (ws://…)") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    if (Model.relayEditStatus.isNotEmpty()) Text(Model.relayEditStatus, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                }
            },
            confirmButton = { TextButton(onClick = { if (Model.updateRelay(pc, relayEditText)) relayEditPc = null }) { Text("Save") } },
            dismissButton = { TextButton(onClick = { relayEditPc = null }) { Text("Cancel") } },
        )
    }
    if (showChangelog) {
        AlertDialog(
            onDismissRequest = { showChangelog = false },
            title = { Text("Changelog", style = MaterialTheme.typography.titleLarge) },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState()).heightIn(max = 400.dp).padding(16.dp)) {
                    Text(CHANGELOG_TEXT, style = MaterialTheme.typography.bodySmall)
                }
            },
            confirmButton = { TextButton(onClick = { showChangelog = false }) { Text("Close") } }
        )
    }
}

@Composable
private fun PairScreen(onScan: () -> Unit) {
    BackHandler { Model.stopPair(); Model.screen = Screen.Home }
    Column(
        Modifier.fillMaxSize().safeDrawingPadding().padding(20.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Pair a PC", style = MaterialTheme.typography.headlineSmall)
        Text("Open Deskhand on your PC and choose Pair a phone. Scan the QR code or enter the pairing details below.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Button(onClick = onScan, enabled = !Model.pairBusy) { Text("Scan QR code") }
        OutlinedTextField(
            value = Model.pairSerial,
            onValueChange = { Model.pairSerial = it },
            label = { Text("Pairing code") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = Model.pairRelay,
            onValueChange = { Model.pairRelay = it },
            label = { Text("Relay address (ws://…)") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Button(onClick = { Model.startPair() }, enabled = !Model.pairBusy) { Text(if (Model.pairBusy) "Pairing…" else "Pair") }
        if (Model.pairCode.isNotEmpty()) {
            Text("Confirmation code: ${Model.pairCode}", style = MaterialTheme.typography.headlineSmall)
        }
        if (Model.pairStatus.isNotEmpty()) Text(Model.pairStatus)
        TextButton(onClick = { Model.stopPair(); Model.screen = Screen.Home }) { Text("Cancel") }
    }
}

private val keyButtons = listOf("esc", "tab", "enter", "backspace", "delete", "left", "up", "down", "right", "home", "end", "pageup", "pagedown")

@Composable
internal fun ViewerScreen(
    agentsRunningCount: Int = 0,
    onAgentsClick: () -> Unit = {},
    onJetstreamClick: (() -> Unit)? = null,
) {
    // Deskhand session service: keep the connection alive while the viewer is connected.
    val deskhandSessionContext = androidx.compose.ui.platform.LocalContext.current
    // 100.026 (§6): POST_NOTIFICATIONS is asked once, on first connect
    // only — never at launch, never again after a denial. The asked flag is
    // persisted, so a re-prompt cannot happen on a later session (or on a
    // later flap of the connection) however the Viewer recomposes.
    val notificationPrefs = remember(deskhandSessionContext) {
        deskhandSessionContext.getSharedPreferences(
            DESKHAND_NOTIFICATION_PREFS,
            android.content.Context.MODE_PRIVATE,
        )
    }
    var notificationPermissionAsked by remember {
        mutableStateOf(notificationPrefs.getBoolean(DESKHAND_NOTIFICATION_ASKED_KEY, false))
    }
    var notificationPermissionInFlight by remember { mutableStateOf(false) }
    // Same read-only check as DiagnosticsScreen.kt:336.
    val notificationsGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
        ContextCompat.checkSelfPermission(deskhandSessionContext, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
    val notificationPermissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        notificationPermissionInFlight = false
        notificationPermissionAsked = true
        notificationPrefs.edit().putBoolean(DESKHAND_NOTIFICATION_ASKED_KEY, true).apply()
        // Spec §6.3: start the service either way — never block a
        // connection on this permission. Only if still connected: the
        // answer can arrive after a disconnect.
        if (Model.viewerConnection == "Connected") {
            setDeskhandSessionService(deskhandSessionContext, true, Model.viewerHost)
        }
    }
    LaunchedEffect(Model.viewerConnection) {
        if (Model.viewerConnection == "Connected") {
            if (shouldAskNotificationPermission(notificationPermissionAsked, notificationsGranted, notificationPermissionInFlight, Build.VERSION.SDK_INT)) {
                notificationPermissionInFlight = true
                notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            } else {
                setDeskhandSessionService(deskhandSessionContext, true, Model.viewerHost)
            }
        } else {
            setDeskhandSessionService(deskhandSessionContext, false)
        }
    }
    // 100.026 (§3.3): Viewer-driven elapsed-time refresh — re-call the
    // service at most once per 5 minutes while connected. The service
    // rebuilds the notification text from its stored sessionStartedAt
    // without resetting it. No timer lives in the service.
    LaunchedEffect(Model.viewerConnection) {
        while (Model.viewerConnection == "Connected") {
            kotlinx.coroutines.delay(DESKHAND_SESSION_REFRESH_INTERVAL_MS)
            if (Model.viewerConnection == "Connected") {
                setDeskhandSessionService(deskhandSessionContext, true, Model.viewerHost)
            }
        }
    }

    // 100.016 (E14): the 2.5 s poll loop moved to the activity level (see
    // setContent) so it also runs while the Diagnostics screen is open.
    val approval = Model.pendingApprovals.firstOrNull()
    if (approval != null) {
        AssistantApprovalDialog(approval, { Model.resolveApproval(approval.optString("id"), it) }, { Model.resolveApproval(approval.optString("id"), false) })
    }
    var showKeys by remember { mutableStateOf(false) }
    // 100.049 (§1.5): frame-arrival interval measurement for the Connected
    // pill. Frame interval, NEVER ping/latency/RTT — there is no latency
    // source in the app; the number shown is the EMA of inter-frame gaps.
    var frameIntervalMs by remember { mutableStateOf<Long?>(null) }
    var lastFrameAtMs by remember { mutableStateOf<Long?>(null) }
    var lastFrameWallMs by remember { mutableStateOf<Long?>(null) }
    // Re-runs on every frame assignment — that is the measurement point,
    // not a bug. Kept cheap: EMA math only. SystemClock.uptimeMillis()
    // (monotonic) for intervals — never wall-clock; lastFrameWallMs is the
    // wall-clock time alongside the uptime time, for the sheet's Last OK row.
    LaunchedEffect(Model.frame) {
        val now = android.os.SystemClock.uptimeMillis()
        val prev = lastFrameAtMs
        if (Model.frame != null && prev != null && Model.viewerConnection == "Connected") {
            val interval = now - prev
            if (interval in 1..10_000) { // ignore clock jumps / first-frame gaps
                frameIntervalMs = ((frameIntervalMs?.toDouble() ?: interval.toDouble()) * 0.8 + interval * 0.2).toLong()
            }
        }
        if (Model.frame != null) { lastFrameAtMs = now; lastFrameWallMs = System.currentTimeMillis() }
        else { frameIntervalMs = null; lastFrameAtMs = null; lastFrameWallMs = null }
    }
    LaunchedEffect(Model.viewerConnection) {
        if (Model.viewerConnection != "Connected") { frameIntervalMs = null; lastFrameAtMs = null; lastFrameWallMs = null }
    }
    val landscape = LocalConfiguration.current.orientation == Configuration.ORIENTATION_LANDSCAPE
    var portraitScale by rememberSaveable { mutableStateOf(DesktopScale.Stretch) }
    var landscapeScale by remember { mutableStateOf(DesktopScale.Stretch) }
    var rotationPending by remember { mutableStateOf(false) }
    LaunchedEffect(landscape) {
        rotationPending = true
        Model.setViewerLandscape(landscape)
        delay(1800)
        rotationPending = false
    }
    // 100.013: fit-to-screen is the default at every stream start — when
    // the viewer (re)connects, both scale states go to Fit. The rotation
    // effect above no longer forces Stretch, so fit survives rotation.
    LaunchedEffect(Model.viewerConnection) {
        if (Model.viewerConnection == "Connected") {
            landscapeScale = DesktopScale.Fit
            portraitScale = DesktopScale.Fit
        }
    }
    val scale = if (landscape) landscapeScale else portraitScale
    // 100.011: session zoom state, keyed on the paired PC id — a new PC
    // starts fresh (its own entry), the same PC keeps the live view across
    // rotation (the zoom factor is never reset by a geometry change, spec
    // §7; DesktopView's geometry effect re-clamps the pan instead).
    // Pan/Control mode is per session, default Control (spec §4.2).
    val zoomState = remember(Model.viewerPc?.id) { DesktopZoomState() }
    var panMode by remember(Model.viewerPc?.id) { mutableStateOf(false) }
    // The PC the "Zoom for this PC" row edits; null while not connected.
    val connectedPc = if (Model.viewerConnection == "Connected") Model.viewerPc else null
    var chatPanel by rememberSaveable { mutableStateOf<String?>(null) }
    var selectedMessage by rememberSaveable { mutableStateOf<String?>(null) }
    val viewerConnectionColor = when (Model.viewerConnection) {
        "Connected" -> DeskhandSuccess
        "Connecting", "Reconnecting" -> DeskhandTextSecondary
        else -> DeskhandWarningText
    }
    // 100.026 (§6.3): denied-permission banner — Viewer only, while
    // connected, and only after the one ask has happened (before the first
    // ask there is no banner). The settings link is the only path after a
    // denial; this banner never re-prompts. The grant is re-checked live
    // on each recomposition (same check as DiagnosticsScreen.kt:336), so a
    // revoke in Settings shows the banner and a grant hides it.
    val showNotificationDeniedBanner = shouldShowNotificationDeniedBanner(
        Model.viewerConnection == "Connected",
        notificationPermissionAsked,
        notificationsGranted,
        Build.VERSION.SDK_INT,
    )
    Column(Modifier.fillMaxSize()) {
    if (showNotificationDeniedBanner) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp).testTag("notificationDeniedBanner"),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                "Notifications off — you won't see the connection status.",
                style = MaterialTheme.typography.bodySmall,
                color = DeskhandWarningText,
                modifier = Modifier.weight(1f),
            )
            TextButton(
                onClick = {
                    val settingsIntent = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).apply {
                        putExtra(Settings.EXTRA_APP_PACKAGE, deskhandSessionContext.packageName)
                        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    }
                    runCatching { deskhandSessionContext.startActivity(settingsIntent) }
                },
                modifier = Modifier.testTag("notificationSettingsButton"),
            ) { Text("Turn on") }
        }
    }
    // 100.049 (§1.5): relay URL + host for the Connected pill's tap sheet.
    // relayHost is parsed the same way PcLink.kt does it.
    val relayUrl = Model.viewerPc?.relay.orEmpty()
    val relayHost = runCatching { java.net.URI(relayUrl.trim()).host }.getOrNull().orEmpty()
    ViewerLayout(
        modifier = Modifier.weight(1f),
        host = Model.viewerHost,
        connection = Model.viewerConnection,
        transportText = Model.transportText,
        frameIntervalMs = frameIntervalMs,
        lastFrameAtMs = lastFrameAtMs,
        lastFrameWallMs = lastFrameWallMs,
        relayUrl = relayUrl,
        relayHost = relayHost,
        control = Model.viewerControl,
        browser = Model.desktopBrowser,
        aspect = Model.viewerAspect,
        composerStatus = when {
            Model.chatStopStatus != null -> Model.chatStopStatus
            Model.chatDeliveryUncertain -> "Check chat history"
            Model.chatSending -> "Sending…"
            else -> null
        },
        onDisconnect = Model::leaveViewer,
        stopEnabled = Model.viewerConnection == "Connected",
        onEmergencyStop = Model::stopStreamAndRestoreLandscape,
        // 100.050: foreground-window state for the top-right toolbar.
        foregroundWindowEnabled = Model.foregroundWindow.enabled,
        foregroundWindowBusy = Model.foregroundWindow.busy,
        onForegroundWindowAction = Model.foregroundWindow::act,
        agentsRunningCount = agentsRunningCount,
        onAgentsClick = onAgentsClick,
        onJetstreamClick = onJetstreamClick,
        desktop = { interactive, chromeVisible, onDesktopTap ->
            val bmp = Model.frame
            val stream: @Composable () -> Unit = {
                val previousOrientationFrame = bmp != null && rotationPending && (bmp.width > bmp.height) != landscape
                if (bmp != null) DesktopView(bmp, zoomState, scale = if (interactive && !previousOrientationFrame) scale else DesktopScale.Fit, interactive = interactive,
                    canControl = Model.viewerControl && Model.viewerConnection == "Connected", panMode = panMode, onDesktopTap = onDesktopTap)
                else Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.padding(16.dp).verticalScroll(rememberScrollState())) {
                    StatusDot(viewerConnectionColor)
                    Text(Model.viewerStatus, color = viewerConnectionColor)
                }
            }
            if (interactive) DesktopStreamOverlay(Model.foregroundWindow.notice ?: Model.desktopBack.notice, content = stream,
                bubbles = { bubbleModifier -> ChatBubbleLane(Model.desktopChat, bubbleModifier) { message ->
                    selectedMessage = message.key; chatPanel = "history"
                } })
            else stream()
        },
        composer = {
            DesktopChatComposer(Model.chatDraft, { Model.chatDraft = it }, Model.chatSending, Model.canSendChat,
                Model::sendChat, onSettings = { chatPanel = "settings" },
                onHistory = { selectedMessage = null; chatPanel = "history" },
                active = Model.chatActive, canStop = Model.chatConnected && !Model.chatStopPending && !Model.restartPending,
                onStop = Model::restartChat,
                voiceOn = Model.voiceReplies, onVoiceToggle = { Model.applyVoiceReplies(!Model.voiceReplies) })
        },
    )
    }
    DesktopChatPanels(chatPanel, selectedMessage, extraSettings = {
        Text("Desktop", style = MaterialTheme.typography.titleMedium)
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            StatusDot(viewerConnectionColor)
            Text("${Model.viewerHost} · ${Model.viewerConnection} · ${if (Model.viewerControl) "Control enabled" else "View only"}", style = MaterialTheme.typography.bodySmall, color = viewerConnectionColor)
        }
        DesktopViewControls(scale, zoomState, onScale = { mode ->
            if (landscape) landscapeScale = mode else portraitScale = mode
            // 100.011: no zoom reset here — changing Fit/Stretch changes the
            // base size only; the factor is preserved and the pan re-clamps
            // via DesktopView's geometry effect (same rule as rotation, §7).
        })
        // 100.011 Zoom controls (spec §3.3, §4, §10). There is no settings
        // screen in this app, so the Zoom section lives in this settings
        // panel, next to the Fit/Stretch control. Row A edits THIS PC's
        // zoom (connected only); row B edits the global default (always).
        // The labels never let the two be confused.
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = { zoomState.applyFit(!zoomState.fit) }, modifier = Modifier.heightIn(min = 48.dp)) {
                Text(if (zoomState.fit) "Fit ✓" else "Fit")
            }
            TextButton(onClick = { panMode = !panMode }, modifier = Modifier.heightIn(min = 48.dp)) {
                Text(if (panMode) "Mode: Pan" else "Mode: Control")
            }
            if (connectedPc != null) {
                Text("Zoom for ${connectedPc.name}:")
                TextButton(onClick = {
                    zoomState.applyFit(false)
                    zoomState.change(clampZoom(zoomState.zoom - 0.25f, Model.zoomCap))
                    zoomState.persist()
                }, modifier = Modifier.heightIn(min = 48.dp)) { Text("−") }
                Text("%.2f×".format(zoomState.zoom))
                TextButton(onClick = {
                    zoomState.applyFit(false)
                    zoomState.change(clampZoom(zoomState.zoom + 0.25f, Model.zoomCap))
                    zoomState.persist()
                }, modifier = Modifier.heightIn(min = 48.dp)) { Text("+") }
            }
        }
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), verticalAlignment = Alignment.CenterVertically) {
            Text("Default zoom:")
            TextButton(onClick = { Model.setGlobalDefaultZoom(Model.globalDefaultZoom() - 0.25f) }, modifier = Modifier.heightIn(min = 48.dp)) { Text("−") }
            Text("%.2f×".format(Model.globalDefaultZoom()))
            TextButton(onClick = { Model.setGlobalDefaultZoom(Model.globalDefaultZoom() + 0.25f) }, modifier = Modifier.heightIn(min = 48.dp)) { Text("+") }
            TextButton(onClick = { Model.updateContrastEnhance(!Model.contrastEnhance) }, modifier = Modifier.heightIn(min = 48.dp)) {
                Text(if (Model.contrastEnhance) "Sharpen: On" else "Sharpen: Off")
            }
            if (connectedPc != null) {
                TextButton(onClick = {
                    Model.resetZoomForCurrentPc()
                    zoomState.restore(null, Model.globalDefaultZoom())
                }, modifier = Modifier.heightIn(min = 48.dp)) { Text("Reset zoom") }
            }
        }
        if (Model.viewerControl) {
            TextButton(onClick = { showKeys = !showKeys }) { Text(if (showKeys) "Hide keys" else "Special keys") }
            if (showKeys) Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
                keyButtons.forEach { key -> OutlinedButton(onClick = { Model.sendInput(JSONObject().put("t", "key").put("k", key)) }) { Text(key) } }
            }
        }
        TextButton(onClick = { Model.desktopBack.closeLatest() }, enabled = Model.desktopBack.enabled && !Model.desktopBack.busy) { Text("Close last opened window") }
        TextButton(onClick = { chatPanel = null; Model.leaveViewer() }) { Text("Disconnect") }
        Text("Assistant", style = MaterialTheme.typography.titleMedium)
        if (Model.chatStopStatus != null) {
            Text(Model.chatStatus, style = MaterialTheme.typography.bodySmall, color = DeskhandWarningText)
            TextButton(onClick = Model::resumeChat, enabled = Model.chatConnected && !Model.chatStopPending) { Text("Resume chat") }
        }
        Model.assistantTasks.firstOrNull()?.let { task ->
            val taskId = task.optString("taskId")
            val prompt = Model.desktopChat.messages.firstOrNull { it.turnId == task.optString("turnId") && it.role == "user" }?.text
            val waiting = Model.pendingApprovals.any { it.optString("taskId") == taskId }
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                StatusDot(if (waiting) DeskhandWarningText else DeskhandSuccess)
                Text("Active task · ${task.optString("model")} · ${if (waiting) "Waiting for approval" else "Working"}", style = MaterialTheme.typography.bodySmall, color = if (waiting) DeskhandWarningText else DeskhandSuccess)
            }
            if (!prompt.isNullOrBlank()) Text(prompt, style = MaterialTheme.typography.bodySmall, maxLines = 3, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
        }
        if (Model.assistantActions.isNotEmpty()) {
            Text("Recent AI actions", style = MaterialTheme.typography.titleSmall)
            Model.assistantActions.take(5).forEach { action ->
                Text("${action.optString("Name")} · ${action.optString("Status")}", style = MaterialTheme.typography.bodySmall)
                action.optString("Error").takeIf { it.isNotBlank() && it != "null" }?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = DeskhandError) }
            }
        }
    }, onDismiss = { chatPanel = null })

}

@Composable
internal fun DesktopView(bmp: ImageBitmap, zoomState: DesktopZoomState, scale: DesktopScale = DesktopScale.Fit,
    interactive: Boolean = true, canControl: Boolean = true, panMode: Boolean = false, send: (JSONObject) -> Unit = Model::sendInput,
    onDesktopTap: () -> Unit = {}) {
    var box by remember { mutableStateOf(IntSize.Zero) }
    val aspect = bmp.width.toFloat() / bmp.height
    val size = desktopBaseSize(box, aspect, scale)
    val fitW = size.width
    val fitH = size.height
    val deviceId = Model.viewerPc?.id
    // 100.011 session-start restore (spec §8.3): factor now, pan when
    // geometry lands. The preview (interactive=false) never restores.
    LaunchedEffect(deviceId) {
        if (!interactive || deviceId == null) return@LaunchedEffect
        zoomState.restore(Model.zoomEntryFor(deviceId), Model.globalDefaultZoom())
        zoomState.applyFit(true)
    }
    // 100.011 geometry hook (spec §7): on rotation / scale / frame change,
    // note the geometry, re-clamp the pan to the new frame, and apply a
    // pending restored pan. The zoom factor is never touched here — the old
    // LaunchedEffect(box, scale) { zoomState.reset() } reset it on every
    // rotation, which is the bug this replaces.
    // B2-G B5: keyed on pendingEntry itself, so a restore is consumed
    // exactly once as soon as it lands — the old keys (box/size/frame)
    // often did not change on a PC switch, and the entry was silently
    // lost (or applied late over the user's pan on the next rotation).
    LaunchedEffect(box, size, bmp.width, bmp.height, zoomState.pendingEntry) {
        if (!interactive) return@LaunchedEffect
        zoomState.lastBox = box
        zoomState.lastSize = size
        zoomState.lastBmp = IntSize(bmp.width, bmp.height)
        zoomState.geometry(box, size)
        val pending = zoomState.pendingEntry
        if (pending != null && box.width > 0 && box.height > 0) {
            val restored = restoreZoomEntry(pending, Model.globalDefaultZoom(), bmp.width, bmp.height)
            zoomState.change(restored.zoom, if (restored.panCentre != null) {
                sourceToPan(restored.panCentre, bmp.width, bmp.height, box, size, restored.zoom, bmp.width, bmp.height)
            } else Offset.Zero)
            zoomState.pendingEntry = null
        }
    }
    // Draw zoom/pan: fit mode and the non-interactive preview both draw at
    // 1.0x centred; otherwise the effective (cap-clamped) zoom and live pan.
    val zoom = if (!interactive || zoomState.fit) ZOOM_FIT else effectiveZoom(zoomState.zoom, Model.zoomCap)
    val pan = if (!interactive || zoomState.fit) Offset.Zero else zoomState.pan
    val shownW = fitW * zoom
    val shownH = fitH * zoom
    val left = box.width / 2f + pan.x - shownW / 2f
    val top = box.height / 2f + pan.y - shownH / 2f
    var magnifierPoint by remember { mutableStateOf<Offset?>(null) }
    // Contrast/sharpen pass (spec §6.1, default ON). A subtle contrast +
    // saturation lift via RenderEffect (API 31+); below 31 the pass is a
    // no-op and the setting has no visual effect. The effect applies to the
    // composited canvas, i.e. after the zoom transform.
    val contrastEffect = remember(Model.contrastEnhance) {
        if (Model.contrastEnhance && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val matrix = ColorMatrix()
            matrix.setSaturation(1.03f)
            val contrast = ColorMatrix(floatArrayOf(
                1.06f, 0f, 0f, 0f, -7.68f,
                0f, 1.06f, 0f, 0f, -7.68f,
                0f, 0f, 1.06f, 0f, -7.68f,
                0f, 0f, 0f, 1f, 0f,
            ))
            matrix.postConcat(contrast)
            RenderEffect.createColorFilterEffect(ColorMatrixColorFilter(matrix)).asComposeRenderEffect()
        } else null
    }

    // 100.042 (BUILD-SPEC §1 decision 5): capture the tap callback via
    // rememberUpdatedState — its identity changes on every recomposition, so
    // it must NOT be a pointerInput key (that would restart the gesture block).
    val currentOnDesktopTap by rememberUpdatedState(onDesktopTap)

    Box(Modifier.fillMaxSize().clipToBounds()) {
    Canvas(
        Modifier.fillMaxSize().testTag("desktopTouchSurface").clipToBounds().onSizeChanged { box = it }
            .graphicsLayer { renderEffect = contrastEffect }
            // zoomState is a key: the gesture block must restart with the
            // new session state when the PC changes, or it would keep
            // driving the previous PC's DesktopZoomState.
            .pointerInput(fitW, fitH, interactive, scale, canControl, panMode, zoomState) {
            if (!interactive || fitW <= 0f) return@pointerInput
            // Read the live state (not the composition-time draw values):
            // the gesture block is not restarted when the zoom changes.
            fun point(p: Offset): Offset? {
                val z = if (zoomState.fit) ZOOM_FIT else effectiveZoom(zoomState.zoom, Model.zoomCap)
                val pn = if (zoomState.fit) Offset.Zero else zoomState.pan
                return desktopPoint(p, box, size, z, pn)
            }
            fun move(p: Offset): Boolean {
                val normalized = point(p) ?: return false
                send(JSONObject().put("t", "move").put("x", normalized.x.toDouble()).put("y", normalized.y.toDouble()))
                return true
            }
            fun button(down: Boolean, right: Boolean) {
                val o = JSONObject().put("t", if (down) "down" else "up")
                if (right) o.put("b", "r")
                send(o)
            }
            awaitEachGesture {
                val first = awaitFirstDown(requireUnconsumed = false)
                val slop = viewConfiguration.touchSlop
                var panning = false
                var dragging = false
                var didPinch = false
                var lastPanPersistMs = 0L
                var precise = false
                var cancelled = false
                var released = false
                var multiTouch = false
                var scrolling = false
                var pinching = false
                var scrollAcc = 0f
                var spanAcc = 0f
                var lastSpan = 0f
                var lastCentroid = Offset.Zero
                var lastPos = first.position
                var lastTime = first.uptimeMillis
                try {
                while (true) {
                    val remaining = (viewConfiguration.longPressTimeoutMillis - (lastTime - first.uptimeMillis)).coerceAtLeast(1L)
                    val event = if (!precise && !panning && !multiTouch) withTimeoutOrNull(remaining) { awaitPointerEvent() } else awaitPointerEvent()
                    if (event == null) {
                        precise = true
                        magnifierPoint = lastPos.takeIf { point(it) != null }
                        continue
                    }
                    if (event.changes.any { it.isConsumed }) { cancelled = true; break }
                    val pressed = event.changes.filter { it.pressed }
                    lastTime = event.changes.maxOf { it.uptimeMillis }
                    if (pressed.size >= 2) {
                        multiTouch = true
                        magnifierPoint = null
                        // A second finger cancels any in-progress drag or
                        // pan; a remote drag must never survive into a pinch.
                        if (dragging) { button(false, false); dragging = false }
                        panning = false
                        val a = pressed[0].position
                        val b = pressed[1].position
                        val span = (a - b).getDistance()
                        val centroid = (a + b) / 2f
                        if (!scrolling && !pinching) {
                            if (lastSpan == 0f) { lastSpan = span; lastCentroid = centroid }
                            spanAcc += span - lastSpan
                            scrollAcc += centroid.y - lastCentroid.y
                            lastSpan = span
                            lastCentroid = centroid
                            if (abs(spanAcc) > slop * 2) pinching = true
                            else if (abs(scrollAcc) > slop * 2) { scrolling = true; scrollAcc = 0f }
                        } else if (pinching) {
                            // 100.011: pinch range is [ZOOM_MIN, cap] (spec
                            // §3.1). 1.0x is zoom-to-fit only (fit toggle) —
                            // never by pinching, never stored. A pinch exits
                            // fit mode.
                            if (zoomState.fit) zoomState.applyFit(false)
                            didPinch = true
                            val previousZoom = zoomState.zoom
                            val newZoom = (previousZoom * span / lastSpan).coerceIn(ZOOM_MIN, Model.zoomCap)
                            val ratio = newZoom / previousZoom
                            val c = Offset(box.width / 2f, box.height / 2f)
                            // keep the point between the fingers fixed while zooming, and follow the fingers when panning
                            val newPan = (zoomState.pan - (lastCentroid - c)) * ratio + (centroid - c)
                            zoomState.change(newZoom, newPan)
                            lastSpan = span
                            lastCentroid = centroid
                        } else {
                            scrollAcc += pressed.map { it.positionChange().y }.average().toFloat()
                            if (abs(scrollAcc) >= 12f) {
                                send(JSONObject().put("t", "scroll").put("dy", (scrollAcc * 6.0).coerceIn(-1200.0, 1200.0)))
                                scrollAcc = 0f
                            }
                        }
                    } else if (!multiTouch && !scrolling && !pinching && pressed.size == 1) {
                        val pos = pressed[0].position
                        if (precise) {
                            magnifierPoint = pos.takeIf { point(it) != null }
                        } else if (!panning && !dragging && (pos - first.position).getDistance() > slop) {
                            // 100.011 (spec §4): Pan mode drags the view,
                            // Control mode drags the remote mouse. A tap
                            // clicks in both modes. Pinch never becomes a
                            // remote drag. View-only (canControl false) has
                            // no remote mouse to drive, so its drag pans.
                            if (zoomState.fit) zoomState.applyFit(false)
                            if (panMode || !canControl) {
                                panning = true
                            } else {
                                move(first.position)
                                button(true, false)
                                dragging = true
                            }
                        } else if (dragging) {
                            move(pos)
                        } else if (panning) {
                            zoomState.change(zoomState.zoom, zoomState.pan + (pos - lastPos))
                            // Debounced persist while dragging (spec §8.4):
                            // at most one write per second, plus one on release.
                            val now = System.currentTimeMillis()
                            if (now - lastPanPersistMs >= 1000L) {
                                zoomState.persist()
                                lastPanPersistMs = now
                            }
                        }
                        lastPos = pos
                    }
                    val finger = event.changes.firstOrNull { it.id == first.id }
                    if (finger != null && !finger.pressed && finger.previousPressed) {
                        lastPos = finger.position
                        released = true
                    }
                    event.changes.forEach { it.consume() }
                    if (event.changes.none { it.pressed }) break
                }

                // 100.011 post-gesture: a remote drag always releases its
                // button (even if the gesture was cancelled, so the PC never
                // holds a stuck button); a pan persists once on release; a
                // tap clicks (never after a drag — wasDragging excludes the
                // click, or a Control-mode drag would end with a spurious
                // click); a pinch snaps to 0.05 and writes the entry.
                val wasDragging = dragging
                val wasPanning = panning
                if (wasDragging) {
                    button(false, false)
                } else if (wasPanning) {
                    zoomState.persist()
                }
                // B2-G B3: a single tap clicks, as the changelog promises
                // ("Tap always clicks in both modes"). The old double-tap
                // tracker silently discarded the first tap, so a single tap
                // did nothing. A double-tap now sends two clicks (one per
                // tap), like a physical mouse.
                if (!cancelled && released && !wasDragging && !wasPanning && !multiTouch && !scrolling && !pinching) {
                    // Tap shows chrome AND clicks the PC (100.042, spec §5.4): double duty, deliberate.
                    currentOnDesktopTap()
                    if (canControl && move(lastPos)) {
                        button(true, false)
                        button(false, false)
                        Model.checkRemoteTextFocus()
                    }
                }
                if (didPinch) {
                    // Pinch release (spec §8.4): snap to the nearest 0.05 and
                    // write the entry. The cap is re-applied after snapping so
                    // a snap can never lift the zoom above a live Low-mode cap.
                    zoomState.change(clampZoom(snapZoom(zoomState.zoom), Model.zoomCap))
                    zoomState.persist()
                }
                } finally { magnifierPoint = null }
            }
        },
    ) {
        drawImage(bmp, dstOffset = IntOffset(left.toInt(), top.toInt()), dstSize = IntSize(shownW.toInt(), shownH.toInt()))
    }
    magnifierPoint?.let { target ->
        Canvas(Modifier.fillMaxSize().testTag("preciseMagnifier")) {
            val radius = minOf(52.dp.toPx(), size.width / 2f, size.height / 2f)
            if (radius > 0f) {
                val center = magnifierCenter(target, box, radius, 24.dp.toPx())
                val direction = target - center
                val distance = direction.getDistance()
                if (distance > 0.01f) {
                    val unit = direction / distance
                    val perpendicular = Offset(-unit.y, unit.x)
                    val anchor = center + unit * (radius - 1.dp.toPx())
                    val halfWidth = 5.dp.toPx()
                    val pointer = Path().apply {
                        moveTo((anchor + perpendicular * halfWidth).x, (anchor + perpendicular * halfWidth).y)
                        lineTo((anchor + unit * 9.dp.toPx()).x, (anchor + unit * 9.dp.toPx()).y)
                        lineTo((anchor - perpendicular * halfWidth).x, (anchor - perpendicular * halfWidth).y)
                        close()
                    }
                    drawPath(pointer, Color.Black.copy(alpha = 0.78f))
                }
                drawCircle(Color.Black.copy(alpha = 0.10f), radius + 4.dp.toPx(), center + Offset(0f, 3.dp.toPx()))
                drawCircle(Color.Black.copy(alpha = 0.16f), radius + 2.dp.toPx(), center + Offset(0f, 2.dp.toPx()))
                val lens = Path().apply { addOval(androidx.compose.ui.geometry.Rect(center, radius)) }
                clipPath(lens) {
                    drawCircle(Color.Black, radius, center)
                    val factor = 2.5f
                    val x = center.x - (target.x - left.toInt().toFloat()) * factor
                    val y = center.y - (target.y - top.toInt().toFloat()) * factor
                    drawImage(bmp, dstOffset = IntOffset(x.toInt(), y.toInt()),
                        dstSize = IntSize((shownW.toInt() * factor).toInt().coerceAtLeast(1), (shownH.toInt() * factor).toInt().coerceAtLeast(1)))
                }
                drawCircle(Color.Black.copy(alpha = 0.88f), radius, center, style = Stroke(2.dp.toPx()))
                drawCircle(Color.White.copy(alpha = 0.9f), (radius - 1.dp.toPx()).coerceAtLeast(0f), center, style = Stroke(1.dp.toPx()))
                val cross = 9.dp.toPx()
                drawLine(Color.Black, center - Offset(cross, 0f), center + Offset(cross, 0f), 3.dp.toPx())
                drawLine(Color.Black, center - Offset(0f, cross), center + Offset(0f, cross), 3.dp.toPx())
                drawLine(Color.White, center - Offset(cross, 0f), center + Offset(cross, 0f), 1.dp.toPx())
                drawLine(Color.White, center - Offset(0f, cross), center + Offset(0f, cross), 1.dp.toPx())
            }
        }
    }
    }
}

// 100.026 (§6, §3.4): notification-permission + Disconnect-extra helpers.
// Pure functions (no Android calls except Intent.getBooleanExtra), so the
// ask-once / banner / disconnect rules are unit-testable without a device.
internal const val DESKHAND_NOTIFICATION_PREFS = "deskhand-notifications"
internal const val DESKHAND_NOTIFICATION_ASKED_KEY = "permission_asked"
internal const val DESKHAND_DISCONNECT_EXTRA = "deskhand_disconnect"

// 100.026 (§3.3): Viewer-driven elapsed-time refresh cadence — the service
// is re-called at most this often while connected.
internal const val DESKHAND_SESSION_REFRESH_INTERVAL_MS = 5 * 60 * 1000L

/**
 * True when the notification intent extra says to disconnect the session
 * (the notification's Disconnect action, spec §3.4 wiring A).
 */
internal fun isDisconnectIntent(intent: Intent?): Boolean =
    intent?.getBooleanExtra(DESKHAND_DISCONNECT_EXTRA, false) == true

/**
 * Ask the POST_NOTIFICATIONS permission only on first connect: Android 13+
 * only, never asked before, not already granted, and no request already in
 * flight. A denial sets asked=true, so this never returns true again.
 */
internal fun shouldAskNotificationPermission(
    askedBefore: Boolean,
    granted: Boolean,
    requestInFlight: Boolean,
    sdkInt: Int,
): Boolean = sdkInt >= Build.VERSION_CODES.TIRAMISU &&
    !askedBefore && !granted && !requestInFlight

/**
 * Show the denied-permission banner only while a session is live, on
 * Android 13+, after the one ask has happened, and while the permission is
 * currently denied (re-checked live, so a revoke in Settings shows it too).
 * Before the first ask there is no banner.
 */
internal fun shouldShowNotificationDeniedBanner(
    connected: Boolean,
    askedBefore: Boolean,
    granted: Boolean,
    sdkInt: Int,
): Boolean = connected && sdkInt >= Build.VERSION_CODES.TIRAMISU &&
    askedBefore && !granted

/** One line of context under Connect, on first use only (before the ask). */
internal fun shouldShowConnectContextLine(askedBefore: Boolean): Boolean = !askedBefore

// Deskhand session service (added 2026-11-08): keeps the live desktop
// connection alive while the viewer is connected. Top-level (not a
// MainActivity member) so the ViewerScreen composable can call it with
// LocalContext.current. The service itself is DeskhandSessionService.kt.
internal fun setDeskhandSessionService(
    context: android.content.Context,
    active: Boolean,
    pcName: String = ""
) {
    if (active) {
        DeskhandSessionService.start(context, pcName)
    } else {
        DeskhandSessionService.stop(context)
    }
}
