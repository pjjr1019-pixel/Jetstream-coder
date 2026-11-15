package com.deskhand.android

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONObject
import java.util.UUID

data class DesktopItem(val token: String, val name: String, val kind: String,
    val canBrowse: Boolean, val canOpen: Boolean, val error: String?)
data class DesktopListResponse(val requestId: String, val folderToken: String?, val path: String,
    val parentToken: String?, val items: List<DesktopItem>, val error: String?, val nextCursor: String?) {
    companion object {
        fun decode(bytes: ByteArray): DesktopListResponse {
            require(bytes.size <= 2_000_000)
            val o = JSONObject(bytes.toString(Charsets.UTF_8))
            val array = o.getJSONArray("items")
            require(array.length() <= 2000)
            return DesktopListResponse(o.getString("requestId"), o.nullable("folderToken"), o.getString("path"),
                o.nullable("parentToken"), List(array.length()) { index ->
                    val i = array.getJSONObject(index)
                    DesktopItem(i.getString("token"), i.getString("name"), i.getString("kind"),
                        i.getBoolean("canBrowse"), i.getBoolean("canOpen"), i.nullable("error"))
                }, o.nullable("error"), o.nullable("nextCursor"))
        }
    }
}
// B2-G: DesktopListRequest / DesktopOpenRequest removed — the controller
// builds the JSON inline (requestPage / sendOpen) and these DTOs (and
// their encode()) had no callers; they had also diverged from the wire.
data class DesktopOpenResult(val requestId: String, val error: String?) {
    companion object {
        fun decode(bytes: ByteArray): DesktopOpenResult {
            require(bytes.size <= 4096)
            val o = JSONObject(bytes.toString(Charsets.UTF_8))
            return DesktopOpenResult(o.getString("requestId"), o.nullable("error"))
        }
    }
}
private fun JSONObject.nullable(key: String) = if (isNull(key)) null else getString(key)

data class BrowserCrumb(val token: String, val label: String)

data class DesktopBrowserState(
    val connected: Boolean = false, val supported: Boolean = false, val control: Boolean = false,
    val folderToken: String? = null, val parentToken: String? = null, val path: String = "Desktop",
    val items: List<DesktopItem> = emptyList(), val loading: Boolean = false, val opening: Boolean = false,
    val error: String? = null, val notice: String? = null, val confirmation: DesktopItem? = null,
    val wholePc: Boolean = false, val openedCount: Int = 0, val crumbs: List<BrowserCrumb> = emptyList(),
    val revealSupported: Boolean = false, val nextCursor: String? = null
)

