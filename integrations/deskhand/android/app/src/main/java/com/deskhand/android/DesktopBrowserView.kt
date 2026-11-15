package com.deskhand.android

import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun DesktopBrowserView(browser: DesktopBrowserController, modifier: Modifier = Modifier) {
    val state = browser.state
    val busy = state.loading || state.opening
    var details by remember { mutableStateOf<DesktopItem?>(null) }
    var query by rememberSaveable { mutableStateOf("") }
    LaunchedEffect(state.folderToken) { query = "" }
    val breadcrumbScroll = rememberScrollState()
    LaunchedEffect(state.folderToken) {
        androidx.compose.runtime.withFrameNanos { }
        breadcrumbScroll.scrollTo(breadcrumbScroll.maxValue)
    }
    val visibleItems = state.items.filter { it.name.contains(query.trim(), ignoreCase = true) }
    Surface(modifier.testTag("desktopBrowser")) {
        Column(Modifier.fillMaxSize().padding(horizontal = 8.dp)) {
            Row(Modifier.fillMaxWidth()) {
                TextButton(onClick = { browser.up() }, enabled = state.parentToken != null && !busy && state.connected) { Text("Up") }
                TextButton(onClick = browser::refresh, enabled = state.connected && state.supported && !busy) { Text("Refresh") }
            }
            Row(Modifier.fillMaxWidth().horizontalScroll(breadcrumbScroll).testTag("desktopPath"), verticalAlignment = Alignment.CenterVertically) {
                if (state.crumbs.isEmpty()) Text(state.path, style = MaterialTheme.typography.labelLarge)
                state.crumbs.forEachIndexed { index, crumb ->
                    if (index > 0) Text(" / ", style = MaterialTheme.typography.labelMedium)
                    if (index == state.crumbs.lastIndex) Text(crumb.label, style = MaterialTheme.typography.labelLarge)
                    else Text(crumb.label, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.clickable(enabled = !busy && state.connected) { browser.navigate(crumb.token) }.padding(vertical = 8.dp))
                }
            }
            OutlinedTextField(value = query, onValueChange = { query = it }, singleLine = true,
                placeholder = { Text("Search this folder", maxLines = 1, style = MaterialTheme.typography.bodySmall) },
                modifier = Modifier.fillMaxWidth().testTag("fileSearch"))
            if (!state.control) Text("View only · browse folders; opening requires control permission.", style = MaterialTheme.typography.bodySmall)
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth().testTag("desktopLoading"))
            when {
                !state.connected -> Text("Connect to the PC to browse Desktop.")
                !state.supported -> Text("Desktop browsing is unavailable on this host version.")
                state.error != null -> Row {
                    Text(desktopError(state.error), Modifier.weight(1f))
                    TextButton(onClick = browser::retry, enabled = !busy) { Text("Retry") }
                }
                !state.loading && state.items.isEmpty() -> Text("This folder is empty.")
                !state.loading && visibleItems.isEmpty() -> Text("No matching files.")
            }
            state.notice?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            LazyColumn(Modifier.weight(1f).fillMaxWidth().testTag("desktopItems")) {
                itemsIndexed(visibleItems, key = { index, item -> item.token.ifEmpty { "unavailable-$index" } }) { _, item ->
                    val enabled = !busy && state.connected && (item.canBrowse || (state.control && (item.canOpen || (state.revealSupported && item.token.isNotBlank()))))
                    Row(
                        Modifier.fillMaxWidth().heightIn(min = 48.dp)
                            .combinedClickable(enabled = enabled, role = Role.Button,
                                onClick = { browser.tap(item) }, onLongClickLabel = if (state.revealSupported && state.control) "Open location on PC" else "Show full name",
                                onLongClick = { if (state.revealSupported && state.control && item.token.isNotBlank()) browser.reveal(item) else details = item })
                            .padding(horizontal = 4.dp, vertical = 4.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        val type = fileType(item)
                        Icon(painterResource(type.icon), contentDescription = type.label,
                            tint = if (item.kind == "unavailable") MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.primary,
                            modifier = Modifier.size(24.dp))
                        Spacer(Modifier.width(8.dp))
                        Column(Modifier.weight(1f)) {
                            Text(item.name, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurface, maxLines = 1,
                                overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
                            item.error?.let { Text(desktopError(it), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                        }
                    }
                }
                if (state.nextCursor != null) item {
                    TextButton(onClick = browser::loadMore, enabled = !busy, modifier = Modifier.fillMaxWidth()) { Text("Load more") }
                }
            }
        }
    }
    details?.let { item ->
        AlertDialog(onDismissRequest = { details = null }, title = { Text("Full name") },
            text = { Text(item.name) }, confirmButton = { TextButton(onClick = { details = null }) { Text("Done") } })
    }
    state.confirmation?.let { item ->
        AlertDialog(onDismissRequest = browser::cancel,
            title = { Text("Open on PC?") },
            text = { Text("Open “${item.name}” using Windows? Shortcuts may open an application or a location outside Desktop.") },
            confirmButton = { TextButton(onClick = browser::confirm) { Text("Open") } },
            dismissButton = { TextButton(onClick = browser::cancel) { Text("Cancel") } })
    }
}

// B2-G: DesktopViewerContent (legacy split stream+browser layout) removed —
// no production caller; the live viewer uses ViewerLayout + DesktopBrowserView.

internal data class FileType(val icon: Int, val label: String)

internal fun fileType(item: DesktopItem): FileType {
    if (item.kind == "folder") return if (item.name.matches(Regex("^[A-Za-z]:[\\\\/]?$")))
        FileType(R.drawable.file_type_drive, "Drive") else FileType(R.drawable.file_type_folder, "Folder")
    when (item.kind) {
        "shortcut" -> return FileType(R.drawable.file_type_shortcut, "Shortcut")
        "application" -> return FileType(R.drawable.file_type_application, "Application")
    }
    return when (item.name.substringAfterLast('.', "").lowercase(java.util.Locale.ROOT)) {
        "txt", "md", "rtf", "doc", "docx", "pdf", "odt" -> FileType(R.drawable.file_type_document, "Document")
        "png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "heic" -> FileType(R.drawable.file_type_image, "Image")
        "mp4", "mkv", "avi", "mov", "webm", "wmv" -> FileType(R.drawable.file_type_video, "Video")
        "mp3", "wav", "flac", "aac", "ogg", "m4a" -> FileType(R.drawable.file_type_audio, "Audio")
        "zip", "rar", "7z", "tar", "gz" -> FileType(R.drawable.file_type_archive, "Archive")
        else -> FileType(R.drawable.file_type_file, "File")
    }
}
