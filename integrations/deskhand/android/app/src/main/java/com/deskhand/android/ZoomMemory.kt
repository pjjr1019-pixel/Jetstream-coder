package com.deskhand.android

import android.content.Context
import android.content.SharedPreferences
import androidx.compose.ui.geometry.Offset
import org.json.JSONObject

// ---- 100.011 per-PC zoom memory (spec §8), deskhand-work tree ----
// Phone-side only: a SharedPreferences document ("zoom-memory") holding one
// entry per paired PC, keyed by PairedPc.id — the one-way HKDF room id
// (Kdf.roomId(secret)), never the pairing secret, never the display name.
// The map is never logged, never exported, never sent to the PC.
// One JSON document under a single key, so every read/write is atomic.
// Store design is tree-independent (same as the 100.011 orientation-fix
// implementation); only the call sites differ (DesktopZoomState/Model).

/** One remembered zoom entry per paired PC (spec §8.1). panX/panY are the
 * visible window's centre in source-frame coordinates (see panToSource). */
data class ZoomEntry(
    val zoom: Float,
    val panX: Float,
    val panY: Float,
    val frameWidth: Int,
    val frameHeight: Int,
    val updatedAt: Long,
)

internal const val ZOOM_MEMORY_MAX_ENTRIES = 10
internal const val ZOOM_MEMORY_PREFS = "zoom-memory"
internal const val ZOOM_MEMORY_KEY = "zoom_store_v1"

/**
 * Decoded zoom store. `loadFailed` is true when the stored JSON was corrupt
 * and the map was discarded (defaults used instead). Only the flag is ever
 * surfaced — never the map contents (spec §8.3, §8.5).
 */
internal data class ZoomStoreData(
    val globalDefaultZoom: Float,
    val contrastOn: Boolean,
    val entries: Map<String, ZoomEntry>,
    val loadFailed: Boolean = false,
)

/** Pure codec: encode the store to a JSON string (no Android dependency, unit-testable). */
internal fun encodeZoomStore(data: ZoomStoreData): String {
    val root = JSONObject()
    root.put("version", 1)
    root.put("globalDefaultZoom", data.globalDefaultZoom.toDouble())
    root.put("contrastOn", data.contrastOn)
    val entries = JSONObject()
    for ((id, e) in data.entries) {
        val o = JSONObject()
        o.put("zoom", e.zoom.toDouble())
        o.put("panX", e.panX.toDouble())
        o.put("panY", e.panY.toDouble())
        o.put("frameWidth", e.frameWidth)
        o.put("frameHeight", e.frameHeight)
        o.put("updatedAt", e.updatedAt)
        entries.put(id, o)
    }
    root.put("entries", entries)
    return root.toString()
}

/**
 * Pure codec: decode a stored JSON string. Returns a store with
 * `loadFailed = true` and an empty map when the document is corrupt (the
 * map is discarded, the global default survives when it parses, otherwise
 * ZOOM_DEFAULT). A single bad entry is skipped, not fatal to the rest.
 */
internal fun decodeZoomStore(json: String?): ZoomStoreData {
    if (json.isNullOrBlank()) return ZoomStoreData(ZOOM_DEFAULT, true, emptyMap())
    return try {
        val root = JSONObject(json)
        var globalDefault = ZOOM_DEFAULT
        if (root.has("globalDefaultZoom")) {
            val g = root.optDouble("globalDefaultZoom", ZOOM_DEFAULT.toDouble()).toFloat()
            if (g.isFinite()) globalDefault = snapZoom(g)
        }
        val contrastOn = root.optBoolean("contrastOn", true)
        val entries = mutableMapOf<String, ZoomEntry>()
        val obj = root.optJSONObject("entries")
        if (obj != null) {
            val keys = obj.keys()
            while (keys.hasNext()) {
                val id = keys.next()
                val e = obj.optJSONObject(id) ?: continue
                try {
                    val zoom = e.optDouble("zoom", ZOOM_DEFAULT.toDouble()).toFloat()
                    val panX = e.optDouble("panX", 0.0).toFloat()
                    val panY = e.optDouble("panY", 0.0).toFloat()
                    val fw = e.optInt("frameWidth", 0)
                    val fh = e.optInt("frameHeight", 0)
                    val at = e.optLong("updatedAt", 0L)
                    if (!zoom.isFinite() || !panX.isFinite() || !panY.isFinite()) continue
                    entries[id] = ZoomEntry(snapZoom(zoom), panX, panY, fw, fh, at)
                } catch (_: Exception) {
                    // One bad entry is skipped; the rest of the map survives.
                }
            }
        }
        ZoomStoreData(globalDefault, contrastOn, entries)
    } catch (_: Exception) {
        ZoomStoreData(ZOOM_DEFAULT, true, emptyMap(), loadFailed = true)
    }
}

