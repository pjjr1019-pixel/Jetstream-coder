package com.deskhand.android

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.unit.IntSize

internal enum class DesktopScale(val label: String) { Fit("Fit"), Stretch("Stretch") }

internal fun desktopBaseSize(viewport: IntSize, aspect: Float, mode: DesktopScale): Size {
    if (viewport.width <= 0 || viewport.height <= 0 || aspect <= 0f) return Size.Zero
    val width = viewport.width.toFloat()
    val height = viewport.height.toFloat()
    if (mode == DesktopScale.Stretch) return Size(width, height)
    val widthLimited = width / height <= aspect
    return if (widthLimited) Size(width, width / aspect)
        else Size(height * aspect, height)
}

internal fun clampDesktopPan(pan: Offset, viewport: IntSize, size: Size, zoom: Float): Offset {
    val x = maxOf(0f, (size.width * zoom - viewport.width) / 2f)
    val y = maxOf(0f, (size.height * zoom - viewport.height) / 2f)
    return Offset(if (x == 0f) 0f else pan.x.coerceIn(-x, x), if (y == 0f) 0f else pan.y.coerceIn(-y, y))
}

/** Shared by gestures and the visible zoom controls. */
internal class DesktopZoomState {
    var zoom by mutableFloatStateOf(ZOOM_DEFAULT)
        private set
    var pan by mutableStateOf(Offset.Zero)
        private set
    /** Zoom-to-fit override (spec §3.3): when true the view draws at 1.0x
     * centred, without touching `zoom`/`pan` — toggling back restores them
     * exactly. A fit is a view, never a stored preference. */
    var fit by mutableStateOf(false)
        private set
    /** Session-start restore waiting for geometry (spec §8.3): applied by
     * DesktopView's geometry effect once the viewport and frame are known.
     * B2-G B5: observable state, so the geometry effect can key on it and
     * consume it exactly once — as a plain var it was invisible to
     * composition and the effect never re-ran for a new entry. */
    var pendingEntry: ZoomEntry? by mutableStateOf(null)
    // Last known interactive-view geometry, for converting pan ↔ source
    // coordinates when persisting from the settings rows.
    var lastBox: IntSize = IntSize.Zero
    var lastSize: Size = Size.Zero
    var lastBmp: IntSize = IntSize.Zero
    private var viewport = IntSize.Zero
    private var base = Size.Zero
    fun geometry(viewport: IntSize, base: Size) {
        this.viewport = viewport
        this.base = base
        pan = clampDesktopPan(pan, viewport, base, zoom)
    }
    fun change(newZoom: Float, newPan: Offset = pan) {
        zoom = newZoom.coerceIn(ZOOM_MIN, ZOOM_MAX)
        pan = clampDesktopPan(newPan, viewport, base, zoom)
    }
    /** B2-G B4: zoom-menu step through the same capped + snapped path as
     * pinch release and the settings steppers — a menu step can never
     * store an off-grid or over-cap factor. */
    fun stepZoom(factor: Float, cap: Float) {
        change(clampZoom(snapZoom(zoom * factor), cap))
    }
    fun reset() { zoom = ZOOM_DEFAULT; pan = Offset.Zero; fit = false; pendingEntry = null }
    fun applyFit(f: Boolean) { fit = f }
    /** Session-start restore (spec §8.3): factor now, pan when geometry lands. */
    fun restore(entry: ZoomEntry?, globalDefault: Float) {
        zoom = if (entry == null) snapZoom(globalDefault) else snapZoom(entry.zoom)
        pan = Offset.Zero
        fit = false
        pendingEntry = entry
    }
    /** The live pan as a source-frame centre, using the last known geometry. */
    fun currentPanCentre(): Offset {
        if (lastBmp.width <= 0 || lastBmp.height <= 0) return Offset(lastBmp.width / 2f, lastBmp.height / 2f)
        return panToSource(pan, lastBox, lastSize, zoom, lastBmp.width, lastBmp.height)
    }
    /** Persist the live zoom + pan for the connected PC (spec §8.4). No-op
     * before the first frame (no geometry to convert against) or while
     * disconnected (Model.saveZoomForCurrentPc no-ops without a viewer PC). */
    fun persist() {
        if (lastBmp.width <= 0 || lastBmp.height <= 0) return
        Model.saveZoomForCurrentPc(zoom, currentPanCentre(), lastBmp.width, lastBmp.height)
    }
}


/** Coordinates in the displayed desktop, or null in letterboxing/outside the viewport. */
internal fun desktopPoint(point: Offset, viewport: IntSize, base: Size, zoom: Float, pan: Offset): Offset? {
    val rawWidth = base.width * zoom
    val rawHeight = base.height * zoom
    val width = rawWidth.toInt().toFloat()
    val height = rawHeight.toInt().toFloat()
    if (width <= 0f || height <= 0f || point.x < 0f || point.y < 0f || point.x > viewport.width || point.y > viewport.height) return null
    val left = (viewport.width / 2f + pan.x - rawWidth / 2f).toInt().toFloat()
    val top = (viewport.height / 2f + pan.y - rawHeight / 2f).toInt().toFloat()
    val x = (point.x - left) / width
    val y = (point.y - top) / height
    return if (x in 0f..1f && y in 0f..1f) Offset(x, y) else null
}

