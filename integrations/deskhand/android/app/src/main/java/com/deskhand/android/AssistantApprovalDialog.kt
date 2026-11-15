package com.deskhand.android

import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import org.json.JSONObject

@Composable
internal fun AssistantApprovalDialog(request: JSONObject, onDecision: (Boolean) -> Unit, onStop: () -> Unit) {
    AlertDialog(
        onDismissRequest = {},
        title = { Text("AI action approval") },
        text = { Text("${request.optString("operation")}\n\nTarget: ${request.optString("target")}\n\nPurpose: ${request.optString("purpose")}", modifier = Modifier.verticalScroll(rememberScrollState())) },
        confirmButton = { TextButton(onClick = { onDecision(true) }) { Text("Approve") } },
        dismissButton = { Row {
            TextButton(onClick = onStop) { Text("Deny") }
        } }
    )
}