/**
 * Pure prune (spec §8.5): keep at most [maxEntries] entries. The
 * least-recently-used entry (smallest updatedAt) is dropped first, but a
 * currently-paired PC (in [protectedIds]) is never dropped.
 */
internal fun pruneZoomEntries(
    entries: Map<String, ZoomEntry>,
    maxEntries: Int,
    protectedIds: Set<String>,
): Map<String, ZoomEntry> {
    if (entries.size <= maxEntries) return entries
    val kept = entries.toMutableMap()
    val droppable = kept.entries
        .filter { it.key !in protectedIds }
        .sortedBy { it.value.updatedAt }
    var toDrop = kept.size - maxEntries
    for (e in droppable) {
        if (toDrop <= 0) break
        kept.remove(e.key)
        toDrop--
    }
    return kept
}

/** Result of restoring one PC's entry for a session (spec §8.3).
 * panCentre null always means "centre the view" — no entry, no live frame
 * yet, or an invalid/stale entry. */
internal data class ZoomRestore(val zoom: Float, val panCentre: Offset?)

/**
 * Pure restore (spec §8.3): no entry → the global default with a centred
 * view. With an entry → the stored zoom (clamped to [ZOOM_MIN, ZOOM_MAX])
 * and the stored pan centre, scaled proportionally to the live frame
 * (rule 2). An invalid or stale entry falls back to the centre (rule 3:
 * never a black edge).
 */
internal fun restoreZoomEntry(
    entry: ZoomEntry?,
    globalDefault: Float,
    liveFrameW: Int,
    liveFrameH: Int,
): ZoomRestore {
    val zoom = if (entry == null) snapZoom(globalDefault) else snapZoom(entry.zoom)
    if (entry == null || liveFrameW <= 0 || liveFrameH <= 0) return ZoomRestore(zoom, null)
    if (entry.frameWidth <= 0 || entry.frameHeight <= 0 || !entry.panX.isFinite() || !entry.panY.isFinite()) {
        return ZoomRestore(zoom, null)
    }
    val cx = (entry.panX * liveFrameW.toFloat() / entry.frameWidth).coerceIn(0f, liveFrameW.toFloat())
    val cy = (entry.panY * liveFrameH.toFloat() / entry.frameHeight).coerceIn(0f, liveFrameH.toFloat())
    return ZoomRestore(zoom, Offset(cx, cy))
}

/**
 * Phone-side per-PC zoom memory. The store is cached in memory after the
 * first read; every mutation updates the cache and persists the whole
 * document (one key, one apply). The map never appears in a log, a
 * diagnostics export, or a crash record — only the load-failed flag does.
 */
internal class ZoomMemory(private val prefs: SharedPreferences) {
    private var cached: ZoomStoreData? = null

    fun load(): ZoomStoreData {
        val c = cached
        if (c != null) return c
        val data = decodeZoomStore(prefs.getString(ZOOM_MEMORY_KEY, null))
        cached = data
        return data
    }

    private fun persist(data: ZoomStoreData) {
        cached = data
        prefs.edit().putString(ZOOM_MEMORY_KEY, encodeZoomStore(data)).apply()
    }

    fun entryFor(deviceId: String): ZoomEntry? = load().entries[deviceId]

    fun globalDefaultZoom(): Float = load().globalDefaultZoom

    fun contrastOn(): Boolean = load().contrastOn

    fun setGlobalDefault(zoom: Float) {
        val cur = load()
        persist(cur.copy(globalDefaultZoom = snapZoom(zoom)))
    }

    fun setContrastOn(on: Boolean) {
        val cur = load()
        persist(cur.copy(contrastOn = on))
    }

    /** Save one PC's entry (spec §8.4). Prunes to the 10-entry LRU cap, never dropping a currently-paired PC. */
    fun saveEntry(deviceId: String, entry: ZoomEntry, protectedIds: Set<String>) {
        val cur = load()
        val next = cur.entries.toMutableMap()
        next[deviceId] = entry
        val pruned = pruneZoomEntries(next, ZOOM_MEMORY_MAX_ENTRIES, protectedIds)
        persist(cur.copy(entries = pruned))
    }

    /** Delete one PC's entry — on unpair (spec §8.5) or on "Reset zoom for this PC" (spec §10). */
    fun delete(deviceId: String) {
        val cur = load()
        if (deviceId !in cur.entries) return
        val next = cur.entries.toMutableMap()
        next.remove(deviceId)
        persist(cur.copy(entries = next))
    }

    companion object {
        fun open(context: Context): ZoomMemory =
            ZoomMemory(context.getSharedPreferences(ZOOM_MEMORY_PREFS, Context.MODE_PRIVATE))
    }
}
