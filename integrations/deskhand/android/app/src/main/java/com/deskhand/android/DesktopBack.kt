package com.deskhand.android

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.ui.res.painterResource
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONObject
import java.util.UUID
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

@Composable
internal fun DesktopStreamOverlay(notice: String?, bubbles: @Composable (Modifier) -> Unit = {},
    windowControls: @Composable () -> Unit = {}, content: @Composable () -> Unit) {
    Box(Modifier.fillMaxSize()) {
        content()
        Box(Modifier.align(Alignment.TopEnd).padding(4.dp)) { windowControls() }
        Column(Modifier.align(Alignment.BottomCenter).fillMaxWidth().fillMaxHeight().padding(top = 60.dp)) {
            Box(Modifier.weight(1f).fillMaxWidth()) { bubbles(Modifier.fillMaxSize()) }
            notice?.let {
                GlassSurface(Modifier.fillMaxWidth().padding(8.dp), shape = MaterialTheme.shapes.small) {
                    Text(it, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(8.dp))
                }
            }
        }
    }
}

class DesktopBackController(private val send: (Int, ByteArray) -> Boolean,
    private val later: (Long, () -> Unit) -> Unit) {
    var enabled by mutableStateOf(false)
        private set
    var busy by mutableStateOf(false)
        private set
    var notice by mutableStateOf<String?>(null)
        private set
    private var requestId: String? = null
    private var allowed = false

    fun sessionLost() {
        requestId = null; allowed = false; enabled = false; busy = false; notice = null
    }
    fun info(supported: Boolean, control: Boolean) {
        allowed = supported && control
        enabled = allowed && !busy
    }
    fun closeLatest() {
        if (!enabled || busy) return
        val id = UUID.randomUUID().toString()
        requestId = id; busy = true; enabled = false; notice = "Closing last opened window…"
        if (!send(Msg.DESKTOP_BACK, JSONObject().put("requestId", id).toString().toByteArray())) {
            requestId = null; busy = false; enabled = allowed
            notice = "Couldn't send the close request."
            return
        }
        later(10_000) {
            if (requestId == id && busy) {
                // A late answer may still arrive. Keep this ID and never retry automatically.
                notice = "Close result unknown. Check the PC window; reconnect before trying again."
            }
        }
    }
    fun message(payload: ByteArray) {
        val response = runCatching { require(payload.size <= 4096); JSONObject(payload.toString(Charsets.UTF_8)) }.getOrNull() ?: return
        if (response.optString("requestId") != requestId) return
        requestId = null; busy = false; enabled = allowed
        val title = response.optString("windowTitle").takeIf { it.isNotBlank() && it != "null" } ?: "window"
        notice = when (response.optString("error").takeUnless { it == "null" }) {
            "", null -> "Close requested: $title. Check for a save prompt."
            "empty" -> "No windows opened from Files to close."
            "busy" -> "The PC is still opening a window. Try again shortly."
            "untracked" -> "The latest launch reused a window or couldn't be tracked. Close it normally on the PC."
            "prompt" -> "Check the save or confirmation dialog on the PC."
            "denied" -> "The PC doesn't allow closing this window."
            "unsupported" -> "Update Deskhand on the PC to use this arrow."
            "missing" -> "That window is already closed."
            else -> "Close result unknown. Check the PC before trying again."
        }
        if (response.optString("error") == "unknown") {
            busy = true; enabled = false
            notice = "Close result unknown. Check the PC window; reconnect before trying again."
            return
        }
        val currentNotice = notice
        later(6000) { if (notice == currentNotice) notice = null }
    }
}

@Composable
internal fun ForegroundWindowButtons(enabled: Boolean, busy: Boolean, onAction: (String) -> Unit) {
    // 100.050 (§5.3): the Surface wrapper is gone — the toolbar's GlassSurface
    // (100.043) is the only surface; this Row renders transparent buttons with
    // no background of its own. Buttons byte-identical: 48.dp, testTags,
    // contentDescriptions, "−" / "×", enabled && !busy gating, busy spinner.
    Row {
        IconButton(onClick = { onAction("minimize") }, enabled = enabled && !busy,
            modifier = Modifier.size(48.dp).testTag("minimizeFocusedWindow").semantics { contentDescription = "Minimize focused window" }) {
            if (busy) CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
            else Text("−", style = MaterialTheme.typography.titleMedium)
        }
        IconButton(onClick = { onAction("close") }, enabled = enabled && !busy,
            modifier = Modifier.size(48.dp).testTag("closeFocusedWindow").semantics { contentDescription = "Close focused window" }) {
            Text("×", style = MaterialTheme.typography.titleMedium)
        }
    }
}
