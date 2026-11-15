package com.deskhand.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 100.011 VM unit tests — per-PC zoom memory codec, prune, and restore
 * (spec §8, §12; P-T1–P-T13 supplement). Pure functions only; the
 * SharedPreferences wrapper is covered by ZoomMemoryPrefsTest.
 */
class ZoomMemoryTest {

    private fun entry(zoom: Float, panX: Float = 800f, panY: Float = 450f, fw: Int = 1600, fh: Int = 900, at: Long = 1000L) =
        ZoomEntry(zoom, panX, panY, fw, fh, at)

    @Test
    fun codec_roundTripPreservesEntries() {
        val data = ZoomStoreData(
            globalDefaultZoom = 1.75f,
            contrastOn = false,
            entries = mapOf("abc123" to entry(1.75f, 640f, 380f, 1600, 900, 123456789L)),
        )
        val decoded = decodeZoomStore(encodeZoomStore(data))
        assertEquals(1.75f, decoded.globalDefaultZoom, 0.001f)
        assertEquals(false, decoded.contrastOn)
        assertEquals(1, decoded.entries.size)
        val e = decoded.entries.getValue("abc123")
        assertEquals(1.75f, e.zoom, 0.001f)
        assertEquals(640f, e.panX, 0.001f)
        assertEquals(380f, e.panY, 0.001f)
        assertEquals(1600, e.frameWidth)
        assertEquals(900, e.frameHeight)
        assertEquals(123456789L, e.updatedAt)
        assertEquals(false, decoded.loadFailed)
    }

    @Test
    fun codec_emptyOrNullIsDefaults() {
        val d1 = decodeZoomStore(null)
        assertEquals(ZOOM_DEFAULT, d1.globalDefaultZoom, 0.001f)
        assertEquals(true, d1.contrastOn)
        assertTrue(d1.entries.isEmpty())
        assertEquals(false, d1.loadFailed)
        val d2 = decodeZoomStore("")
        assertEquals(ZOOM_DEFAULT, d2.globalDefaultZoom, 0.001f)
        assertTrue(d2.entries.isEmpty())
    }

    @Test
    fun codec_corruptDocumentDiscardsMapAndFlagsFailure() {
        val d = decodeZoomStore("{not json at all")
        assertEquals(true, d.loadFailed)
        assertTrue(d.entries.isEmpty())
        assertEquals(ZOOM_DEFAULT, d.globalDefaultZoom, 0.001f)
        assertEquals(true, d.contrastOn)
    }

    @Test
    fun codec_oneBadEntryIsSkippedNotFatal() {
        val json = """{"version":1,"globalDefaultZoom":1.5,"contrastOn":true,"entries":{""" +
            """"good":{"zoom":1.75,"panX":640,"panY":380,"frameWidth":1600,"frameHeight":900,"updatedAt":5},""" +
            """"bad":"just a string, not an object",""" +
            """"weird":{"zoom":"nope","panX":640,"panY":380,"frameWidth":1600,"frameHeight":900,"updatedAt":6}}}"""
        val d = decodeZoomStore(json)
        assertEquals(false, d.loadFailed)
        // The non-object entry is skipped; the non-numeric zoom degrades to
        // the default instead of dropping the entry.
        assertEquals(2, d.entries.size)
        assertTrue("good" in d.entries)
        assertTrue("bad" !in d.entries)
        assertTrue("weird" in d.entries)
        assertEquals(1.75f, d.entries.getValue("good").zoom, 0.001f)
        assertEquals(ZOOM_DEFAULT, d.entries.getValue("weird").zoom, 0.001f)
    }

    @Test
    fun codec_globalDefaultIsSnappedOnDecode() {
        val d = decodeZoomStore("""{"version":1,"globalDefaultZoom":1.63,"contrastOn":true,"entries":{}}""")
        assertEquals(1.65f, d.globalDefaultZoom, 0.001f)
        val d2 = decodeZoomStore("""{"version":1,"globalDefaultZoom":0.5,"contrastOn":true,"entries":{}}""")
        assertEquals(ZOOM_MIN, d2.globalDefaultZoom, 0.001f)
    }

    @Test
    fun prune_dropsLeastRecentlyUsedOverCap() {
        val entries = (1..11).associate { "pc$it" to entry(1.5f, at = it.toLong()) }
        val pruned = pruneZoomEntries(entries, 10, emptySet())
        assertEquals(10, pruned.size)
        assertTrue("pc1" !in pruned) // oldest (updatedAt 1) is dropped
        assertTrue("pc11" in pruned)
    }

    @Test
    fun prune_neverDropsACurrentlyPairedPc() {
        val entries = (1..11).associate { "pc$it" to entry(1.5f, at = it.toLong()) }
        val pruned = pruneZoomEntries(entries, 10, setOf("pc1"))
        assertEquals(10, pruned.size)
        assertTrue("pc1" in pruned) // protected even though oldest
        assertTrue("pc2" !in pruned) // next oldest is dropped instead
    }

    @Test
    fun prune_underCapIsUntouched() {
        val entries = mapOf("a" to entry(1.5f), "b" to entry(1.75f))
        assertEquals(entries, pruneZoomEntries(entries, 10, emptySet()))
    }

    @Test
    fun restore_noEntryIsGlobalDefaultCentred() {
        val r = restoreZoomEntry(null, 1.5f, 1600, 900)
        assertEquals(1.50f, r.zoom, 0.001f)
        assertNull(r.panCentre)
    }

    @Test
    fun restore_entryZoomIsClamped() {
        assertEquals(ZOOM_MIN, restoreZoomEntry(entry(1.0f), 1.5f, 1600, 900).zoom, 0.001f)
        assertEquals(ZOOM_MAX, restoreZoomEntry(entry(3.0f), 1.5f, 1600, 900).zoom, 0.001f)
        assertEquals(1.65f, restoreZoomEntry(entry(1.63f), 1.5f, 1600, 900).zoom, 0.001f)
    }

    @Test
    fun restore_panCentreScalesToLiveFrame() {
        // Saved on an 800x450 frame, centre at (400, 225) → live 1600x900 centre (800, 450).
        val r = restoreZoomEntry(entry(1.75f, 400f, 225f, 800, 450), 1.5f, 1600, 900)
        assertEquals(1.75f, r.zoom, 0.001f)
        val c = r.panCentre!!
        assertEquals(800f, c.x, 0.01f)
        assertEquals(450f, c.y, 0.01f)
    }

    @Test
    fun restore_invalidEntryFallsBackToCentre() {
        // Saved frame unknown → centre (rule 3).
        assertNull(restoreZoomEntry(entry(1.75f, 400f, 225f, 0, 0), 1.5f, 1600, 900).panCentre)
        // NaN pan → centre.
        assertNull(restoreZoomEntry(entry(1.75f, Float.NaN, 225f, 800, 450), 1.5f, 1600, 900).panCentre)
        // No live frame yet → centre (the factor is still applied).
        val r = restoreZoomEntry(entry(1.75f, 400f, 225f, 800, 450), 1.5f, 0, 0)
        assertEquals(1.75f, r.zoom, 0.001f)
        assertNull(r.panCentre)
    }

    @Test
    fun restore_outOfBoundsCentreIsClampedIntoFrame() {
        val r = restoreZoomEntry(entry(1.75f, 5000f, 5000f, 1600, 900), 1.5f, 1600, 900)
        val c = r.panCentre!!
        assertEquals(1600f, c.x, 0.01f)
        assertEquals(900f, c.y, 0.01f)
    }
}
