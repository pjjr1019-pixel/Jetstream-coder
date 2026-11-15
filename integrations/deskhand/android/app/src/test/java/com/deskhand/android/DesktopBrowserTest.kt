package com.deskhand.android

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class BrowserFixture {
    val sent = mutableListOf<Pair<Int, JSONObject>>()
    val timers = mutableListOf<() -> Unit>()
    var canSend = true
    val browser = DesktopBrowserController({ type, bytes ->
        sent.add(type to JSONObject(bytes.toString(Charsets.UTF_8))); canSend
    }, { _, action -> timers.add(action) })
    fun list(items: List<DesktopItem> = emptyList(), folder: String = "root", parent: String? = null,
        error: String? = null, id: String = sent.last().second.getString("requestId"), next: String? = null) {
        val json = JSONObject().put("requestId", id).put("folderToken", folder).put("path", if (parent == null) "Desktop" else "Desktop\\Folder")
            .put("parentToken", parent ?: JSONObject.NULL).put("error", error ?: JSONObject.NULL).put("nextCursor", next ?: JSONObject.NULL)
            .put("items", JSONArray().also { a -> items.forEach { i -> a.put(JSONObject().put("token", i.token).put("name", i.name)
                .put("kind", i.kind).put("canBrowse", i.canBrowse).put("canOpen", i.canOpen).put("error", i.error ?: JSONObject.NULL)) } })
        browser.message(Msg.DESKTOP_LIST_RESULT, json.toString().toByteArray())
    }
    fun opened(error: String? = null) {
        browser.message(Msg.DESKTOP_OPEN_RESULT, JSONObject().put("requestId", sent.last().second.getString("requestId"))
            .put("error", error ?: JSONObject.NULL).toString().toByteArray())
    }
    val folder = DesktopItem("folder", "Folder", "folder", true, false, null)
    val file = DesktopItem("file", "notes.txt", "file", false, true, null)
}

class DesktopBrowserTest {
    @Test fun paginatesAndRevealsFilesAndFolders() {
        val f = BrowserFixture(); f.browser.info(true, true, wholePc = true, revealSupported = true)
        f.list(listOf(f.folder), next = "cursor")
        f.browser.loadMore()
        assertEquals("cursor", f.sent.last().second.getString("cursor"))
        f.list(listOf(f.file))
        assertEquals(listOf(f.folder, f.file), f.browser.state.items)
        assertNull(f.browser.state.nextCursor)
        f.browser.reveal(f.folder)
        assertTrue(f.sent.last().second.getBoolean("revealLocation"))
        assertEquals("folder", f.sent.last().second.getString("itemToken"))
        f.opened()
        f.browser.reveal(f.file)
        assertEquals("file", f.sent.last().second.getString("itemToken"))
        f.opened()
        val count = f.sent.size
        f.browser.info(true, false, wholePc = true, revealSupported = true)
        f.browser.reveal(f.folder)
        assertEquals(count, f.sent.size)
    }
    @Test fun modernHostStartsAtThisPcAndLegacyKeepsDesktop() {
        val modern = BrowserFixture(); modern.browser.info(true, true, wholePc = true)
        assertTrue(modern.sent.last().second.getBoolean("thisPc"))
        modern.list(); modern.browser.refresh()
        assertEquals("root", modern.sent.last().second.getString("folderToken"))
        val legacy = BrowserFixture(); legacy.browser.info(true, true)
        assertFalse(legacy.sent.last().second.has("thisPc"))
    }

