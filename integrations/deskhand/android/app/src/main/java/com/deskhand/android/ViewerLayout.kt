package com.deskhand.android

import androidx.activity.compose.BackHandler
import android.content.Context
import android.view.inputmethod.InputMethodManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.core.*
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.foundation.background
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.shape.GenericShape
import androidx.compose.ui.res.painterResource
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.customActions
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

internal val ViewerColors = darkColorScheme(
    primary = Color(0xFFD0BCFF), onPrimary = Color(0xFF381E72),
    background = Color(0xFF141218), onBackground = Color(0xFFE6E0E9),
    surface = Color(0xFF1D1B20), onSurface = Color(0xFFE6E0E9),
    surfaceVariant = Color(0xFF49454F), onSurfaceVariant = Color(0xFFCAC4D0),
)

@Composable
internal fun DeskhandViewerTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = ViewerColors, typography = DeskhandViewerTypography, content = content)
}

// Red stop-sign button (Phil, 100.013 patch): a small octagon, the real
// stop-sign shape, under the window controls at the top right, replacing
// the big red "Stop stream" button. Same action, quieter presence.
private val StopSignShape = GenericShape { size, _ ->
    val w = size.width
    val h = size.height
    moveTo(w * 0.9619f, h * 0.6913f)
    lineTo(w * 0.6913f, h * 0.9619f)
    lineTo(w * 0.3087f, h * 0.9619f)
    lineTo(w * 0.0381f, h * 0.6913f)
    lineTo(w * 0.0381f, h * 0.3087f)
    lineTo(w * 0.3087f, h * 0.0381f)
    lineTo(w * 0.6913f, h * 0.0381f)
    lineTo(w * 0.9619f, h * 0.3087f)
    close()
}

// C1 (100.042): auto-hide delay for the viewer chrome — chrome fades away
// this long after the last interaction.
private const val ChromeAutoHideDelayMs = 3000L

// B2-G: ViewerTab.Files / ViewerTab.Assistant removed — they were never
// exposed in navigation (normalised away to Desktop) and had no tab UI.
// The single value stays so a saved tab in Activity state still reads.
internal enum class ViewerTab { Desktop }
internal fun normalizedViewerTab(tab: ViewerTab) = ViewerTab.Desktop