internal fun magnifierCenter(point: Offset, viewport: IntSize, radius: Float, gap: Float): Offset {
    val x = point.x.coerceIn(radius, viewport.width - radius)
    val above = point.y - gap - radius
    val preferred = if (above >= radius) above else point.y + gap + radius
    return Offset(x, preferred.coerceIn(radius, viewport.height - radius))
}

// ---- 100.011 video enlargement (spec §2.2, §3.1, §8.1) ----
// While zoomed the factor lives in [ZOOM_MIN, ZOOM_MAX]. ZOOM_FIT (1.0x)
// is zoom-to-fit only, reached via the fit toggle — never by pinching,
// never stored in the per-PC map.

internal const val ZOOM_MIN = 1.25f
internal const val ZOOM_MAX = 2.0f
internal const val ZOOM_DEFAULT = 1.5f
internal const val ZOOM_FIT = 1.0f

/** Snap a zoom factor to the nearest 0.05 step, clamped to [ZOOM_MIN, ZOOM_MAX]. */
internal fun snapZoom(z: Float): Float {
    if (!z.isFinite()) return ZOOM_DEFAULT
    val snapped = kotlin.math.round(z / 0.05f) * 0.05f
    return snapped.coerceIn(ZOOM_MIN, ZOOM_MAX)
}

/** Clamp a desired zoom to the live range: floor ZOOM_MIN, ceiling the live cap. */
internal fun clampZoom(z: Float, cap: Float): Float {
    if (!z.isFinite()) return ZOOM_DEFAULT
    return z.coerceIn(ZOOM_MIN, cap.coerceAtLeast(ZOOM_MIN))
}

/**
 * Effective (rendered) zoom: the stored/desired factor, capped by the live
 * data-saver cap. The cap is a live constraint only — it never writes to
 * storage (spec §9). With no data saver landed, the cap is ZOOM_MAX and
 * the effective zoom equals the desired zoom.
 */
internal fun effectiveZoom(desired: Float, cap: Float): Float = desired.coerceAtMost(cap)

/**
 * Convert the live screen-px pan offset to the source-frame coordinates of
 * the visible window's centre (spec §8.1). `box` is the viewport, `size`
 * the base size, `zoom` the live zoom, `bmpW`/`bmpH` the frame size.
 * The live pan is a screen-px offset from centre, so the centre of the
 * visible window in source coordinates is (bmpW/2 - pan.x * bmpW/shownW,
 * bmpH/2 - pan.y * bmpH/shownH). Pan Zero (centred) maps to the frame centre.
 */
internal fun panToSource(pan: Offset, box: IntSize, size: Size, zoom: Float, bmpW: Int, bmpH: Int): Offset {
    val shownW = size.width * zoom
    val shownH = size.height * zoom
    if (shownW <= 0f || shownH <= 0f || bmpW <= 0 || bmpH <= 0) return Offset(bmpW / 2f, bmpH / 2f)
    return Offset(
        bmpW / 2f - pan.x * bmpW / shownW,
        bmpH / 2f - pan.y * bmpH / shownH,
    )
}

/**
 * Convert a stored source-frame centre back to a screen-px pan offset for
 * the live geometry. When the saved frame size differs from the live frame,
 * the centre is scaled proportionally first (spec §8.3 rule 2), then the
 * result is clamped to the live frame — never out-of-bounds. Invalid
 * centres (NaN, or a saved frame <= 0) fall back to centre (Offset.Zero
 * pan, rule 3). The centre is also clamped into the live frame bounds
 * before converting, so a stale corner can never produce a black edge.
 */
internal fun sourceToPan(
    centre: Offset,
    savedFrameW: Int,
    savedFrameH: Int,
    box: IntSize,
    size: Size,
    zoom: Float,
    bmpW: Int,
    bmpH: Int,
): Offset {
    if (!centre.x.isFinite() || !centre.y.isFinite() || savedFrameW <= 0 || savedFrameH <= 0 || bmpW <= 0 || bmpH <= 0) {
        return Offset.Zero
    }
    val cx = (centre.x * bmpW.toFloat() / savedFrameW).coerceIn(0f, bmpW.toFloat())
    val cy = (centre.y * bmpH.toFloat() / savedFrameH).coerceIn(0f, bmpH.toFloat())
    val shownW = size.width * zoom
    val shownH = size.height * zoom
    if (shownW <= 0f || shownH <= 0f) return Offset.Zero
    val pan = Offset((bmpW / 2f - cx) * shownW / bmpW, (bmpH / 2f - cy) * shownH / bmpH)
    return clampDesktopPan(pan, box, size, zoom)
}
