package com.deskhand.android

import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DesktopBrowserViewTest {
    @get:Rule val compose = createComposeRule()
    @Test fun fileIconsMatchTypeAndExtension() {
        val f = BrowserFixture()
        assertEquals("Folder", fileType(f.folder).label)
        assertEquals("Drive", fileType(f.folder.copy(name = "C:\\")).label)
        assertEquals("Document", fileType(f.file.copy(name = "NOTES.TXT")).label)
        assertEquals("Image", fileType(f.file.copy(name = "picture.jpg")).label)
        assertEquals("Video", fileType(f.file.copy(name = "movie.mp4")).label)
        assertEquals("Audio", fileType(f.file.copy(name = "song.mp3")).label)
        assertEquals("Archive", fileType(f.file.copy(name = "bundle.zip")).label)
        assertEquals("Application", fileType(f.file.copy(kind = "application")).label)
        assertEquals("Shortcut", fileType(f.file.copy(kind = "shortcut")).label)
        assertEquals("File", fileType(f.file.copy(name = "unknown.xyz")).label)
    }
    @Test fun rendersListAndConfirmationCancelThenOpen() {
        val f = BrowserFixture(); f.browser.info(true, true); f.list(listOf(f.folder, f.file))
        compose.setContent { MaterialTheme { DesktopBrowserView(f.browser, Modifier.size(400.dp, 600.dp)) } }
        compose.onNodeWithText("notes.txt").assertIsDisplayed().performClick()
        compose.onNodeWithText("Open on PC?").assertIsDisplayed()
        compose.onNodeWithText("Cancel").performClick()
        compose.onNodeWithText("Open on PC?").assertDoesNotExist()
        assertEquals(1, f.sent.size)
        compose.onNodeWithText("notes.txt").performClick(); compose.onNodeWithText("Open", useUnmergedTree = true).performClick()
        assertEquals(Msg.DESKTOP_OPEN, f.sent.last().first)
    }
    @Test fun compactRowsRetainTapTargetsAndRevealFullName() {
        val f = BrowserFixture(); f.browser.info(true, true)
        val name = "A very long filename that does not fit in the narrow drawer.txt"
        f.list(listOf(f.file.copy(name = name), f.folder))
        compose.setContent { MaterialTheme { DesktopBrowserView(f.browser, Modifier.size(200.dp, 600.dp)) } }
        val row = compose.onNodeWithText(name).fetchSemanticsNode().boundsInRoot
        val next = compose.onNodeWithText("Folder").fetchSemanticsNode().boundsInRoot
        assertTrue("Rows remain comfortably tappable", row.height >= 48f * compose.density.density)
        assertTrue("Rows use less than the old large layout", next.top - row.top <= 50f * compose.density.density)
        compose.onNodeWithText(name).performTouchInput { longClick() }
        compose.onNodeWithText("Full name").assertIsDisplayed()
        compose.onNodeWithText("Done").performClick()
        compose.onNodeWithText(name).performClick()
        compose.onNodeWithText("Open on PC?").assertIsDisplayed()
    }
    @Test fun loadingErrorRetryAndEmptyAreReadable() {
        val f = BrowserFixture(); f.browser.info(true, true)
        compose.setContent { MaterialTheme { DesktopBrowserView(f.browser, Modifier.size(400.dp, 600.dp)) } }
        compose.onNodeWithTag("desktopLoading").assertIsDisplayed()
        compose.runOnIdle { f.list(error = "inaccessible") }
        compose.onNodeWithText(desktopError("inaccessible")).assertIsDisplayed()
        compose.onNodeWithText("Retry").performClick()
        compose.runOnIdle { f.list() }
        compose.onNodeWithText("This folder is empty.").assertIsDisplayed()
    }
    @Test fun folderNavigationUpAndViewOnly() {
        val f = BrowserFixture(); f.browser.info(true, false); f.list(listOf(f.folder, f.file))
        compose.setContent { MaterialTheme { DesktopBrowserView(f.browser, Modifier.size(400.dp, 600.dp)) } }
        compose.onNodeWithText("View only", substring = true).assertIsDisplayed()
        compose.onNodeWithText("notes.txt").performClick()
        compose.onNodeWithText("Open on PC?").assertDoesNotExist()
        compose.onAllNodesWithText("Folder").onFirst().performClick()
        compose.runOnIdle { f.list(folder = "folder", parent = "root") }
        compose.onNodeWithText("Desktop").assertIsDisplayed()
        compose.onNodeWithText("Folder").assertIsDisplayed()
        compose.onNodeWithText("Up").performClick()
        assertEquals("root", f.sent.last().second.getString("folderToken"))
    }
    @Test fun searchFiltersCurrentFolderAndBreadcrumbReturnsToParent() {
        val f = BrowserFixture(); f.browser.info(true, true); f.list(listOf(f.file, f.folder))
        compose.setContent { MaterialTheme { DesktopBrowserView(f.browser, Modifier.size(300.dp, 700.dp)) } }
        compose.onNodeWithTag("fileSearch").performTextInput("NOTES")
        compose.onNodeWithText("notes.txt").assertIsDisplayed()
        compose.onNodeWithText("Folder").assertDoesNotExist()
        compose.onNodeWithTag("fileSearch").performTextClearance()
        compose.onNodeWithText("Folder").performClick()
        compose.runOnIdle { f.list(folder = "folder", parent = "root") }
        compose.onNodeWithText("Desktop").performClick()
        assertEquals("root", f.sent.last().second.getString("folderToken"))
    }
    // B2-G: portraitScrollKeepsStreamFixedAndLandscapeHidesBrowser deleted
    // with DesktopViewerContent (legacy split layout, no production caller).
    // The scroll behaviour it covered lives in DesktopBrowserView, covered
    // by the search/scroll tests above.
}
