package com.deskhand.android

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.activity.compose.LocalActivity
import androidx.compose.ui.unit.dp

@Composable
fun UpdateDialog() {
    val state = AppUpdates.ui
    val activity = LocalActivity.current
    if (!state.show) return
    AlertDialog(
        onDismissRequest = { AppUpdates.hide() },
        title = { Text("Deskhand updates") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                state.release?.let { Text("Version ${it.versionName} · ${"%.1f".format(it.size / 1048576.0)} MB") }
                Text(state.status)
                if (state.downloading) {
                    LinearProgressIndicator(progress = { state.progress / 100f }, modifier = Modifier.fillMaxWidth())
                    Text("${state.progress}%")
                }
                Text("Updates come from your paired PC. Your pairing and settings are kept.")
                if (!state.downloading) TextButton(onClick = { AppUpdates.check() }) { Text("Check for updates") }
            }
        },
        confirmButton = {
            when {
                state.downloading -> TextButton(onClick = { AppUpdates.cancel() }) { Text("Cancel download") }
                state.ready -> TextButton(onClick = { activity?.let { AppUpdates.install(it) } }, enabled = activity != null) { Text("Install") }
                state.release != null -> TextButton(onClick = { AppUpdates.startDownload() }) { Text("Update / Retry") }
            }
        },
        dismissButton = { TextButton(onClick = { AppUpdates.hide() }) { Text("Later") } },
    )
}
