package com.deskhand.android

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.unit.IntSize
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 100.011 VM unit tests — zoom transform math (spec §12, supplement to the
 * phone tests Z-T1–Z-T10). Pure functions only; no Android framework needed.
 */
class ZoomMathTest {

    @Test
    fun snapZoom_snapsToNearest005() {
        assertEquals(1.50f, snapZoom(1.5f), 0.001f)
        assertEquals(1.25f, snapZoom(1.27f), 0.001f)
        assertEquals(1.30f, snapZoom(1.28f), 0.001f)
        assertEquals(1.60f, snapZoom(1.62f), 0.001f)
        assertEquals(1.65f, snapZoom(1.63f), 0.001f)
        assertEquals(2.00f, snapZoom(2.0f), 0.001f)
    }

    @Test
    fun snapZoom_clampsToRange() {
        // Below the floor (including the old 1.0x default) snaps up to ZOOM_MIN.
        assertEquals(ZOOM_MIN, snapZoom(1.0f), 0.001f)
        assertEquals(ZOOM_MIN, snapZoom(1.24f), 0.001f)
        // Above the ceiling snaps down to ZOOM_MAX.
        assertEquals(ZOOM_MAX, snapZoom(2.5f), 0.001f)
        assertEquals(ZOOM_MAX, snapZoom(6.0f), 0.001f)
        // Non-finite input falls back to the default.
        assertEquals(ZOOM_DEFAULT, snapZoom(Float.NaN), 0.001f)
        assertEquals(ZOOM_DEFAULT, snapZoom(Float.POSITIVE_INFINITY), 0.001f)
    }

    @Test
    fun clampZoom_respectsFloorAndCap() {
        assertEquals(ZOOM_MIN, clampZoom(1.0f, ZOOM_MAX), 0.001f)
        assertEquals(1.50f, clampZoom(1.5f, ZOOM_MAX), 0.001f)
        assertEquals(ZOOM_MAX, clampZoom(3.0f, ZOOM_MAX), 0.001f)
        // A live Low-mode cap (spec §9) ceilings the zoom below ZOOM_MAX.
        assertEquals(1.25f, clampZoom(1.8f, 1.25f), 0.001f)
        assertEquals(1.25f, clampZoom(1.25f, 1.25f), 0.001f)
        assertEquals(ZOOM_DEFAULT, clampZoom(Float.NaN, ZOOM_MAX), 0.001f)
    }

    @Test
    fun effectiveZoom_isMinOfDesiredAndCap() {
        assertEquals(1.75f, effectiveZoom(1.75f, ZOOM_MAX), 0.001f)
        assertEquals(1.25f, effectiveZoom(1.75f, 1.25f), 0.001f)
        assertEquals(2.00f, effectiveZoom(2.00f, 2.00f), 0.001f)
    }

    @Test
    fun clampDesktopPan_zoomedLimitsToVisibleRange() {
        val viewport = IntSize(400, 800)
        val size = Size(400f, 800f)
        // zoom 1.5 → shown 600x1200; pan range x ±100, y ±200.
        assertEquals(Offset(100f, 200f), clampDesktopPan(Offset(150f, 300f), viewport, size, 1.5f))
        assertEquals(Offset(-100f, -200f), clampDesktopPan(Offset(-150f, -300f), viewport, size, 1.5f))
        assertEquals(Offset(50f, -80f), clampDesktopPan(Offset(50f, -80f), viewport, size, 1.5f))
        // zoom 1.0 → the image fits the viewport; pan is forced to zero.
        assertEquals(Offset.Zero, clampDesktopPan(Offset(50f, 50f), viewport, size, 1.0f))
    }

    @Test
    fun panToSource_centredPanIsFrameCentre() {
        val box = IntSize(800, 600)
        val size = desktopBaseSize(box, 1600f / 900f, DesktopScale.Fit)
        val centre = panToSource(Offset.Zero, box, size, 1.5f, 1600, 900)
        assertEquals(800f, centre.x, 0.01f)
        assertEquals(450f, centre.y, 0.01f)
    }

    @Test
    fun panRoundTrip_restoresTheSamePan() {
        val box = IntSize(800, 600)
        val size = desktopBaseSize(box, 1600f / 900f, DesktopScale.Fit)
        val zoom = 1.5f
        val pan = Offset(100f, 20f)
        val centre = panToSource(pan, box, size, zoom, 1600, 900)
        val back = sourceToPan(centre, 1600, 900, box, size, zoom, 1600, 900)
        assertEquals(pan.x, back.x, 0.01f)
        assertEquals(pan.y, back.y, 0.01f)
    }

    @Test
    fun sourceToPan_scalesProportionallyToLiveFrame() {
        val box = IntSize(800, 600)
        val size = desktopBaseSize(box, 1600f / 900f, DesktopScale.Fit)
        // Saved on a 800x450 frame with the centre at the frame centre.
        val centred = sourceToPan(Offset(400f, 225f), 800, 450, box, size, 1.5f, 1600, 900)
        assertEquals(Offset.Zero, centred)
        // Saved centre at the left quarter of the old frame → live left
        // quarter; the pan is bounded by the live clamp (±200, ±37.5).
        val quarter = sourceToPan(Offset(200f, 112.5f), 800, 450, box, size, 1.5f, 1600, 900)
        assertTrue(quarter.x > 0f && quarter.x <= 200f)
        assertTrue(quarter.y >= -37.5f && quarter.y <= 37.5f)
    }

    @Test
    fun sourceToPan_invalidCentreFallsBackToCentre() {
        val box = IntSize(800, 600)
        val size = desktopBaseSize(box, 1600f / 900f, DesktopScale.Fit)
        assertEquals(Offset.Zero, sourceToPan(Offset(Float.NaN, 0f), 1600, 900, box, size, 1.5f, 1600, 900))
        assertEquals(Offset.Zero, sourceToPan(Offset(800f, 450f), 0, 0, box, size, 1.5f, 1600, 900))
        assertEquals(Offset.Zero, sourceToPan(Offset(800f, 450f), 1600, 900, box, size, 1.5f, 0, 0))
    }

    @Test
    fun sourceToPan_outOfBoundsCentreIsClampedNotLost() {
        val box = IntSize(800, 600)
        val size = desktopBaseSize(box, 1600f / 900f, DesktopScale.Fit)
        // A stale centre far outside the frame clamps to the frame edge and
        // then to the live pan range — never a black edge (spec §8.3 rule 3).
        val p = sourceToPan(Offset(5000f, 5000f), 1600, 900, box, size, 1.5f, 1600, 900)
        assertEquals(-200f, p.x, 0.01f)
        assertEquals(-37.5f, p.y, 0.01f)
    }

    @Test
    fun desktopBaseSize_fitKeepsAspect() {
        val size = desktopBaseSize(IntSize(800, 600), 1600f / 900f, DesktopScale.Fit)
        assertEquals(800f, size.width, 0.01f)
        assertEquals(450f, size.height, 0.01f)
        val stretched = desktopBaseSize(IntSize(800, 600), 1600f / 900f, DesktopScale.Stretch)
        assertEquals(800f, stretched.width, 0.01f)
        assertEquals(600f, stretched.height, 0.01f)
    }
}
