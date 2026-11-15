package com.deskhand.android

import android.os.SystemClock
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONObject

internal data class ChatMessage(val turnId: String, val role: String, val text: String, val label: String, val status: String, val sentAtMs: Long? = null) {
    val key get() = "$turnId/$role"
}
internal data class ChatBubble(val message: ChatMessage, val born: Long)

/** Session-level deadlines survive composition changes; snapshots never replay historical messages. */
internal class DesktopChatState(private val now: () -> Long = SystemClock::elapsedRealtime) {
    companion object {
        const val EnterMs = 220L
        const val HoldMs = 6000L
        const val FadeMs = 600L
        const val LifetimeMs = EnterMs + HoldMs + FadeMs
    }
    var bubbles by mutableStateOf<List<ChatBubble>>(emptyList())
        private set
    var messages by mutableStateOf<List<ChatMessage>>(emptyList())
        private set
    private var baseline = true
    private val seen = linkedSetOf<String>()
    private val pending = mutableSetOf<String>()
    private val labels = mutableMapOf<String, String>()
    // 100.036: assistant keys already handed to the voice callback, so a
    // reply is spoken exactly once no matter how often the history poll
    // re-delivers the same turn. (seen cannot do this: a streamed partial
    // already occupies the assistant key before the final reply arrives.)
    private val voiceNotified = mutableSetOf<String>()

    fun reset() { bubbles = emptyList(); messages = emptyList(); seen.clear(); pending.clear(); labels.clear(); voiceNotified.clear(); baseline = true }
    fun reconnect() { baseline = true }
    fun timeNow() = now()
    fun expire() { bubbles = bubbles.filter { now() - it.born < LifetimeMs } }

    private fun put(message: ChatMessage, preview: Boolean) {
        val index = messages.indexOfFirst { it.key == message.key }
        messages = if (index < 0) (messages + message).takeLast(400)
            else messages.toMutableList().also { it[index] = message }
        expire()
        bubbles = bubbles.map { if (it.message.key == message.key) it.copy(message = message) else it }
        if (preview && seen.add(message.key)) bubbles = (bubbles + ChatBubble(message, now())).takeLast(3)
        else seen.add(message.key)
        while (seen.size > 800) seen.remove(seen.first())
    }

    fun submitted(command: JSONObject) {
        val id = command.getString("id")
        labels[id] = listOf(command.optString("assistant"), command.optString("model")).filter { it.isNotBlank() }.joinToString(" · ").ifBlank { "Assistant" }
        pending.add(id)
        val sentAtMs = command.optLong("sent_at", 0).takeIf { it > 0 }
        put(ChatMessage(id, "user", command.getString("text"), "You", "Sending…", sentAtMs), true)
    }
    fun acknowledged(id: String) {
        messages.firstOrNull { it.turnId == id && it.role == "user" }?.let { put(it.copy(status = "Queued"), false) }
    }
    // 100.039: send time for a turn row — sent_at (epoch millis) when the
    // phone stamped it, else created (epoch seconds) converted to millis
    // (every row ever written carries created), else null.
    private fun sentAtFromTurn(turn: JSONObject): Long? {
        val sentAt = turn.optLong("sent_at", 0)
        if (sentAt > 0) return sentAt
        if (!turn.isNull("created")) {
            val createdSec = turn.optDouble("created", 0.0)
            if (createdSec > 0) return (createdSec * 1000).toLong()
        }
        return null
    }
    // 100.036 Quick Reply Mode: onNewAssistantReply fires once per NEW
    // assistant message (a terminal reply, or a rejected-turn error) so the
    // caller can speak it aloud when the voice toggle is on. It never fires
    // for user messages, streamed partials, or historical turns replayed
    // from the baseline after a (re)connect — those are not new replies.
    fun rejected(id: String, error: String, onNewAssistantReply: (ChatMessage) -> Unit = {}) {
        messages.firstOrNull { it.turnId == id && it.role == "user" }?.let { put(it.copy(status = "Failed"), false) }
        val reply = ChatMessage(id, "assistant", error, labels[id] ?: "Assistant", "Failed")
        put(reply, true)
        if (voiceNotified.add(reply.key)) onNewAssistantReply(reply)
        pending.remove(id)
    }

