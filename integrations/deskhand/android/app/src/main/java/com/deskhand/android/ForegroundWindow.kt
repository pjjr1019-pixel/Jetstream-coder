package com.deskhand.android

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONObject
import java.util.UUID

class ForegroundWindowController(private val send: (Int, ByteArray) -> Boolean, private val later: (Long, () -> Unit) -> Unit) {
    var enabled by mutableStateOf(false); private set
    var busy by mutableStateOf(false); private set
    var notice by mutableStateOf<String?>(null); private set
    private var allowed = false
    private var requestId: String? = null
    fun sessionLost() { allowed = false; enabled = false; busy = false; requestId = null; notice = null }
    fun info(supported: Boolean, control: Boolean) { allowed = supported && control; enabled = allowed && !busy }
    fun act(action: String) {
        if (!enabled || busy || action !in listOf("minimize", "close")) return
        val id = UUID.randomUUID().toString()
        requestId = id; busy = true; enabled = false
        if (!send(Msg.FOREGROUND_WINDOW, JSONObject().put("requestId", id).put("action", action).toString().toByteArray())) {
            requestId = null; busy = false; enabled = allowed; notice = "Couldn't send the window request."; return
        }
        later(10_000) { if (requestId == id && busy) notice = "Window action unconfirmed. Check the PC; reconnect before trying again." }
    }
    fun message(payload: ByteArray) {
        val result = runCatching { require(payload.size <= 4096); JSONObject(String(payload, Charsets.UTF_8)) }.getOrNull() ?: return
        if (result.optString("requestId") != requestId) return
        val error = result.optString("error").takeUnless { it.isBlank() || it == "null" }
        if (error == "unknown") { notice = "Window action unconfirmed. Check the PC; reconnect before trying again."; return }
        requestId = null; busy = false; enabled = allowed
        notice = when (error) {
            null -> null
            "empty" -> "No focused window to control."
            "changed" -> "The focused window changed. Try again."
            "denied" -> "Windows couldn't control that window."
            "busy" -> "PC is busy. Try again."
            else -> "Couldn't control the focused window."
        }
        val current = notice
        if (current != null) later(6000) { if (notice == current) notice = null }
    }
}
