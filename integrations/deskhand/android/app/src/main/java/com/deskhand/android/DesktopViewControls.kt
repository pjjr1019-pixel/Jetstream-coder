package com.deskhand.android

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

// B2-G B2: "Fit" / "Show full desktop" drive the fit view override
// (zoomState.applyFit), not just the base size — DesktopScale.Fit alone
// only changes the base size, so at 1.5-2.0x the desktop stayed cropped.
// B2-G B4: the zoom menu steps through the same capped + snapped path as
// pinch release and the settings steppers (see DesktopZoomState.stepZoom).
@Composable
internal fun DesktopViewControls(scale: DesktopScale, zoom: DesktopZoomState, onScale: (DesktopScale) -> Unit) {
    var menu by remember { mutableStateOf(false) }
    GlassSurface(shape = MaterialTheme.shapes.small) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = { zoom.applyFit(true); onScale(DesktopScale.Fit) }, modifier = Modifier.testTag("fitDesktop")
                .semantics { contentDescription = "Fit entire desktop" }, contentPadding = PaddingValues(horizontal = 6.dp)) {
                Text(if (scale == DesktopScale.Fit) "• Fit" else "Fit", style = MaterialTheme.typography.labelMedium)
            }
            TextButton(onClick = { zoom.applyFit(false); onScale(DesktopScale.Stretch) }, modifier = Modifier.testTag("stretchDesktop")
                .semantics { contentDescription = "Stretch desktop" }, contentPadding = PaddingValues(horizontal = 6.dp)) {
                Text(if (scale == DesktopScale.Stretch) "• Stretch" else "Stretch", style = MaterialTheme.typography.labelMedium)
            }
            Box {
                TextButton(onClick = { menu = true }, modifier = Modifier.testTag("zoomMenu")
                    .semantics { contentDescription = "Zoom controls" }, contentPadding = PaddingValues(horizontal = 6.dp)) {
                    Text("Zoom", style = MaterialTheme.typography.labelMedium)
                }
                DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                    Text("${(zoom.zoom * 100).roundToInt()}% · Pinch to zoom", modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                        style = MaterialTheme.typography.labelMedium)
                    DropdownMenuItem(text = { Text("Zoom in") }, onClick = { zoom.stepZoom(1.5f, Model.zoomCap) }, enabled = zoom.zoom < Model.zoomCap)
                    DropdownMenuItem(text = { Text("Zoom out") }, onClick = { zoom.stepZoom(1f / 1.5f, Model.zoomCap) }, enabled = zoom.zoom > ZOOM_MIN)
                    DropdownMenuItem(text = { Text("Show full desktop") }, onClick = { zoom.applyFit(true); onScale(DesktopScale.Fit); menu = false })
                }
            }
        }
    }
}
