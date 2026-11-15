package com.deskhand.android

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.abs

/**
 * Right-side agents drawer (v100.005 — faithful copy of the file
 * explorer drawer, mirrored to the right).
 *
 * The 100.004 drawer approximated the file panel (fixed 320dp width,
 * explicit tween slide, big filled Refresh pill). Phil confirmed
 * ~00:29 EST it did not match: the file drawer is about half the
 * screen wide and its rows/text buttons read differently. This build
 * copies the file drawer's own component instead of approximating it:
 *
 * - WIDTH: the file drawer is `maxWidth * drawerFraction` (0.5f
 *   default, 0.4–0.85, resizable by dragging its inner edge) in
 *   ViewerLayout.kt. The agents drawer uses the same fraction of the
 *   same window width, the same default, the same range, and the same
 *   resize strip (24dp straddling the inner edge, 3x72dp grip bar).
 * - SLIDE: the file drawer is `AnimatedVisibility` with
 *   `slideInHorizontally { -it }` / `slideOutHorizontally { -it }`
 *   and NO explicit animation spec and NO fade. The agents drawer
 *   uses the identical call with the sign mirrored (`{ it }`) — same
 *   default spec, same smoothness, in from the right edge and out to
 *   the right. The scrim alone fades; the drawer itself never fades.
 * - LAYOUT: the panel content copies DesktopBrowserView's structure
 *   exactly — Surface, Column with 8dp horizontal padding, a top row
 *   of text buttons (Refresh / Close), the title, an OutlinedTextField
 *   search box that filters the list, status lines, then entries as
 *   plain rows (48dp min height, 20dp icon slot, titleSmall name +
 *   labelSmall second line). No filled Refresh pill, no cards.
 * - OPEN: tap the "›" tab or swipe left from the right edge. The tab
 *   tap failed in 100.004 because the activity-level edge zone sat
 *   above the tab in z-order; the zone now lives inside the Viewer's
 *   desktop box, BELOW the tab, so the tab wins taps and the zone
 *   still wins swipes. The file drawer gets the same treatment on the
 *   left edge (swipe right from the left edge opens it).
 *
 * State is hoisted to the activity level (100.004 fix): the app
 * auto-connects into the Viewer on launch, so a Home-only button is
 * never seen. The sheet stays composed and is driven by `visible`
 * (so the exit slide animates); the runtimes tick once a second
 * while it is open. Tapping the dimmed area or Close dismisses.
 *
 * Live feed: WIRED in 100.017 (C13 push + the absorbed 100.018 Part B
 * fetch): Model.fetchAgentsSnapshot() polls the `runtime_stream` op
 * while the drawer is open, and push frames over the relay socket append
 * events the moment they land (the 1 s poll stays as a dormant fallback
 * that resumes when push goes quiet). Cards come from `subagent.*`
 * events only — none exist yet, so 0 cards is the honest state — and
 * `turn_` / `file_` events render as one latest-activity footer line,
 * never as cards. Never fake agents.
 */

/** Small "Agents • N" button for the Home header; opens the drawer. */
@Composable
fun AgentsButton(runningCount: Int, onClick: () -> Unit) {
    TextButton(onClick = onClick) {
        Text(
            text = if (runningCount > 0) "Agents • $runningCount running" else "Agents",
            style = MaterialTheme.typography.bodyMedium,
            color = DeskhandPrimary,
        )
    }
}

/** Which edge a drawer opens from. */
// Public, not internal: the public DrawerEdgeZone takes it as a
// parameter, and Kotlin forbids a public function from exposing an
// internal type.
enum class DrawerEdge { Left, Right }

/**
 * Shared system-gesture exclusion rects for the edge swipe zones.
 * Both zones write their own slot and apply the union, so the left
 * and right edges stay excluded together instead of one overwriting
 * the other when it recomposes.
 */
private object EdgeGestureExclusions {
    var left: android.graphics.Rect? = null
    var right: android.graphics.Rect? = null
    fun apply(view: android.view.View) {
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
            view.systemGestureExclusionRects = listOfNotNull(left, right)
        }
    }
}