@Composable
internal fun ViewerLayout(
    host: String, connection: String, control: Boolean, browser: DesktopBrowserController, aspect: Float,
    onDisconnect: () -> Unit, desktop: @Composable (interactive: Boolean, chromeVisible: Boolean, onDesktopTap: () -> Unit) -> Unit,
    composer: @Composable () -> Unit,
    modifier: Modifier = Modifier, imeInsets: WindowInsets = WindowInsets.ime,
    initialTab: ViewerTab = ViewerTab.Desktop, composerStatus: String? = null,
    stopEnabled: Boolean = false, onEmergencyStop: () -> Unit = {},
    // 100.050: foreground-window state for the top-right toolbar.
    foregroundWindowEnabled: Boolean = false, foregroundWindowBusy: Boolean = false,
    onForegroundWindowAction: (String) -> Unit = {},
    agentsRunningCount: Int = 0, onAgentsClick: () -> Unit = {},
    onJetstreamClick: (() -> Unit)? = null,
    transportText: String = "",
    frameIntervalMs: Long? = null,
    lastFrameAtMs: Long? = null,
    lastFrameWallMs: Long? = null,
    relayUrl: String = "",
    relayHost: String = "",
) {
    var savedTab by rememberSaveable { mutableStateOf(initialTab) }
    val tab = normalizedViewerTab(savedTab)
    var drawerOpen by rememberSaveable { mutableStateOf(false) }
    var drawerFraction by rememberSaveable { mutableFloatStateOf(0.5f) }
    // C1 (100.042): chrome visibility + the single auto-hide timer. remember,
    // NOT rememberSaveable — rotation must restore chrome visible.
    var chromeVisible by remember { mutableStateOf(true) }
    // 100.049: the pill's connection-details sheet.
    var showConnectionSheet by remember { mutableStateOf(false) }
    var lastInteractionMs by remember { mutableLongStateOf(System.currentTimeMillis()) }
    fun noteChromeInteraction() {
        chromeVisible = true
        lastInteractionMs = System.currentTimeMillis()
    }
    val openedCount = browser.state.openedCount
    var previousOpenedCount by remember { mutableIntStateOf(openedCount) }
    LaunchedEffect(openedCount) { if (openedCount > previousOpenedCount) drawerOpen = false; previousOpenedCount = openedCount }
    val focus = LocalFocusManager.current
    val context = LocalContext.current
    val remoteKeyboard = remember { RemoteKeyboard(context) }
    fun openRemoteKeyboard() {
        if (control && connection == "Connected") {
            remoteKeyboard.requestFocus()
            (context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager).showSoftInput(remoteKeyboard, InputMethodManager.SHOW_IMPLICIT)
        }
    }
    LaunchedEffect(Model.remoteKeyboardRequest) {
        if (Model.remoteKeyboardRequest > 0) openRemoteKeyboard()
    }
    LaunchedEffect(control, connection) {
        if (!control || connection != "Connected") {
            Model.invalidateRemoteTextFocus()
            remoteKeyboard.clearFocus()
            (context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager).hideSoftInputFromWindow(remoteKeyboard.windowToken, 0)
        }
    }
    val imeVisible = imeInsets.getBottom(LocalDensity.current) > 0
    // C1 timer (100.042): hide chrome 3 s after the last interaction, unless
    // a drawer is open or the keyboard is up. Exactly one timer.
    // 100.049 §1.4: the open connection sheet also suspends the timer —
    // chrome must not auto-hide underneath it. noteChromeInteraction()
    // alone only resets the timer; it does not suspend it.
    LaunchedEffect(lastInteractionMs, drawerOpen, imeVisible, showConnectionSheet) {
        if (!drawerOpen && !imeVisible && !showConnectionSheet) {
            delay(ChromeAutoHideDelayMs)
            chromeVisible = false
        }
    }
    LaunchedEffect(imeVisible) {
        if (imeVisible) noteChromeInteraction()
    }
    LaunchedEffect(connection) {
        if (connection == "Connected") noteChromeInteraction()
    }
    // B2-G: the expanded-desktop / showControls branches are gone with the
    // dead controls slot — Back now only closes the drawer, else disconnects.
    BackHandler(enabled = !imeVisible) {
        when {
            drawerOpen -> drawerOpen = false
            else -> onDisconnect()
        }
    }
    val connectionColor = when (connection) {
        "Connected" -> DeskhandDarkSuccess
        "Connecting", "Reconnecting" -> DeskhandDarkTextSecondary
        else -> DeskhandDarkWarningText
    }
    Surface(modifier.fillMaxSize()) {
        BoxWithConstraints(Modifier.fillMaxSize()) {
            AndroidView(factory = { remoteKeyboard }, modifier = Modifier.size(1.dp))
            val landscape = maxWidth > maxHeight
            val viewingDesktop = tab == ViewerTab.Desktop
            Column(Modifier.fillMaxSize().windowInsetsPadding(imeInsets)
                .testTag(if (landscape && viewingDesktop) "landscapeViewer" else "viewerLayout")) {
                if (viewingDesktop) {
                    BoxWithConstraints(Modifier.weight(1f).fillMaxWidth()) {
                        Box(Modifier.fillMaxSize().background(Color.Black)
                            .testTag("primaryDesktop"), Alignment.Center) { desktop(true, chromeVisible) { noteChromeInteraction() } }
                        val drawerWidth = maxWidth * drawerFraction
                        val widthPixels = with(LocalDensity.current) { maxWidth.toPx() }
                        Box(Modifier.align(Alignment.CenterStart).width(drawerWidth).fillMaxHeight()) {
                            AnimatedFilesDrawer(drawerOpen, browser, drawerWidth)
                        }
                        if (drawerOpen) {
                            Box(Modifier.align(Alignment.CenterStart).offset(x = drawerWidth - 12.dp)
                                .width(24.dp).fillMaxHeight().testTag("drawerResize")
                                .semantics {
                                    contentDescription = "Resize files drawer"
                                    customActions = listOf(
                                        CustomAccessibilityAction("Make drawer wider") { drawerFraction = (drawerFraction + 0.1f).coerceAtMost(0.85f); true },
                                        CustomAccessibilityAction("Make drawer narrower") { drawerFraction = (drawerFraction - 0.1f).coerceAtLeast(0.4f); true })
                                }
                                .pointerInput(widthPixels) { detectHorizontalDragGestures { change, drag ->
                                    noteChromeInteraction(); change.consume(); drawerFraction = (drawerFraction + drag / widthPixels).coerceIn(0.4f, 0.85f)
                                } }) {
                                Box(Modifier.align(Alignment.TopCenter).padding(top = 32.dp).width(3.dp).height(72.dp).background(MaterialTheme.colorScheme.primary.copy(alpha = 0.7f), MaterialTheme.shapes.small))
                            }
                        }
                        val arrowOffset by animateDpAsState(if (drawerOpen) drawerWidth else 0.dp, label = "drawer arrow")
                        // Left-edge swipe zone (100.005): swipe right from the left
                        // edge opens the file drawer - the tap-only tab's swipe
                        // counterpart. Placed before the tab so the tab wins taps.
                        if (!drawerOpen) DrawerEdgeZone(edge = DrawerEdge.Left, onOpenDrawer = { noteChromeInteraction(); drawerOpen = true })
                        Box(Modifier.align(Alignment.CenterStart).offset(x = arrowOffset).size(width = 48.dp, height = 56.dp)
                            .testTag("filesDrawerToggle")
                            .semantics { contentDescription = if (drawerOpen) "Close files drawer" else "Open files drawer" }
                            .clickable { noteChromeInteraction(); focus.clearFocus(); drawerOpen = !drawerOpen }, contentAlignment = Alignment.CenterStart) {
                            androidx.compose.animation.AnimatedVisibility(visible = chromeVisible, enter = fadeIn(tween(220)), exit = fadeOut(tween(220))) {
                                // 100.043 (C2): files toggle is glass — same 30x48 size, same 12.dp end radii.
                                GlassSurface(Modifier.width(30.dp).height(48.dp), shape = RoundedCornerShape(topEnd = 12.dp, bottomEnd = 12.dp)) {
                                    Box(contentAlignment = Alignment.Center) {
                                        if (drawerOpen) Text("‹", style = MaterialTheme.typography.titleMedium)
                                        else Icon(painterResource(R.drawable.file_type_folder), contentDescription = null, modifier = Modifier.size(22.dp), tint = MaterialTheme.colorScheme.primary)
                                    }
                                }
                            }
                        }
                        // Agents drawer tab (100.004): mirrors the files-drawer
                        // toggle on the left. The app auto-connects into the
                        // Viewer on launch, so the drawer needs a way in from
                        // here too — tap the tab, or swipe left from the right
                        // edge (DrawerEdgeZone, just above this tab).
                        // Right-edge swipe zone (100.005): swipe left from the right
                        // edge opens the agents drawer. Placed before the tab so the
                        // tab wins taps and the zone still wins swipes.
                        DrawerEdgeZone(edge = DrawerEdge.Right, onOpenDrawer = { noteChromeInteraction(); onAgentsClick() })
                        Box(Modifier.align(Alignment.CenterEnd).size(width = 48.dp, height = 56.dp)
                            .testTag("agentsDrawerToggle")
                            .semantics { contentDescription = "Open agents drawer" }
                            .clickable { noteChromeInteraction(); focus.clearFocus(); onAgentsClick() }, contentAlignment = Alignment.CenterEnd) {
                            androidx.compose.animation.AnimatedVisibility(visible = chromeVisible, enter = fadeIn(tween(220)), exit = fadeOut(tween(220))) {
                                // 100.043 (C2): agents toggle is glass — same 30x48 size, same 12.dp end radii.
                                GlassSurface(Modifier.width(30.dp).height(48.dp), shape = RoundedCornerShape(topStart = 12.dp, bottomStart = 12.dp)) {
                                    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center,
                                        modifier = Modifier.fillMaxSize()) {
                                        Text("›", style = MaterialTheme.typography.titleMedium)
                                        if (agentsRunningCount > 0) Text("$agentsRunningCount", style = MaterialTheme.typography.labelSmall,
                                            color = MaterialTheme.colorScheme.primary)
                                    }
                                }
                            }
                        }
                        // 100.049 (C2): frosted-glass pill. GlassSurface is defined by
                        // 100.043 (frosted glass) — do NOT define it here.
                        androidx.compose.animation.AnimatedVisibility(visible = chromeVisible && !drawerOpen, enter = fadeIn(tween(220)), exit = fadeOut(tween(220)), modifier = Modifier.align(Alignment.TopStart)) {
                            GlassSurface(
                                modifier = Modifier.align(Alignment.TopStart).statusBarsPadding().padding(6.dp),
                                shape = MaterialTheme.shapes.small
                            ) {
                                Row(
                                    Modifier
                                        .horizontalScroll(rememberScrollState())
                                        .clickable { noteChromeInteraction(); showConnectionSheet = true }
                                        .testTag("connectionPill")
                                        .semantics { contentDescription = "Connection details" }
                                        .padding(horizontal = 8.dp, vertical = 2.dp),
                                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    // 100.049: pulsing dot, Connected only. 100.047 (C5 motion)
                                    // may retune the pulse duration.
                                    if (connection == "Connected") PulsingStatusDot(connectionColor)
                                    else StatusDot(connectionColor)
                                    Text(connection, style = MaterialTheme.typography.labelSmall,
                                        color = connectionColor)
                                    // 100.049: frame-arrival interval — the v1 number. This is
                                    // frame interval, NOT ping/latency/RTT (no such source exists).
                                    if (connection == "Connected" && frameIntervalMs != null) Text("$frameIntervalMs ms frame", style = MaterialTheme.typography.labelSmall,
                                        color = connectionColor)
                                    // 100.015 (§11.5): "Connected via …" / "Trying …" /
                                    // "No transport …" — the live transport, next to
                                    // the connection state. Hidden when idle.
                                    if (transportText.isNotEmpty()) Text(transportText, style = MaterialTheme.typography.labelSmall,
                                        color = connectionColor, modifier = Modifier.testTag("transportIndicator"))
                                    composerStatus?.let { Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary) }
                                }
                            }
                        }
                        // 100.050 (§5.4): the top-right cluster is one glass pill now —
                        // window buttons + stop octagon, octagon LAST. GlassSurface is
                        // defined by 100.043 (frosted glass) — do NOT define it here.
                        androidx.compose.animation.AnimatedVisibility(visible = chromeVisible && !drawerOpen, enter = fadeIn(tween(220)), exit = fadeOut(tween(220)), modifier = Modifier.align(Alignment.TopEnd)) {
                            GlassSurface(
                                modifier = Modifier.align(Alignment.TopEnd).statusBarsPadding().padding(top = 56.dp, end = 4.dp),
                                shape = MaterialTheme.shapes.medium
                            ) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    onJetstreamClick?.let { openJobs ->
                                        TextButton(onClick = { noteChromeInteraction(); openJobs() },
                                            modifier = Modifier.semantics { contentDescription = "Jetstream jobs" }) { Text("Jobs") }
                                    }
                                    // 100.050 (§5.4): the onAction wrap — every toolbar tap
                                    // resets the C1 auto-hide timer first. The wrap lives
                                    // here, in ViewerLayout, where noteChromeInteraction()
                                    // is local; do NOT reach it from ViewerScreen and do
                                    // NOT create a second timer.
                                    ForegroundWindowButtons(foregroundWindowEnabled, foregroundWindowBusy, { action -> noteChromeInteraction(); onForegroundWindowAction(action) })
                                    // Stop octagon LAST, moved verbatim from the standalone
                                    // Box (only parent + offset changed): same shape, size,
                                    // red, testTag, contentDescription, action, gating.
                                    Box(modifier = Modifier.size(40.dp)
                                        .background((if (stopEnabled) Color(0xFFE53935) else Color(0xFFE53935).copy(alpha = 0.38f)), StopSignShape)
                                        .clickable(enabled = stopEnabled) { noteChromeInteraction(); onEmergencyStop() }
                                        .testTag("stopStream")
                                        .semantics { contentDescription = "Stop stream" }
                                    ) {}
                                }
                            }
                        }
                    }
                    // 100.050 fix round (composer always visible, item 74): the
                    // composer is NOT chrome — it is the only way to read and
                    // write messages, so it renders unconditionally. The C1 idle
                    // fade hid it 3 s after the last touch while reply bubbles
                    // live only ~6.82 s, and history is reachable only through
                    // the Chat history button inside the composer — after the
                    // fade there was no persistent chat surface.
                    Box(Modifier.fillMaxWidth().windowInsetsPadding(WindowInsets.navigationBars).padding(horizontal = 4.dp, vertical = 2.dp).testTag("desktopComposer")) { composer() }

                }
            }
        }
    }
    // 100.049: connection details sheet, rendered while open. The sheet is
    // NOT chrome — it does not fade with C1 — and its dismiss restarts the
    // auto-hide timer from dismiss (see §1.4).
    if (showConnectionSheet) {
        ConnectionDetailsSheet(
            connection = connection,
            connectionColor = connectionColor,
            host = host,
            relayUrl = relayUrl,
            relayHost = relayHost,
            lastFrameWallMs = lastFrameWallMs,
            frameIntervalMs = frameIntervalMs,
            onDismiss = { showConnectionSheet = false; noteChromeInteraction() }
        )
    }
}