/** Session-owned tokens stay in memory; activity recreation does not reset this controller. */
class DesktopBrowserController(
    private val send: (Int, ByteArray) -> Boolean,
    private val later: (Long, () -> Unit) -> Unit
) {
    var state by mutableStateOf(DesktopBrowserState())
        private set
    private var listId: String? = null
    private var openId: String? = null
    private var retryFolder: String? = null
    private var requestCursor: String? = null
    private var revealing = false

    fun sessionLost() {
        listId = null; openId = null; retryFolder = null; requestCursor = null; revealing = false
        state = DesktopBrowserState(notice = "Connect to the PC to browse Desktop.")
    }
    fun info(supported: Boolean, control: Boolean, wholePc: Boolean = false, revealSupported: Boolean = false) {
        val first = !state.connected
        state = state.copy(connected = true, supported = supported, control = control, wholePc = wholePc, revealSupported = revealSupported,
            confirmation = if (control) state.confirmation else null)
        if (first && supported) navigate(null)
    }
    fun refresh() = navigate(state.folderToken)
    fun retry() = requestPage(retryFolder, requestCursor)
    fun up(): Boolean {
        val parent = state.parentToken ?: return false
        if (!state.loading && !state.opening) navigate(parent)
        return true
    }
    fun navigate(folder: String?) {
        requestPage(folder, null)
    }
    fun loadMore() {
        val cursor = state.nextCursor ?: return
        requestPage(state.folderToken, cursor)
    }
    private fun requestPage(folder: String?, cursor: String?) {
        if (!state.connected || !state.supported || state.loading || state.opening) return
        val id = UUID.randomUUID().toString()
        listId = id; retryFolder = folder; requestCursor = cursor
        state = state.copy(loading = true, error = null, notice = null, confirmation = null)
        val request = JSONObject().put("requestId", id).put("folderToken", folder ?: JSONObject.NULL)
        if (folder == null && state.wholePc) request.put("thisPc", true)
        if (state.revealSupported) request.put("paged", true)
        if (cursor != null) request.put("cursor", cursor)
        if (!send(Msg.DESKTOP_LIST, request.toString().toByteArray())) {
            listId = null; state = state.copy(loading = false, error = "disconnected"); return
        }
        later(15_000) {
            if (listId == id) { listId = null; state = state.copy(loading = false, error = "timeout") }
        }
    }
    fun tap(item: DesktopItem) {
        if (!state.connected || state.loading || state.opening || item !in state.items) return
        if (item.canBrowse) navigate(item.token)
        else if (item.canOpen && state.control) state = state.copy(confirmation = item)
    }
    fun cancel() { state = state.copy(confirmation = null) }
    fun confirm() {
        val item = state.confirmation ?: return
        state = state.copy(confirmation = null)
        if (!state.connected || !state.control || state.opening || state.loading || item !in state.items) return
        sendOpen(item, false)
    }
    fun reveal(item: DesktopItem) {
        if (!state.connected || !state.control || !state.revealSupported || state.opening || state.loading || item !in state.items || item.token.isBlank()) return
        sendOpen(item, true)
    }
    private fun sendOpen(item: DesktopItem, reveal: Boolean) {
        val id = UUID.randomUUID().toString()
        revealing = reveal
        openId = id
        state = state.copy(opening = true, notice = null)
        val request = JSONObject().put("requestId", id).put("itemToken", item.token).put("confirmed", true)
        if (reveal) request.put("revealLocation", true)
        if (!send(Msg.DESKTOP_OPEN, request.toString().toByteArray())) {
            openId = null; state = state.copy(opening = false, notice = desktopError("unknown")); return
        }
        later(15_000) {
            if (openId == id) { openId = null; state = state.copy(opening = false, notice = desktopError("unknown")) }
        }
    }
    fun message(type: Int, payload: ByteArray) {
        if (!state.connected) return
        if (type == Msg.DESKTOP_LIST_RESULT) {
            val result = runCatching { DesktopListResponse.decode(payload) }.getOrNull() ?: return
            if (result.requestId != listId) return
            listId = null
            state = if (result.error != null) state.copy(loading = false, error = result.error)
            else {
                val token = result.folderToken
                val old = state.crumbs
                val index = old.indexOfFirst { it.token == token }
                val label = state.items.firstOrNull { it.token == token }?.name
                    ?: result.path.substringAfterLast('\\').ifBlank { result.path }
                val crumbs = when {
                    token == null -> emptyList()
                    index >= 0 -> old.take(index + 1)
                    old.lastOrNull()?.token == result.parentToken -> old + BrowserCrumb(token, label)
                    else -> listOf(BrowserCrumb(token, label))
                }
                val items = if (requestCursor != null) state.items + result.items else result.items
                requestCursor = null
                state.copy(loading = false, error = null, folderToken = token,
                    parentToken = result.parentToken, path = result.path, items = items, crumbs = crumbs, nextCursor = result.nextCursor)
            }
        } else if (type == Msg.DESKTOP_OPEN_RESULT) {
            val result = runCatching { DesktopOpenResult.decode(payload) }.getOrNull() ?: return
            if (result.requestId != openId) return
            openId = null
            state = state.copy(opening = false, notice = result.error?.let(::desktopError) ?: if (revealing) "Opened location on PC." else "Opened on PC.", openedCount = state.openedCount + if (result.error == null) 1 else 0)
            revealing = false
        }
    }
}

fun desktopError(code: String): String = when (code) {
    "denied" -> "The PC does not allow this action."
    "inaccessible" -> "Windows cannot access this item. It may be in use."
    "missing" -> "This item no longer exists. Refresh the folder."
    "stale_item" -> "This item changed. Refresh the folder or reconnect."
    "unsupported" -> "Windows cannot open this item."
    "busy" -> "The PC is busy. Try again; reconnect if this continues."
    "too_large" -> "This folder has too many items to display (maximum 2,000)."
    "timeout" -> "The PC did not respond. Try again."
    "disconnected" -> "The PC is disconnected."
    "unknown" -> "The PC may have opened the item. Check before trying again."
    else -> "The PC could not complete this request. Refresh and try again."
}