/**
 * Invisible edge swipe zone that opens a drawer (100.005 copy).
 *
 * A 28dp strip on one edge of its parent box, full height. A swipe
 * starting there and travelling 56dp toward the screen centre opens
 * the drawer: leftward on the right edge (agents), rightward on the
 * left edge (files). Only drags in the opening direction are
 * consumed — taps, vertical drags, and drags the wrong way pass
 * through untouched, so the desktop stream keeps working. The strip
 * is excluded from Android's system back gesture so an edge swipe
 * opens the drawer instead of leaving the app.
 *
 * Place the zone BEFORE the drawer's tab in the same parent box, so
 * the tab (later sibling) wins taps in its band and the zone still
 * wins swipes everywhere else on the edge. The zone reports its exact
 * window rect via onGloballyPositioned, so the exclusion matches
 * wherever the zone is placed (activity level or Viewer desktop box).
 */
@Composable
fun BoxScope.DrawerEdgeZone(edge: DrawerEdge, onOpenDrawer: () -> Unit) {
    val view = LocalView.current
    var zoneRect by remember { mutableStateOf<android.graphics.Rect?>(null) }
    LaunchedEffect(zoneRect) {
        if (edge == DrawerEdge.Left) EdgeGestureExclusions.left = zoneRect
        else EdgeGestureExclusions.right = zoneRect
        EdgeGestureExclusions.apply(view)
    }
    DisposableEffect(edge) {
        onDispose {
            if (edge == DrawerEdge.Left) EdgeGestureExclusions.left = null
            else EdgeGestureExclusions.right = null
            EdgeGestureExclusions.apply(view)
        }
    }
    Box(
        Modifier
            .align(if (edge == DrawerEdge.Left) Alignment.CenterStart else Alignment.CenterEnd)
            .width(28.dp)
            .fillMaxHeight()
            .onGloballyPositioned { coords ->
                val r = coords.boundsInWindow()
                zoneRect = android.graphics.Rect(r.left.toInt(), r.top.toInt(), r.right.toInt(), r.bottom.toInt())
            }
            .pointerInput(edge, onOpenDrawer) {
                val slopPx = 24.dp.toPx()
                val openPx = 56.dp.toPx()
                awaitEachGesture {
                    val down = awaitFirstDown(requireUnconsumed = false)
                    var totalX = 0f
                    var totalY = 0f
                    var draggingOpen = false
                    while (true) {
                        val event = awaitPointerEvent()
                        val change = event.changes.firstOrNull { it.id == down.id } ?: break
                        if (!change.pressed) break
                        totalX += change.position.x - change.previousPosition.x
                        totalY += change.position.y - change.previousPosition.y
                        if (!draggingOpen) {
                            if (abs(totalX) > slopPx && abs(totalX) > abs(totalY)) {
                                val opening = if (edge == DrawerEdge.Left) totalX > 0f else totalX < 0f
                                if (opening) draggingOpen = true else break
                            } else if (abs(totalY) > slopPx) break
                        } else {
                            change.consume()
                            val farEnough = if (edge == DrawerEdge.Left) totalX >= openPx else totalX <= -openPx
                            if (farEnough) {
                                onOpenDrawer()
                                break
                            }
                        }
                        if (event.changes.none { it.pressed }) break
                    }
                }
            }
    )
}

/**
 * The drawer itself — a faithful copy of the file drawer's
 * AnimatedFilesDrawer + DesktopBrowserView, mirrored to the right.
 * Render UNCONDITIONALLY at the activity level in MainActivity
 * .setContent and drive it with `visible` (the exit slide only
 * animates if the sheet stays composed while it closes):
 *
 *   AgentsRightSheet(
 *       visible = showAgentsDrawer, snapshot = agentsSnapshot,
 *       boardPosts = Model.boardPosts,
 *       isLoading = agentsLoading,
 *       onClose = { showAgentsDrawer = false },
 *   )
 *
 * 100.033 (message board, spec §7.2): the same sheet gains a Board tab
 * (Agents | Board, tab bar under the header). The Agents tab keeps the
 * existing cards byte-for-byte; the Board tab shows boardPosts
 * newest-first with an origin chip on every post. One Refresh re-fetches
 * both tabs' data in one pass (the shared runtime_stream fetch).
 *
 * `nowMs` ticks once a second while the sheet is open so the elapsed
 * runtimes count up live. The search box filters the list by task or
 * chat text, exactly like the file drawer's "Search this folder".
 */
