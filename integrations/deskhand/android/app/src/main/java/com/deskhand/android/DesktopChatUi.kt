package com.deskhand.android

import android.animation.ValueAnimator
import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.tween
import androidx.compose.foundation.clickable
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import org.json.JSONObject

@Composable
internal fun DesktopChatComposer(draft: String, onDraftChange: (String) -> Unit, sending: Boolean,
    canSend: Boolean, onSend: () -> Unit, onSettings: () -> Unit, onHistory: () -> Unit,
    active: Boolean = false, canStop: Boolean = false, onStop: () -> Unit = {},
    canStopAgent: Boolean = canStop, onStopAgent: () -> Unit = { Model.stopAgentWork() },
    agentStopStatus: String? = Model.agentStopStatus,
    voiceOn: Boolean = false, onVoiceToggle: () -> Unit = {}) {
    val shape = RoundedCornerShape(28.dp)
    var focused by remember { mutableStateOf(false) }
    // 100.025 (S4/S13): the "Stop agent" control sits beside the square
    // button and is shown whenever the square button is (any active
    // turn). It sends stop_agent_work, which stops agent work only —
    // chat admissions are untouched. On a plain chat turn the service
    // harmlessly reports interrupt_requested false with an empty
    // queued_cancelled. The service's returned `status` string is
    // shown verbatim underneath when present.
    Column(Modifier.fillMaxWidth()) {
    // 100.043 (C2): the composer is glass — GlassSurface supplies the
    // translucent fill (surface at 0.60f, up from the old surfaceVariant
    // at 0.45f) and the glass border. The focus signal is kept via
    // GlassSurface's optional border parameter (option (a) in the spec —
    // one border, one place): focused -> primary at full strength,
    // unfocused -> the composer's existing outline at 0.45f, visibly
    // distinct. The 28.dp pill shape, the h6 outer / h4 inner padding,
    // and testTag "chatComposer" are unchanged; the Column, the
    // "Stop agent" button, and agentStopStatus stay outside/below the glass.
    GlassSurface(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 6.dp).testTag("chatComposer"),
        shape = shape,
        border = BorderStroke(1.dp, if (focused) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outline.copy(alpha = 0.45f))
    ) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        IconButton(onClick = onSettings, modifier = Modifier.semantics { contentDescription = "Assistant settings" }) {
            Icon(painterResource(R.drawable.assistant_settings), contentDescription = null, modifier = Modifier.size(28.dp))
        }
        IconButton(onClick = onHistory, modifier = Modifier.semantics { contentDescription = "Chat history" }) {
            Icon(painterResource(R.drawable.chat_history), contentDescription = null, modifier = Modifier.size(28.dp))
        }
        // 100.036 Quick Reply Mode: one-tap voice toggle. Replies stay
        // text-only unless this is on, in which case new AI replies are
        // spoken aloud. The icon shows the current state.
        IconButton(onClick = onVoiceToggle, modifier = Modifier.testTag("voiceRepliesToggle")
            .semantics { contentDescription = if (voiceOn) "Voice replies on" else "Voice replies off" }) {
            Icon(painterResource(if (voiceOn) R.drawable.voice_replies_on else R.drawable.voice_replies_off),
                contentDescription = null, modifier = Modifier.size(28.dp))
        }
        BasicTextField(value = draft, onValueChange = onDraftChange, singleLine = true,
            textStyle = MaterialTheme.typography.bodyMedium.copy(color = MaterialTheme.colorScheme.onSurface),
            cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { if (canSend && !sending && !active) onSend() }),
            modifier = Modifier.weight(1f).heightIn(min = 48.dp)
                .onFocusChanged { focused = it.isFocused }
                .semantics { contentDescription = "Message assistant" },
            decorationBox = { inner -> Box(Modifier.fillMaxWidth().heightIn(min = 48.dp).padding(horizontal = 4.dp), contentAlignment = Alignment.CenterStart) {
                if (draft.isEmpty()) Text("Message assistant", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
                inner()
            } })
        FilledIconButton(onClick = if (active) onStop else onSend,
            enabled = if (active) canStop else canSend && !sending,
            modifier = Modifier.semantics { contentDescription = if (active) "Restart chat" else if (sending) "Sending…" else "Send" }) {
            if (active) Box(Modifier.size(17.dp).background(LocalContentColor.current, RoundedCornerShape(2.dp)))
            else if (sending) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp)
            else Icon(painterResource(R.drawable.chat_send), contentDescription = null, modifier = Modifier.size(28.dp))
        }
        if (active) OutlinedButton(onClick = onStopAgent, enabled = canStopAgent,
            modifier = Modifier.padding(start = 4.dp).semantics { contentDescription = "Stop agent" }) {
            Text("Stop agent", maxLines = 1)
        }
    }
    } // GlassSurface (100.043 C2)
    if (agentStopStatus != null) Text(agentStopStatus, style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(start = 12.dp, top = 4.dp, bottom = 2.dp).testTag("agentStopStatus"))
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun DesktopChatPanels(panel: String?, selectedMessage: String?, extraSettings: @Composable () -> Unit = {}, onDismiss: () -> Unit) {
    val focus = LocalFocusManager.current
    val keyboard = LocalSoftwareKeyboardController.current
    LaunchedEffect(panel) { if (panel != null) { focus.clearFocus(); keyboard?.hide() } }
    if (panel == null) return
    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().fillMaxHeight(0.85f).padding(horizontal = 16.dp)) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text(if (panel == "settings") "Settings" else "Chat history", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                TextButton(onClick = onDismiss) { Text("Done") }
            }
            if (panel == "settings") Column(Modifier.fillMaxWidth().weight(1f).verticalScroll(rememberScrollState())) {
                extraSettings()
                AssistantSettings(Modifier.fillMaxWidth())
            }
            else ChatHistory(Model.desktopChat.messages, selectedMessage, Modifier.weight(1f).fillMaxWidth())
        }
    }
}