// 100.049: pulsing status dot, Connected only. 100.047 (C5 motion) may
// retune the pulse duration. StatusDot itself is untouched.
@Composable
private fun PulsingStatusDot(color: Color) {
    val pulseTransition = rememberInfiniteTransition(label = "connectionPulse")
    val pulseAlpha by pulseTransition.animateFloat(
        initialValue = 1.0f, targetValue = 0.45f,
        animationSpec = infiniteRepeatable(
            animation = tween(1200), repeatMode = RepeatMode.Reverse
        ), label = "connectionPulseAlpha"
    )
    Box(Modifier.alpha(pulseAlpha)) { StatusDot(color) }
}

// 100.049: tap sheet for the Connected pill. Plain Text rows, current type
// values — 100.044/100.046 will restyle. The number shown is the
// frame-arrival interval, labelled as such — never as ping/latency/RTT
// (no such source exists in the app).
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun ConnectionDetailsSheet(
    connection: String,
    connectionColor: Color,
    host: String,
    relayUrl: String,
    relayHost: String,
    lastFrameWallMs: Long?,
    frameIntervalMs: Long?,
    onDismiss: () -> Unit
) {
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ) {
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                StatusDot(connectionColor)
                Text("Connection", style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(connection, style = MaterialTheme.typography.bodyMedium)
            }
            ConnectionSheetRow(label = "PC", value = host)
            ConnectionSheetRow(
                label = "Relay",
                value = if (relayUrl.isEmpty()) "Not paired" else relayUrl,
                valueStyle = MaterialTheme.typography.labelSmall,
                valueMaxLines = 2
            )
            ConnectionSheetRow(
                label = if (relayHost.firstOrNull()?.isDigit() == true) "Embedded IP" else "Relay host",
                value = if (relayHost.isEmpty()) "—" else relayHost
            )
            ConnectionSheetRow(
                label = "Last frame OK",
                value = lastFrameWallMs?.let {
                    SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(it))
                } ?: "No frame yet"
            )
            ConnectionSheetRow(
                label = "Frame interval",
                value = frameIntervalMs?.let { "$it ms" } ?: "—"
            )
        }
    }
}

@Composable
private fun ConnectionSheetRow(
    label: String,
    value: String,
    valueStyle: TextStyle = MaterialTheme.typography.bodyMedium,
    valueMaxLines: Int = Int.MAX_VALUE
) {
    Column(Modifier.fillMaxWidth()) {
        Text(label, style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, style = valueStyle, maxLines = valueMaxLines,
            overflow = if (valueMaxLines < Int.MAX_VALUE) TextOverflow.Ellipsis else TextOverflow.Clip)
    }
}

@Composable
private fun AnimatedFilesDrawer(open: Boolean, browser: DesktopBrowserController, width: androidx.compose.ui.unit.Dp) {
    AnimatedVisibility(visible = open, enter = slideInHorizontally { -it }, exit = slideOutHorizontally { -it }) {
        DesktopBrowserView(browser, Modifier.width(width).fillMaxHeight())
    }
}
