package com.deskhand.android

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.security.MessageDigest

data class UpdateRelease(val packageId: String, val versionCode: Long, val versionName: String,
    val size: Long, val minSdk: Int, val sha256: String) {
    fun json(): String = JSONObject().put("packageId", packageId).put("versionCode", versionCode)
        .put("versionName", versionName).put("size", size).put("minSdk", minSdk).put("sha256", sha256).toString()
    companion object {
        fun parse(o: JSONObject): UpdateRelease {
            val r = UpdateRelease(o.getString("packageId"), o.getLong("versionCode"), o.getString("versionName"),
                o.getLong("size"), o.getInt("minSdk"), o.getString("sha256"))
            require(r.packageId == "com.deskhand.android" && r.versionCode in 1..Int.MAX_VALUE.toLong() &&
                r.versionName.isNotBlank() && r.versionName.length <= 80 && r.size in 1..536870912 &&
                r.minSdk >= 28 && Regex("[a-f0-9]{64}").matches(r.sha256)) { "Invalid update information." }
            return r
        }
    }
}

/** Disk-backed transfer. Chunks must exactly match the single requested offset. */
class UpdateDownload(val release: UpdateRelease, val file: File) : AutoCloseable {
    private val digest = MessageDigest.getInstance("SHA-256")
    var offset = 0L; private set
    // B2-G B8: resume from an existing partial instead of always
    // truncating. A partial is only trusted when its length is a whole
    // number of chunks below the release size; anything else (oversize,
    // mid-chunk, or a complete-but-unrenamed file) is discarded and the
    // download restarts from zero. The digest is rebuilt from the kept
    // bytes so the final checksum still covers the whole file.
    private val out: FileOutputStream = run {
        val existing = if (file.exists()) file.length() else 0L
        if (existing > 0 && existing < release.size && existing % 65536L == 0L) {
            file.inputStream().use { input ->
                val b = ByteArray(65536)
                while (true) {
                    val n = input.read(b)
                    if (n < 0) break
                    digest.update(b, 0, n)
                }
            }
            offset = existing
            FileOutputStream(file, true)
        } else {
            if (existing > 0) file.delete()
            FileOutputStream(file)
        }
    }
    fun accept(bytes: ByteArray): Boolean {
        require(bytes.size >= 73 && bytes.size <= 72 + 65536) { "Invalid update chunk." }
        val hash = String(bytes, 0, 64, Charsets.US_ASCII)
        val at = ByteBuffer.wrap(bytes, 64, 8).long
        val count = bytes.size - 72
        require(hash == release.sha256 && at == offset && count.toLong() == minOf(65536L, release.size - offset)) { "Invalid update chunk." }
        out.write(bytes, 72, count); digest.update(bytes, 72, count); offset += count
        if (offset != release.size) return false
        out.fd.sync(); out.close()
        require(digest.digest().joinToString("") { "%02x".format(it) } == release.sha256) { "Update checksum mismatch. Retry the download." }
        return true
    }
    override fun close() { out.close() }
}
