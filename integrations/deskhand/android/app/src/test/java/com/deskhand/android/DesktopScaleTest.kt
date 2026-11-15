package com.deskhand.android

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.unit.IntSize
import org.junit.Assert.*
import org.junit.Test

class DesktopScaleTest {
    @Test fun zoomControlsClampAndResetPan() {
        val zoom = DesktopZoomState()
        val viewport = IntSize(1000, 600)
        zoom.geometry(viewport, desktopBaseSize(viewport, 1.6f, DesktopScale.Fit))
        zoom.change(3f, Offset(100f, 100f))
        assertEquals(2f, zoom.zoom, 0f)  // 100.011: clamps to ZOOM_MAX (was 3f in the 1-6x range)
        assertNotEquals(Offset.Zero, zoom.pan)
        zoom.change(20f)
        assertEquals(2f, zoom.zoom, 0f)  // 100.011: clamps to ZOOM_MAX (was 6f in the 1-6x range)
        zoom.reset()
        assertEquals(1.5f, zoom.zoom, 0f)  // 100.011: resets to ZOOM_DEFAULT (was 1f)
        assertEquals(Offset.Zero, zoom.pan)
    }

    @Test fun sessionStartFitPreservesUnderlyingZoom() {
        val zoom = DesktopZoomState()
        val entry = ZoomEntry(zoom = 1.75f, panX = 0f, panY = 0f, frameWidth = 1920, frameHeight = 1080, updatedAt = 0L)
        zoom.restore(entry, ZOOM_DEFAULT)
        assertEquals(1.75f, zoom.zoom, 0f)
        assertFalse(zoom.fit)
        zoom.applyFit(true)
        assertTrue(zoom.fit)
        assertEquals(1.75f, zoom.zoom, 0f)
    }

    @Test fun preciseCoordinatesAndLensStayWithinBounds() {
        val viewport = IntSize(400, 600)
        val fit = desktopBaseSize(viewport, 2f, DesktopScale.Fit)
        assertNull(desktopPoint(Offset(100f, 20f), viewport, fit, 1f, Offset.Zero))
        assertEquals(Offset(0.5f, 0.5f), desktopPoint(Offset(200f, 300f), viewport, fit, 1f, Offset.Zero))
        val stretch = desktopBaseSize(viewport, 2f, DesktopScale.Stretch)
        assertEquals(Offset(0.25f, 0.75f), desktopPoint(Offset(100f, 450f), viewport, stretch, 1f, Offset.Zero))
        assertEquals(Offset(0.5f, 0.5f), desktopPoint(Offset(250f, 320f), viewport, stretch, 2f, Offset(50f, 20f)))
        val top = magnifierCenter(Offset(1f, 2f), viewport, 50f, 20f)
        assertTrue(top.x >= 50f && top.y >= 50f && top.y > 2f)
        val bottom = magnifierCenter(Offset(399f, 599f), viewport, 50f, 20f)
        assertTrue(bottom.x <= 350f && bottom.y <= 550f && bottom.y < 599f)
    }

    @Test fun portraitStretchUsesEmptyHeightAndKeepsWholeDesktop() {
        val viewport = IntSize(1080, 1320)
        val fit = desktopBaseSize(viewport, 1.6f, DesktopScale.Fit)
        val fill = desktopBaseSize(viewport, 1.6f, DesktopScale.Stretch)
        assertEquals(675f, fit.height, 0.01f)
        assertEquals(1320f, fill.height, 0.01f)
        assertEquals(1080f, fill.width, 0.01f)
        assertEquals(Offset.Zero, clampDesktopPan(Offset(500f, -500f), viewport, fill, 1f))
    }

    @Test fun stretchedDesktopEdgesMatchViewportWithoutCropping() {
        val viewport = IntSize(1080, 1320)
        val size = desktopBaseSize(viewport, 1.6f, DesktopScale.Stretch)
        val right = clampDesktopPan(Offset(10000f, 10000f), viewport, size, 1f)
        val left = clampDesktopPan(Offset(-10000f, -10000f), viewport, size, 1f)
        assertEquals(0f, viewport.width / 2f + right.x - size.width / 2f, 0.01f)
        assertEquals(viewport.width.toFloat(), viewport.width / 2f + left.x + size.width / 2f, 0.01f)
        assertEquals(0f, right.y, 0f)
        assertEquals(0f, left.y, 0f)
    }

    @Test fun landscapeFitPreservesEntireDesktopAndStretchUsesExactBounds() {
        val viewport = IntSize(2116, 861)
        val fit = desktopBaseSize(viewport, 1.6f, DesktopScale.Fit)
        assertTrue(fit.width <= viewport.width && fit.height <= viewport.height)
        assertEquals(1.6f, fit.width / fit.height, 0.001f)
        assertEquals(Offset.Zero, clampDesktopPan(Offset(50f, 50f), viewport, fit, 1f))
        val stretch = desktopBaseSize(viewport, 1.6f, DesktopScale.Stretch)
        assertEquals(2116f, stretch.width, 0f)
        assertEquals(861f, stretch.height, 0f)
    }
}