    // 100.040 (§7): the submitted-turn wait timed out with no history
    // update, so the delivery is unconfirmed — not failed. Mirrors
    // rejected(): the user row takes the terminal status and an
    // assistant bubble carries the notice. The wording is the same
    // string snapshot() uses for a server-side unknown turn (below),
    // kept byte-identical so the two paths never diverge.
    fun unconfirmed(id: String, onNewAssistantReply: (ChatMessage) -> Unit = {}) {
        messages.firstOrNull { it.turnId == id && it.role == "user" }?.let { put(it.copy(status = "unknown"), false) }
        val reply = ChatMessage(id, "assistant", "Delivery unconfirmed. Check before resending.", labels[id] ?: "Assistant", "unknown")
        put(reply, true)
        if (voiceNotified.add(reply.key)) onNewAssistantReply(reply)
        pending.remove(id)
    }

    fun snapshot(turns: List<JSONObject>, onNewAssistantReply: (ChatMessage) -> Unit = {}) {
        val serverKeys = mutableSetOf<String>()
        val dropKeys = mutableSetOf<String>()
        val parsed = mutableListOf<ChatMessage>()
        turns.forEach { turn ->
            val id = turn.optString("id")
            if (id.isBlank()) return@forEach
            val status = turn.optString("status")
            val preview = !baseline || id in pending
            val user = ChatMessage(id, "user", turn.optString("text"), "You", status, sentAtFromTurn(turn))
            put(user, preview); parsed.add(user); serverKeys.add(user.key)
            val terminal = status in listOf("completed", "failed", "unknown", "cancelled")
            val body = when {
                !turn.isNull("reply") -> turn.optString("reply")
                !turn.isNull("error") -> turn.optString("error")
                status == "unknown" -> "Delivery unconfirmed. Check before resending."
                else -> ""
            }
            if (terminal && body.isNotBlank()) {
                val label = listOf(turn.optString("assistant"), turn.optString("model")).filter { it.isNotBlank() }.joinToString(" · ")
                val reply = ChatMessage(id, "assistant", body, label.ifBlank { "Assistant" }, status)
                put(reply, preview); parsed.add(reply); serverKeys.add(reply.key)
                // Record first, fire second: a baseline turn is marked
                // considered even though it stays silent, so the next
                // history poll (baseline now false) cannot speak it late.
                val firstDelivery = voiceNotified.add(reply.key)
                if (preview && firstDelivery) onNewAssistantReply(reply)
                pending.remove(id)
            }
            // Batch 2: a dispatching turn with streamed partial text shows
            // the partial in place of the (absent) reply, marked
            // in-progress. The final reply replaces it (same message key),
            // never appends after it. A turn with no assistant text this
            // snapshot drops any lingering partial for that turn.
            val partial = if (turn.isNull("partial")) "" else turn.optString("partial")
            if (!terminal && partial.isNotBlank()) {
                val label = listOf(turn.optString("assistant"), turn.optString("model")).filter { it.isNotBlank() }.joinToString(" · ")
                val showing = ChatMessage(id, "assistant", "$partial …", label.ifBlank { "Assistant" }, status)
                put(showing, preview); parsed.add(showing); serverKeys.add(showing.key)
            } else {
                dropKeys.add("$id/assistant")
            }
        }
        // Server order is authoritative, with local submissions/errors awaiting a server row retained.
        messages = (parsed + messages.filter { it.key !in serverKeys && it.key !in dropKeys }).distinctBy { it.key }.takeLast(400)
        baseline = false
    }
}