@Composable
internal fun AssistantSettings(modifier: Modifier = Modifier) {
    var modelsOpen by remember { mutableStateOf(false) }
    var effortOpen by remember { mutableStateOf(false) }
    var routeOpen by remember { mutableStateOf(false) }
    val route = Model.chatRoutes.getOrNull(Model.chatRouteIndex)
    Column(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(if (Model.chatSending) "Sending…" else if (Model.chatStatus == "Ready") "Chat ready" else Model.chatStatus)
        if (Model.localAiTasksAvailable && route?.optString("assistant") == "local") {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Switch(checked = Model.computerTaskMode, onCheckedChange = { Model.setAgenticMode(it) })
                Spacer(Modifier.width(8.dp))
                Text("Agentic mode", style = MaterialTheme.typography.labelMedium)
            }
            Text("Requires desktop access on the PC. Some actions ask for approval.", style = MaterialTheme.typography.bodySmall)
        } else Text("Computer tasks are unavailable for this assistant.", style = MaterialTheme.typography.labelMedium)
        Text("Assistant / model", style = MaterialTheme.typography.labelLarge)
        Box {
            OutlinedButton(onClick = { modelsOpen = true }) { Text(route?.optString("name") ?: "Choose assistant") }
            DropdownMenu(expanded = modelsOpen, onDismissRequest = { modelsOpen = false }) {
                Model.chatRoutes.forEachIndexed { index, item ->
                    DropdownMenuItem(text = { Text(item.optString("name")) }, onClick = {
                        Model.chatRouteIndex = index; Model.chatEffort = item.optString("effort"); modelsOpen = false
                    })
                }
            }
        }
        Text("Effort", style = MaterialTheme.typography.labelLarge)
        Box {
            OutlinedButton(onClick = { effortOpen = true }) { Text(Model.chatEffort.ifBlank { "Default effort" }) }
            DropdownMenu(expanded = effortOpen, onDismissRequest = { effortOpen = false }) {
                val efforts = route?.optJSONArray("efforts")
                if (efforts != null) for (i in 0 until efforts.length()) {
                    val effort = efforts.getString(i)
                    DropdownMenuItem(text = { Text(effort) }, onClick = { Model.chatEffort = effort; effortOpen = false })
                }
            }
        }
        Text("Route", style = MaterialTheme.typography.labelLarge)
        Box {
            OutlinedButton(onClick = { routeOpen = true }) { Text(Model.chatRouteMode) }
            DropdownMenu(expanded = routeOpen, onDismissRequest = { routeOpen = false }) {
                listOf("Auto", "Fast", "Smart").forEach { mode ->
                    DropdownMenuItem(text = { Text(mode) }, onClick = { Model.applyChatRouteMode(mode); routeOpen = false })
                }
            }
        }
        if (Model.chatRouteReason.isNotBlank()) Text(Model.chatRouteReason, style = MaterialTheme.typography.bodySmall)
        TextButton(onClick = { Model.chatRequest(JSONObject().put("op", "catalog").put("refresh", true)) }, enabled = Model.chatConnected) { Text("Refresh assistants") }
    }
}

