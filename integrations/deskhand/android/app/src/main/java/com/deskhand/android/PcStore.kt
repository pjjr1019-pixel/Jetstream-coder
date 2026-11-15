package com.deskhand.android

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

// 100.015 (spec §11.3): the paired-PC record carries an ordered candidate
// list `relays` in the fixed order [Tailscale, tunnel/primary, LAN].
// `relay` is now a derived property — always relays.first() — so the two
// can never diverge. The 4-arg constructor stays for the old single-relay
// shape (migration + existing call sites); it builds a one-item list.
data class PairedPc(val name: String, val secret: ByteArray, val control: Boolean, val relays: List<String>) {
    init {
        require(relays.isNotEmpty() && relays.all { it.isNotBlank() }) { "PairedPc requires at least one non-blank relay" }
    }

    /** The primary relay: always relays.first(). */
    val relay: String get() = relays.firstOrNull() ?: ""

    val id: String get() = Kdf.roomId(secret)

    constructor(name: String, relay: String, secret: ByteArray, control: Boolean) : this(name, secret, control, listOf(relay))
}

/** Paired-PC list, encrypted at rest with a non-exportable Android Keystore key. */
class PcStore(context: Context) {
    private val file = File(context.filesDir, "pcs.enc")

    /**
     * B2-G B5: set when load() fails. The raw file is preserved (never
     * deleted or overwritten by the failed load) and the error is exposed
     * here for Model so the Home screen can say so, instead of the app
     * silently showing "No PC paired yet".
     */
    var loadError: String? = null
        private set
    var loadFailed: Boolean = false
        private set

    // 100.015 (spec §11.3): records dropped by load() because their relay
    // list was empty with no legacy "relay" to migrate from. The raw file
    // is preserved (same B5 rule as a failed load); Model surfaces the
    // count on Home as "pair again".
    var droppedCorruptCount: Int = 0
        private set

    fun load(): List<PairedPc> {
        loadFailed = false
        loadError = null
        droppedCorruptCount = 0
        if (!file.exists()) return emptyList()
        return try {
            val raw = file.readBytes()
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, raw.copyOfRange(0, 12)))
            val arr = JSONArray(String(cipher.doFinal(raw, 12, raw.size - 12), Charsets.UTF_8))
            // 100.015: per-record parse — one corrupt record is dropped
            // (counted in droppedCorruptCount) instead of failing the
            // whole load; a record with an empty list but a surviving
            // legacy "relay" migrates to a one-item list (see
            // parsePairedPcRecord). Never log a relay URL here.
            (0 until arr.length()).mapNotNull {
                val o = arr.getJSONObject(it)
                parsePairedPcRecord(o) ?: run {
                    droppedCorruptCount++
                    android.util.Log.w("Deskhand", "dropped corrupt paired-PC record (empty relay list, no legacy relay): ${o.optString("name", "?")}")
                    null
                }
            }
        } catch (e: Exception) {
            // B2-G B5: never silently discard the pairings, and never let
            // the next save() destroy the only copy of the raw file — the
            // file stays on disk (save() backs it up first) and the error
            // is exposed via loadError for the UI.
            loadFailed = true
            loadError = e.javaClass.simpleName + (e.message?.let { ": $it" } ?: "")
            emptyList()
        }
    }

    fun save(list: List<PairedPc>) {
        val arr = JSONArray()
        list.forEach { arr.put(pairedPcToJson(it)) }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val sealed = cipher.doFinal(arr.toString().toByteArray(Charsets.UTF_8))
        // B2-G B6: atomic replace — never delete the live file before the
        // replacement is durably in place. The previous copy is kept as
        // pcs.enc.bak (refreshed each save); a failed replace restores it,
        // so a failed save can no longer destroy the only good copy. The
        // backup is also what preserves a corrupt file after a failed load
        // (B5): pairing a new PC stays possible and the old bytes survive.
        val tmp = File(file.parentFile, "pcs.enc.tmp")
        java.io.FileOutputStream(tmp).use { it.write(cipher.iv + sealed); it.fd.sync() }
        val backup = File(file.parentFile, "pcs.enc.bak")
        if (file.exists()) runCatching { file.copyTo(backup, overwrite = true) }
        if (tmp.renameTo(file)) {
            loadFailed = false
            loadError = null
            return
        }
        // renameTo failed (commonly: the target already exists). Move the
        // live file aside first, then move tmp in; if that fails, restore
        // the live file from the backup instead of losing it.
        var movedAside = true
        if (file.exists()) {
            backup.delete()
            movedAside = file.renameTo(backup)
        }
        if (movedAside && tmp.renameTo(file)) {
            loadFailed = false
            loadError = null
            return
        }
        if (movedAside) runCatching { backup.renameTo(file) }
        tmp.delete()
        error("Couldn't save paired PCs.")
    }

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }

    private companion object {
        const val ALIAS = "deskhand_store_v1"
    }
}

/**
 * Serialise one paired PC (100.015 spec §11.3). "relays" is the ordered
 * candidate list; "relay" (the first) is also written so an older build
 * reading the same file still finds its single address.
 */
internal fun pairedPcToJson(pc: PairedPc): JSONObject =
    JSONObject().put("name", pc.name).put("relay", pc.relay)
        .put("relays", JSONArray(pc.relays))
        .put("secret", Base64.encodeToString(pc.secret, Base64.NO_WRAP)).put("control", pc.control)

/**
 * Parse one stored record, migrating the old single-"relay" shape
 * (100.015 spec §11.3). Returns null for a corrupt record: an empty
 * "relays" list with no legacy "relay" to migrate from — the caller
 * drops that record and surfaces "pair again". A record whose "relays"
 * is present but empty falls back to the legacy "relay" when it
 * survives, keeping the old single-relay behaviour for that PC.
 */
internal fun parsePairedPcRecord(o: JSONObject): PairedPc? {
    val name = o.optString("name", "My PC").ifBlank { "My PC" }
    val secret = runCatching { Base64.decode(o.getString("secret"), Base64.NO_WRAP) }.getOrNull() ?: return null
    val control = o.optBoolean("control")
    val stored = if (o.has("relays")) {
        val arr = o.optJSONArray("relays")
        if (arr != null) (0 until arr.length()).mapNotNull { arr.optString(it).trim().trimEnd('/').takeIf { s -> s.isNotBlank() } } else emptyList()
    } else emptyList()
    val legacy = o.optString("relay", "").trim()
    val relays = when {
        stored.isNotEmpty() -> stored
        legacy.isNotBlank() -> listOf(legacy)
        else -> return null
    }
    return PairedPc(name, secret, control, relays)
}
