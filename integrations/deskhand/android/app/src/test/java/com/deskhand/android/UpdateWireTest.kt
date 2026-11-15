package com.deskhand.android

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.ByteBuffer
import java.security.MessageDigest

class UpdateWireTest {
    private val bytes = ByteArray(150000) { (it % 251).toByte() }
    private val hash = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    private fun release() = UpdateRelease("com.deskhand.android", 3, "0.1.3", bytes.size.toLong(), 28, hash)
    private fun chunk(offset: Int, data: ByteArray = bytes.copyOfRange(offset, minOf(offset + 65536, bytes.size))): ByteArray =
        hash.toByteArray(Charsets.US_ASCII) + ByteBuffer.allocate(8).putLong(offset.toLong()).array() + data
    @Test fun reconstructsMultipleChunksAndChecksChecksum() {
        val file = File.createTempFile("deskhand", ".apk")
        try {
            UpdateDownload(release(), file).use { d ->
                assertFalse(d.accept(chunk(0))); assertFalse(d.accept(chunk(65536))); assertTrue(d.accept(chunk(131072)))
                assertEquals(bytes.size.toLong(), d.offset)
            }
            assertArrayEquals(bytes, file.readBytes())
        } finally { file.delete() }
    }
    @Test fun rejectsWrongOffsetTruncationAndCorruption() {
        val file = File.createTempFile("deskhand", ".apk")
        try {
            UpdateDownload(release(), file).use { d ->
                assertThrows(IllegalArgumentException::class.java) { d.accept(chunk(65536)) }
                assertThrows(IllegalArgumentException::class.java) { d.accept(chunk(0).copyOf(75)) }
                val corrupted = chunk(0); corrupted[72] = 99
                d.accept(corrupted); d.accept(chunk(65536))
                assertThrows(IllegalArgumentException::class.java) { d.accept(chunk(131072)) }
            }
        } finally { file.delete() }
    }
    @Test fun rejectsForeignSnapshotDuplicatesAndOversizedChunks() {
        val file = File.createTempFile("deskhand", ".apk")
        try {
            UpdateDownload(release(), file).use { d ->
                val foreign = chunk(0); foreign[0] = if (foreign[0] == 97.toByte()) 98 else 97
                assertThrows(IllegalArgumentException::class.java) { d.accept(foreign) }
                assertThrows(IllegalArgumentException::class.java) { d.accept(chunk(0) + byteArrayOf(1)) }
                d.accept(chunk(0))
                assertThrows(IllegalArgumentException::class.java) { d.accept(chunk(0)) }
            }
            // Cancellation closes/removes the partial; retry must start at zero.
            assertTrue(file.delete())
            UpdateDownload(release(), file).use { retry ->
                assertEquals(0L, retry.offset)
                retry.accept(chunk(0)); retry.accept(chunk(65536)); assertTrue(retry.accept(chunk(131072)))
            }
            assertArrayEquals(bytes, file.readBytes())
        } finally { file.delete() }
    }

    @Test fun validatesManifest() {
        assertEquals(release(), UpdateRelease.parse(JSONObject(release().json())))
        for ((key, value) in listOf("packageId" to "evil.app", "sha256" to "../outside", "size" to -1, "versionCode" to 0)) {
            assertThrows(IllegalArgumentException::class.java) { UpdateRelease.parse(JSONObject(release().json()).put(key, value)) }
        }
        assertEquals(40, Msg.UPDATE_CHECK); assertEquals(41, Msg.UPDATE_INFO)
        assertEquals(42, Msg.UPDATE_GET); assertEquals(43, Msg.UPDATE_CHUNK); assertEquals(44, Msg.UPDATE_ERROR)
    }
}