@Composable
internal fun ChatHistory(messages: List<ChatMessage>, selected: String?, modifier: Modifier = Modifier) {
    val list = rememberLazyListState()
    LaunchedEffect(Unit) {
        val target = messages.indexOfFirst { it.key == selected }.takeIf { it >= 0 } ?: messages.lastIndex
        if (target >= 0) list.scrollToItem(target)
    }
    if (messages.isEmpty()) Text("No messages yet.", modifier.padding(8.dp))
    else LazyColumn(modifier.testTag("chatHistory"), state = list, verticalArrangement = Arrangement.spacedBy(8.dp), contentPadding = PaddingValues(bottom = 24.dp)) {
        items(messages, key = { it.key }) { message ->
            Row(Modifier.fillMaxWidth(), horizontalArrangement = if (message.role == "user") Arrangement.End else Arrangement.Start) {
                Surface(Modifier.fillMaxWidth(0.92f).testTag("history-${message.key}"), shape = MaterialTheme.shapes.medium,
                    color = if (message.role == "user") MaterialTheme.colorScheme.secondaryContainer else MaterialTheme.colorScheme.surfaceVariant) {
                    Column(Modifier.padding(12.dp)) {
                        Text("${message.label} · ${message.status}", style = MaterialTheme.typography.labelSmall,
                            color = LocalContentColor.current.copy(alpha = 0.82f))
                        SelectionContainer { Text(message.text, color = MaterialTheme.colorScheme.onSurface) }
                        // 100.039: send time under each SENT bubble only.
                        if (message.role == "user") {
                            formatSentTime(message.sentAtMs)?.let {
                                Text(it, style = MaterialTheme.typography.labelSmall,
                                    color = LocalContentColor.current.copy(alpha = 0.6f))
                            }
                        }
                    }
                }
            }
        }
    }
}

internal data class BubbleMotion(val alpha: Float, val riseDp: Float)
/** 100.039: send time for a SENT bubble — HH:mm:ss in the phone's local time; null when unknown. */
internal fun formatSentTime(sentAtMs: Long?): String? =
    sentAtMs?.let { android.text.format.DateFormat.format("HH:mm:ss", java.util.Date(it)).toString() }
internal fun bubbleMotion(age: Long, animations: Boolean): BubbleMotion {
    if (age >= DesktopChatState.LifetimeMs) return BubbleMotion(0f, 0f)
    if (!animations) return BubbleMotion(1f, 0f)
    val enter = (age.toFloat() / DesktopChatState.EnterMs).coerceIn(0f, 1f)
    val exit = ((age - DesktopChatState.EnterMs - DesktopChatState.HoldMs).toFloat() / DesktopChatState.FadeMs).coerceIn(0f, 1f)
    return BubbleMotion(enter * (1f - exit), 24f * (1f - enter) - 24f * exit)
}

@Composable
internal fun ChatBubbleLane(state: DesktopChatState, modifier: Modifier = Modifier, onMessage: (ChatMessage) -> Unit) {
    var time by remember { mutableStateOf(state.timeNow()) }
    val bubbles = state.bubbles
    val animations = ValueAnimator.areAnimatorsEnabled()
    val density = LocalDensity.current.density
    LaunchedEffect(bubbles.map { it.message.key to it.born }) {
        while (state.bubbles.isNotEmpty()) {
            withFrameNanos { time = state.timeNow() }
            state.expire()
        }
    }
    BoxWithConstraints(modifier.testTag("chatBubbleLane")) {
      val visibleCount = (maxHeight.value / 140f).toInt().coerceIn(1, 3)
      Column(Modifier.align(Alignment.BottomStart).fillMaxWidth().padding(8.dp).animateContentSize(tween(220)),
          verticalArrangement = Arrangement.spacedBy(6.dp)) {
        bubbles.takeLast(visibleCount).forEach { bubble -> key(bubble.message.key) {
            val message = bubble.message
            val motion = bubbleMotion((time - bubble.born).coerceAtLeast(0), animations)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = if (message.role == "user") Arrangement.End else Arrangement.Start) {
                Surface(Modifier.fillMaxWidth(0.85f).graphicsLayer { alpha = motion.alpha; translationY = motion.riseDp * density }
                    .clickable(enabled = motion.alpha > 0.02f) { onMessage(message) }.testTag("bubble-${message.key}"), shape = MaterialTheme.shapes.medium,
                    color = if (message.role == "user") MaterialTheme.colorScheme.secondaryContainer.copy(alpha = 0.95f)
                        else MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.95f)) {
                    Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
                        Text("${message.label} · ${message.status}", style = MaterialTheme.typography.labelSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        Text(message.text, style = MaterialTheme.typography.bodyMedium, maxLines = 4, overflow = TextOverflow.Ellipsis)
                        // 100.039: send time under each SENT bubble only.
                        if (message.role == "user") {
                            formatSentTime(message.sentAtMs)?.let {
                                Text(it, style = MaterialTheme.typography.labelSmall,
                                    color = LocalContentColor.current.copy(alpha = 0.6f))
                            }
                        }
                    }
                }
            }
        } }
      }
    }
}