    @Test fun decodesWindowsWireFixture() {
        val json = """{"requestId":"47fefc70-8740-4b89-9d0c-9462fc0d034b","folderToken":"root-token","path":"Desktop","parentToken":null,"items":[{"token":"file-token","name":"notes.txt","kind":"file","canBrowse":false,"canOpen":true,"error":null}],"error":null}"""
        val response = DesktopListResponse.decode(json.toByteArray())
        assertEquals("notes.txt", response.items.single().name)
        assertNull(response.parentToken)
        assertTrue(response.items.single().canOpen)
    }
    @Test fun folderNavigationBackAndRefresh() {
        val f = BrowserFixture(); f.browser.info(true, true)
        assertTrue(f.browser.state.loading)
        f.list(listOf(f.folder, f.file)); f.browser.tap(f.folder)
        assertEquals("folder", f.sent.last().second.getString("folderToken"))
        f.list(folder = "folder", parent = "root")
        assertEquals("Desktop\\Folder", f.browser.state.path)
        f.browser.refresh(); assertEquals("folder", f.sent.last().second.getString("folderToken"))
        f.list(folder = "folder", parent = "root")
        assertTrue(f.browser.up()); assertEquals("root", f.sent.last().second.getString("folderToken"))
        f.list(); assertFalse(f.browser.up())
    }
    @Test fun launchRequiresConfirmationAndSendsOnce() {
        val f = BrowserFixture(); f.browser.info(true, true); f.list(listOf(f.file))
        f.browser.tap(f.file); assertEquals(f.file, f.browser.state.confirmation)
        assertEquals(1, f.sent.size)
        f.browser.cancel(); f.browser.confirm(); assertEquals(1, f.sent.size)
        f.browser.tap(f.file); f.browser.confirm(); f.browser.confirm(); f.browser.tap(f.file)
        assertEquals(2, f.sent.size); assertTrue(f.sent.last().second.getBoolean("confirmed"))
        assertTrue(f.browser.state.opening)
        f.opened(); assertFalse(f.browser.state.opening); assertEquals("Opened on PC.", f.browser.state.notice)
    }
    @Test fun viewOnlyAndRevocationDismissConfirmation() {
        val f = BrowserFixture(); f.browser.info(true, false); f.list(listOf(f.file, f.folder))
        f.browser.tap(f.file); assertNull(f.browser.state.confirmation)
        f.browser.info(true, true); f.browser.tap(f.file); assertNotNull(f.browser.state.confirmation)
        f.browser.info(true, false); assertNull(f.browser.state.confirmation)
        f.browser.confirm(); assertEquals(1, f.sent.size)
        f.browser.tap(f.folder); assertEquals(2, f.sent.size)
    }
    @Test fun staleResponsesDisconnectAndReconnect() {
        val f = BrowserFixture(); f.browser.info(true, true)
        val old = f.sent.last().second.getString("requestId")
        f.browser.sessionLost(); f.browser.info(true, true)
        f.list(listOf(f.file), id = old); assertTrue(f.browser.state.loading)
        assertTrue(f.sent.last().second.isNull("folderToken"))
        f.list(); assertFalse(f.browser.state.loading)
        f.browser.sessionLost(); f.list(listOf(f.file)); assertTrue(f.browser.state.items.isEmpty())
    }
    @Test fun loadingErrorEmptyRetryAndTimeout() {
        val f = BrowserFixture(); f.browser.info(true, true); f.browser.refresh(); assertEquals(1, f.sent.size)
        f.list(error = "inaccessible"); assertEquals("inaccessible", f.browser.state.error)
        f.browser.retry(); f.list(); assertNull(f.browser.state.error); assertTrue(f.browser.state.items.isEmpty())
        f.browser.refresh(); f.timers.last().invoke(); assertEquals("timeout", f.browser.state.error)
        f.browser.retry(); f.list(listOf(f.file)); f.browser.tap(f.file); f.browser.confirm()
        val count = f.sent.size; f.timers.last().invoke()
        assertEquals(count, f.sent.size); assertFalse(f.browser.state.opening)
        assertTrue(f.browser.state.notice!!.contains("may have opened"))
    }
    @Test fun unsupportedHostAndFailedSend() {
        val f = BrowserFixture(); f.browser.info(false, true); assertTrue(f.sent.isEmpty())
        f.browser.sessionLost(); f.canSend = false; f.browser.info(true, true)
        assertEquals("disconnected", f.browser.state.error); assertFalse(f.browser.state.loading)
    }
    @Test fun malformedResponseCannotCrashOrCompletePendingRequest() {
        val f = BrowserFixture(); f.browser.info(true, true)
        f.browser.message(Msg.DESKTOP_LIST_RESULT, "not json".toByteArray())
        assertTrue(f.browser.state.loading)
        f.timers.last().invoke(); assertFalse(f.browser.state.loading)
    }
    @Test fun unchangedInfoPreservesFolderAndNoExtraRequests() {
        val f = BrowserFixture(); f.browser.info(true, true); f.list(listOf(f.folder))
        f.browser.tap(f.folder); f.list(folder = "folder", parent = "root")
        val count = f.sent.size; f.browser.info(true, true)
        assertEquals("folder", f.browser.state.folderToken); assertEquals(count, f.sent.size)
    }
}