@Composable
fun AgentsRightSheet(
    visible: Boolean,
    snapshot: AgentsSnapshot,
    boardPosts: List<BoardPost> = emptyList(),
    isLoading: Boolean,
    onClose: () -> Unit,
    // 100.017: onRefresh is Model.fetchAgentsSnapshot() (MainActivity
    // wires it) — the Refresh button, the open, and the 5 s drawer loop
    // all fetch through it. The default stays a no-op so previews and
    // tests can render the sheet without a session.
    onRefresh: () -> Unit = {},
) {
    var nowMs by remember { mutableStateOf(System.currentTimeMillis()) }
    var query by rememberSaveable { mutableStateOf("") }
    var drawerFraction by rememberSaveable { mutableFloatStateOf(0.5f) }
    // 100.033 (message board, spec §7.2): Agents | Board tabs, one
    // drawer. The dot on Board means a post newer than the last time the
    // Board tab was opened; opening the tab marks it seen (and while the
    // tab is open, arriving posts are seen as they land).
    var boardTab by rememberSaveable { mutableStateOf(false) }
    val boardNewestTsMs = boardPosts.maxOfOrNull { it.tsMs } ?: 0L
    val boardHasNew = boardNewestTsMs > Model.boardLastSeenTsMs
    LaunchedEffect(boardTab, boardNewestTsMs) {
        if (boardTab && boardNewestTsMs > 0L) Model.boardMarkSeen(boardNewestTsMs)
    }
    // Tick + fetch only while open; the effect restarts (and the old
    // loop is cancelled) when `visible` flips.
    LaunchedEffect(visible) {
        if (visible) {
            onRefresh()
            // 100.017 (C13 push + 100.018 design §B3): the 1 s tick stays,
            // but the fetch follows the transport — dormant while push is
            // live, an immediate fetch the moment push drops, then every
            // 5th tick (5 s) while push stays down. The effect is
            // cancelled when `visible` flips, so nothing fetches while the
            // drawer is closed.
            var tick = 0
            var wasPushLive = Model.streamTransport == StreamTransport.LIVE_PUSH
            while (true) {
                delay(1000)
                nowMs = System.currentTimeMillis()
                tick++
                Model.refreshStreamTransport(nowMs)
                val pushLive = Model.streamTransport == StreamTransport.LIVE_PUSH
                val justWentDown = wasPushLive && !pushLive
                wasPushLive = pushLive
                if (!pushLive && (justWentDown || tick % 5 == 0)) onRefresh()
            }
        }
    }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        // Same width as the file drawer: the same fraction of the same
        // window width (side safe insets are 0 on this phone — the
        // reference photo puts the file panel's edge at ~50%).
        val drawerWidth = maxWidth * drawerFraction
        val widthPixels = with(LocalDensity.current) { maxWidth.toPx() }
        // Scrim: fade only. The drawer itself slides, copied exactly
        // from the file drawer — it never fades.
        AnimatedVisibility(
            visible = visible,
            enter = fadeIn(),
            exit = fadeOut(),
        ) {
            Box(
                Modifier
                    .fillMaxSize()
                    .background(MaterialTheme.colorScheme.scrim.copy(alpha = 0.4f))
                    .clickable { onClose() }
            )
        }
        // Panel: slide only, default spec — the file drawer's
        // AnimatedVisibility call with the sign mirrored (`{ it }` =
        // in from the right edge, out to the right).
        AnimatedVisibility(
            visible = visible,
            enter = slideInHorizontally { it },
            exit = slideOutHorizontally { it },
            modifier = Modifier.align(Alignment.CenterEnd),
        ) {
            DeskhandViewerTheme {
                Surface(Modifier.width(drawerWidth).fillMaxHeight()) {
                    Column(Modifier.fillMaxSize().padding(horizontal = 8.dp)) {
                        Row(
                            Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                        ) {
                            TextButton(onClick = onRefresh, enabled = !isLoading) { Text("Refresh") }
                            TextButton(onClick = onClose) { Text("Close") }
                        }
                        // 100.033: tab bar under the header — Agents |
                        // Board. One Refresh (above) re-fetches both tabs'
                        // data in one pass (the shared runtime_stream
                        // fetch). The dot on Board means a post newer than
                        // the last time the Board tab was opened.
                        Row(
                            Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            TextButton(onClick = { boardTab = false }, modifier = Modifier.testTag("boardTabAgents")) {
                                Text(
                                    text = "Agents",
                                    color = if (!boardTab) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                            TextButton(onClick = { boardTab = true }, modifier = Modifier.testTag("boardTabBoard")) {
                                Text(
                                    text = if (boardHasNew) "Board ●" else "Board",
                                    color = if (boardTab) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                        Text(
                            text = if (boardTab) "Message board" else "Muse agents",
                            style = MaterialTheme.typography.titleMedium,
                            color = MaterialTheme.colorScheme.onSurface,
                        )
                        if (!boardTab) {
                            OutlinedTextField(
                                value = query,
                                onValueChange = { query = it },
                                singleLine = true,
                                placeholder = { Text("Search agents", maxLines = 1, style = MaterialTheme.typography.bodySmall) },
                                modifier = Modifier.fillMaxWidth().testTag("agentsSearch"),
                            )
                        }
                        if (!boardTab) {
                            Text(
                                text = if (snapshot.runningCount > 0) "${snapshot.runningCount} running" else "No agents running",
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurface,
                            )
                        }
                        Text(
                            text = formatUpdatedAgo(snapshot.updatedAtMs, nowMs),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        // 100.017 (C13 push, spec §6.2): TWO facts, never
                        // one blended claim — transport first, worker
                        // second. Push live does NOT mean the worker is up
                        // (the relay socket is host↔phone; the gateway
                        // worker is a different session).
                        Text(
                            text = streamTransportText(Model.streamTransport, nowMs, Model.streamLastDeliveryMs),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.testTag("agentsTransportLine"),
                        )
                        Text(
                            text = workerFooterLine(Model.healthSnapshot),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.testTag("agentsWorkerLine"),
                        )
                        // 100.037 (E12 watchdog, spec §7.4): the SECOND
                        // worker line, directly under the relay-based one
                        // above — transport first, worker second, never
                        // one blended claim. The line above is the phone
                        // link (health snapshot's `worker` row); this one
                        // is the Muse worker, derived from `worker.*`
                        // stream events (Model.museWorkerState). No
                        // Reconnect button here in v1 (spec §10.3).
                        Text(
                            text = museWorkerFooterLine(Model.museWorkerState, nowMs),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.testTag("agentsMuseWorkerLine"),
                        )
                        // 100.029 (E41 structured logging, spec §6.3):
                        // runtime-stream footer — health + idle age +
                        // configured level per source (agent, lifecycle,
                        // bridge, tunnel), in that order. One Text per
                        // line, same style as the transport/worker lines
                        // above. Same snapshot object those lines use
                        // (Model.healthSnapshot); nowMs / 1000 is the
                        // epoch-second idle-age clock. A down source is
                        // named as down and never carries a stale age
                        // dressed up as fresh (see sourceFooterLine).
                        sourceFooterLines(Model.healthSnapshot, nowMs / 1000).forEachIndexed { index, line ->
                            Text(
                                text = line,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                modifier = Modifier.testTag("agentsSourceFooterLine${SOURCE_FOOTER_KEYS[index]}"),
                            )
                        }
                        // 100.018 design §C3: warn when the fetch is stale
                        // (>120 s, as before) OR when agents are shown and
                        // no agent event has arrived in 15 min (the feed is
                        // fetching but the data is stale).
                        val fetchStale = snapshot.updatedAtMs > 0 && nowMs - snapshot.updatedAtMs > 120_000
                        val dataStale = snapshot.totalCount > 0 && snapshot.newestEventTsMs > 0 &&
                            nowMs - snapshot.newestEventTsMs > 900_000
                        if (fetchStale || dataStale) {
                            Text(
                                text = if (fetchStale) "Feed is stale — data may be over 2 minutes old."
                                else "No agent activity for over 15 minutes — the feed is fetching but nothing new has arrived.",
                                style = MaterialTheme.typography.bodySmall,
                                color = DeskhandDarkWarningText,
                                modifier = Modifier
                                    .padding(top = 8.dp)
                                    .background(DeskhandDarkWarningSurface, RoundedCornerShape(8.dp))
                                    .padding(8.dp)
                                    .fillMaxWidth(),
                            )
                        }
                        val q = query.trim()
                        fun matches(agent: MuseAgent): Boolean =
                            q.isEmpty() || agent.task.contains(q, ignoreCase = true) ||
                                (agent.chatLabel?.contains(q, ignoreCase = true) == true)
                        val running = snapshot.running.filter(::matches)
                        val finished = snapshot.finished.filter(::matches)
                        // 100.033: the chain below is the Agents tab —
                        // the Board tab renders in the else branch after it.
                        if (!boardTab) {
                        if (snapshot.totalCount == 0 && !isLoading) {
                            // 100.018 design §C3 honesty matrix: three
                            // situations, three texts — never one collapsed
                            // "nothing yet". `turn_` / `file_` activity is a
                            // footer line under it, never a card (§C2).
                            Text(
                                text = when (snapshot.vmEventsSource) {
                                    "error" -> "Agent feed has bad data — ${snapshot.skippedBadLines} lines skipped."
                                    "ok" -> if (snapshot.latestActivity != null) "No agents running"
                                    else "Nothing to show yet. Open the drawer again after the agents feed is connected — the count and runtimes appear here automatically."
                                    else -> "Agents feed not connected — no subagent data yet."
                                },
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            snapshot.latestActivity?.let { activity ->
                                Text(
                                    text = "Last activity: $activity",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.padding(top = 8.dp),
                                )
                            }
                        } else if (running.isEmpty() && finished.isEmpty()) {
                            Text(
                                text = "No matching agents.",
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        } else {
                            LazyColumn(Modifier.weight(1f).fillMaxWidth().testTag("agentsItems")) {
                                items(running, key = { it.id }) { agent ->
                                    AgentRow(agent = agent, nowMs = nowMs, isRunning = true)
                                }
                                if (finished.isNotEmpty()) {
                                    item {
                                        Text(
                                            text = "Recently finished",
                                            style = MaterialTheme.typography.titleSmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                                            modifier = Modifier.padding(top = 8.dp),
                                        )
                                    }
                                    items(finished, key = { it.id }) { agent ->
                                        AgentRow(agent = agent, nowMs = nowMs, isRunning = false)
                                    }
                                }
                            }
                            // 100.018 design §C2: the newest turn_/file_
                            // event as one footer line under the lists —
                            // never a card.
                            snapshot.latestActivity?.let { activity ->
                                Text(
                                    text = "Last activity: $activity",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.padding(top = 8.dp),
                                )
                            }
                        }
                        } else {
                            // 100.033 (message board, spec §7.2): posts
                            // newest-first. Empty states never fake posts;
                            // each missing leg is named in the footer (§5.3).
                            if (boardPosts.isEmpty()) {
                                Text(
                                    text = "Nothing here yet — posts appear as agents start and finish work.",
                                    style = MaterialTheme.typography.bodyMedium,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            } else {
                                LazyColumn(Modifier.weight(1f).fillMaxWidth().testTag("boardItems")) {
                                    items(boardPosts, key = { it.id }) { post ->
                                        BoardPostRow(post = post, nowMs = nowMs)
                                    }
                                }
                            }
                            Text(
                                text = boardFooterLine(boardPosts.size, snapshot.vmEventsSource, snapshot.updatedAtMs, nowMs),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                modifier = Modifier.padding(top = 8.dp).testTag("boardFooter"),
                            )
                        }
                    }
                }
            }
        }
        // Resize strip on the drawer's inner (left) edge — mirror of
        // the file drawer's resize handle: 24dp straddling the edge,
        // 3x72dp grip bar, drag to resize 0.4–0.85 (dragging left
        // widens a right drawer, so the sign is flipped). Rendered
        // AFTER the panel so it sits above it in the overlap, exactly
        // like the file drawer's handle sits above its drawer. Only
        // while open.
        if (visible) {
            Box(
                Modifier
                    .align(Alignment.CenterEnd)
                    .offset(x = -(drawerWidth - 12.dp))
                    .width(24.dp)
                    .fillMaxHeight()
                    .testTag("agentsDrawerResize")
                    .semantics {
                        contentDescription = "Resize agents drawer"
                        customActions = listOf(
                            CustomAccessibilityAction("Make drawer wider") { drawerFraction = (drawerFraction + 0.1f).coerceAtMost(0.85f); true },
                            CustomAccessibilityAction("Make drawer narrower") { drawerFraction = (drawerFraction - 0.1f).coerceAtLeast(0.4f); true },
                        )
                    }
                    .pointerInput(widthPixels) {
                        detectHorizontalDragGestures { change, drag ->
                            change.consume()
                            drawerFraction = (drawerFraction - drag / widthPixels).coerceIn(0.4f, 0.85f)
                        }
                    }
            ) {
                Box(
                    Modifier.align(Alignment.TopCenter).padding(top = 32.dp).width(3.dp).height(72.dp)
                        .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.7f), MaterialTheme.shapes.small)
                )
            }
        }
    }
}

/**
 * One agent entry — a plain row in the file drawer's language, not a
 * card: 48dp min height, 20dp icon slot (status dot), titleSmall task
 * + labelSmall second line. Tap the row to expand it (full task,
 * result, full meta); tap again to collapse.
 */
@Composable
private fun AgentRow(agent: MuseAgent, nowMs: Long, isRunning: Boolean) {
    var expanded by remember { mutableStateOf(false) }
    val elapsedSec = when {
        isRunning && agent.startedAtMs > 0 -> (nowMs - agent.startedAtMs) / 1000
        agent.durationSec != null -> agent.durationSec
        else -> -1L
    }
    Row(
        Modifier
            .fillMaxWidth()
            .heightIn(min = 48.dp)
            .clickable { expanded = !expanded }
            .padding(horizontal = 4.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier.size(24.dp),
            contentAlignment = Alignment.Center,
        ) {
            Box(
                Modifier
                    .size(12.dp)
                    .clip(CircleShape)
                    .background(
                        when (agent.status) {
                            AgentStatus.RUNNING -> DeskhandDarkSuccess
                            AgentStatus.FAILED -> DeskhandDarkError
                            AgentStatus.DONE -> MaterialTheme.colorScheme.onSurfaceVariant
                        }
                    )
            )
        }
        Spacer(Modifier.width(8.dp))
        Column(Modifier.weight(1f)) {
            Text(
                text = agent.task.ifBlank { "(no task text)" },
                style = MaterialTheme.typography.titleSmall,
                color = MaterialTheme.colorScheme.onSurface,
                maxLines = if (expanded) Int.MAX_VALUE else 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                text = listOfNotNull(
                    if (isRunning) "Running • ${formatElapsed(elapsedSec)}" else "Finished • ${formatElapsed(elapsedSec)}",
                    agent.chatLabel?.let { "in $it" },
                    "id ${agent.shortId}",
                ).joinToString(" • "),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (expanded) {
                if (isRunning) {
                    Text(
                        text = formatLastActive(agent.lastActiveSec),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                if (agent.result != null) {
                    Text(
                        text = agent.result,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
    }
}

/** "HH:MM" in the device's local time, for the worker footer line. */
internal fun formatClockHhMm(epochSec: Long): String =
    SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(epochSec * 1000))

/** "HH:MM:SS" in the device's local time, for the Muse-worker footer line. */
internal fun formatWorkerClockHhMmSs(epochSec: Long): String =
    SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(epochSec * 1000))

/**
 * 100.017 (C13 push, spec §6.2): the worker half of the drawer footer.
 * "Worker connected" only when the health snapshot's worker row is OK;
 * otherwise the last-OK time, or "Worker status unknown" when no health
 * snapshot has arrived yet (the 2.5 s health poll has not delivered).
 * Never inferred from push liveness — push live does not mean the
 * worker is up.
 */
internal fun workerFooterLine(snapshot: HealthSnapshot?): String {
    val row = snapshot?.components?.firstOrNull { it.key == "worker" }
        ?: return "Worker status unknown"
    if (row.status == HealthStatus.OK) return "Worker connected"
    if (row.lastOkEpochSec > 0) return "Worker disconnected since ${formatClockHhMm(row.lastOkEpochSec)}"
    return "Worker disconnected"
}

/**
 * 100.037 (E12 watchdog, spec §7.4): the Muse-worker half of the drawer
 * footer — the SECOND line under the relay-based workerFooterLine above.
 * Transport first, worker second, never one blended claim: the line
 * above says whether the phone link is up; this one says whether the
 * Muse worker session is up, as last derived from `worker.*` events.
 *
 * Forms (spec §7.1 E12 strings, minus the button — there is NO
 * phone-side Reconnect button in v1, §10.3):
 * - UNKNOWN (no worker.* event ever seen): "Muse worker status unknown".
 *   The phone genuinely does not know yet — never a guessed "connected".
 * - DISCONNECTED: "Muse worker disconnected since HH:MM:SS, last attempt
 *   HH:MM, next in Ns". Parts are omitted when unknown: no attempt yet
 *   reads "no attempt yet"; no since-time reads "Muse worker
 *   disconnected". The next-in countdown is live (last attempt +
 *   next_delay_s, against nowMs) and drops off once the attempt is due
 *   — a stale countdown is never shown as fresh.
 * - DISCONNECTED + auto-reconnect stopped: "Muse worker disconnected
 *   since HH:MM:SS — auto-reconnect stopped after N attempts, last
 *   attempt HH:MM", with " (grant off)" / " (grant paused)" /
 *   " (reconnect failing)" when the stop reason says so ("cap" needs
 *   no suffix — the count says it).
 * - CONNECTED (verified recovery only, spec §5.4): "Muse worker:
 *   reconnected HH:MM (was down Xm Ys)". No ticking "last heartbeat"
 *   is rendered: no heartbeat reaches the phone, so the connected form
 *   is the recovery form — it stays until a new disconnect supersedes
 *   it, and the down duration is shown once, from the event.
 */
internal fun museWorkerFooterLine(state: MuseWorkerState, nowMs: Long): String {
    return when (state.connection) {
        MuseWorkerConnection.UNKNOWN -> "Muse worker status unknown"
        MuseWorkerConnection.CONNECTED -> {
            val whenText = if (state.reconnectedAtMs > 0) formatClockHhMm(state.reconnectedAtMs / 1000) else "—"
            val downText = state.downSeconds?.let { " (was down ${formatElapsed(it)})" } ?: ""
            "Muse worker: reconnected $whenText$downText"
        }
        MuseWorkerConnection.DISCONNECTED -> {
            val sinceText = if (state.disconnectedSinceMs > 0) {
                "Muse worker disconnected since ${formatWorkerClockHhMmSs(state.disconnectedSinceMs / 1000)}"
            } else {
                "Muse worker disconnected"
            }
            if (state.stoppedReason != null) {
                val attemptsText = if (state.attempts > 0) " after ${state.attempts} attempts" else ""
                val lastText = if (state.lastAttemptMs > 0) {
                    ", last attempt ${formatClockHhMm(state.lastAttemptMs / 1000)}"
                } else ""
                val reasonText = when (state.stoppedReason) {
                    "grant-off" -> " (grant off)"
                    "grant-paused" -> " (grant paused)"
                    "reconcile-failing" -> " (reconnect failing)"
                    else -> ""
                }
                "$sinceText — auto-reconnect stopped$attemptsText$lastText$reasonText"
            } else {
                val attemptText = if (state.lastAttemptMs > 0) {
                    "last attempt ${formatClockHhMm(state.lastAttemptMs / 1000)}"
                } else {
                    "no attempt yet"
                }
                val nextText = run {
                    val delay = state.nextDelaySec ?: return@run null
                    if (state.lastAttemptMs <= 0) return@run null
                    val remainingSec = (state.lastAttemptMs + delay * 1000 - nowMs) / 1000
                    if (remainingSec > 0) "next in ${remainingSec}s" else null
                }
                listOfNotNull(sinceText, attemptText, nextText).joinToString(", ")
            }
        }
    }
}

/**
 * 100.033 (message board, spec §5.3): the board's one quiet footer line.
 * Built from what the feeds actually report, on every poll:
 * - the fetch itself stale (>120 s, same threshold as the drawer) →
 *   STALE, last good posts shown — never "Live" on a dead poll;
 * - never fetched → an honest "not updated yet", never "Live" either;
 * - otherwise "Live · N posts · updated Xs ago", with each missing leg
 *   named, not hidden: the event leg unwired (sources.vm_events != ok)
 *   → "live step notes not wired yet"; the relay not started → the
 *   outside-AI line (stage 2 is coordinator-side and has not started).
 * The relay line is dropped by the build that starts the relay — until
 * then it stays, so the board never looks like it carries relay posts.
 */
internal fun boardFooterLine(postCount: Int, vmEventsSource: String, updatedAtMs: Long, nowMs: Long): String {
    if (updatedAtMs <= 0) return "Not updated yet — open the drawer and tap Refresh."
    val ageSec = (nowMs - updatedAtMs) / 1000
    if (ageSec > 120) return "STALE — no update for ${formatElapsed(ageSec)} · last good posts shown"
    val gaps = buildList {
        if (vmEventsSource != "ok") add("live step notes not wired yet")
        add("outside-AI relay not started — subagent posts only")
    }
    return "Live · $postCount posts · updated ${formatElapsed(ageSec)} ago · ${gaps.joinToString(" · ")}"
}

/**
 * One board post (100.033, spec §7.2) — a plain row in the file drawer's
 * language, like AgentRow: author chip (kind-coloured dot + name) + time
 * (true ts, "19:24") + origin chip (live / snapshot / relay — always
 * visible, never hidden behind a tap) + the text (per-kind cap, "show
 * more" past the cap) + ticking elapsed ("Running • 6m 30s") or duration
 * ("Finished • 4m 12s") for agent-linked rows + the "in <chat>" line.
 * Tapping the row expands it in place: full text, full ref (source chat,
 * message id, agent id), relayed time vs said time. No pop-up. Colours
 * are DeskhandTheme tokens only — subagent teal DeskhandPrimary,
 * outside-ai amber DeskhandDarkWarningText, jett/system muted.
 */
@Composable
private fun BoardPostRow(post: BoardPost, nowMs: Long) {
    var expanded by remember { mutableStateOf(false) }
    val showMore = boardNeedsShowMore(post)
    // Ticking elapsed for a running agent-linked post (same trick as the
    // drawer runtimes: from the true start ts); a finished post shows its
    // stored duration instead. Relay posts carry neither.
    val runSec = when {
        post.status == AgentStatus.RUNNING && post.tsMs > 0 -> (nowMs - post.tsMs) / 1000
        post.status == AgentStatus.RUNNING -> post.elapsedSec ?: -1L
        else -> post.durationSec ?: -1L
    }
    val statusText = when (post.status) {
        AgentStatus.RUNNING -> "Running • ${formatElapsed(runSec)}"
        AgentStatus.DONE -> "Finished • ${formatElapsed(runSec)}"
        AgentStatus.FAILED -> "Failed • ${formatElapsed(runSec)}"
        null -> null
    }
    Column(
        Modifier
            .fillMaxWidth()
            .clickable { expanded = !expanded }
            .padding(horizontal = 4.dp, vertical = 6.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier
                    .size(12.dp)
                    .clip(CircleShape)
                    .background(
                        when (post.authorKind) {
                            BoardAuthorKind.SUBAGENT -> DeskhandPrimary
                            BoardAuthorKind.OUTSIDE_AI -> DeskhandDarkWarningText
                            else -> MaterialTheme.colorScheme.onSurfaceVariant
                        }
                    )
            )
            Spacer(Modifier.width(8.dp))
            Text(
                text = post.authorName.ifBlank { "(unknown author)" },
                style = MaterialTheme.typography.titleSmall,
                color = MaterialTheme.colorScheme.onSurface,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f, fill = false),
            )
            Text(
                text = if (post.tsMs > 0) formatClockHhMm(post.tsMs / 1000) else "—",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            modifier = Modifier.padding(top = 2.dp),
        ) {
            // Origin chip — part of the row, always visible (spec §9.2).
            Box(
                Modifier
                    .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(4.dp))
                    .padding(horizontal = 6.dp, vertical = 2.dp),
            ) {
                Text(
                    text = post.origin.name.lowercase(),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Text(
                text = listOfNotNull(statusText, post.chatLabel?.let { "in $it" }).joinToString(" • "),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
        }
        Text(
            text = boardDisplayText(post, expanded),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurface,
            maxLines = if (expanded) Int.MAX_VALUE else 3,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 2.dp),
        )
        if (showMore) {
            TextButton(onClick = { expanded = !expanded }) {
                Text(if (expanded) "show less" else "show more")
            }
        }
        if (expanded) {
            Column(Modifier.padding(top = 4.dp)) {
                post.agentId?.let {
                    Text(
                        text = "agent id $it",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                post.sourceChatId?.let {
                    Text(
                        text = "source chat $it",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                post.sourceMessageId?.let {
                    Text(
                        text = "message $it",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                if (post.origin == BoardOrigin.RELAY) {
                    Text(
                        text = listOfNotNull(
                            post.saidTsMs?.let { "said ${formatClockHhMm(it / 1000)}" },
                            post.relayedTsMs?.let { "relayed ${formatClockHhMm(it / 1000)}" },
                        ).joinToString(" · ").ifBlank { "relayed — source time unknown" },
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}
