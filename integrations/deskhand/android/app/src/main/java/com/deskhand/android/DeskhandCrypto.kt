package com.deskhand.android

import java.math.BigInteger
import java.nio.ByteBuffer
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.ECPoint
import java.security.spec.ECPublicKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** Wire-compatible port of Deskhand.Core (Serial, Kdf, Handshake, SecureSession). */
object Serial {
    private const val ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    const val LENGTH = 20

    fun normalize(input: String?): String? {
        if (input == null) return null
        val sb = StringBuilder()
        for (c in input.uppercase()) {
            if (c == '-' || c == ' ') continue
            val m = when (c) {
                'I', 'L' -> '1'
                'O' -> '0'
                else -> c
            }
            if (ALPHABET.indexOf(m) < 0) return null
            sb.append(m)
        }
        return if (sb.length == LENGTH) sb.toString() else null
    }

    fun toSecret(normalized: String): ByteArray = normalized.toByteArray(Charsets.UTF_8)
}

object Kdf {
    private val salt = "deskhand-v1".toByteArray(Charsets.UTF_8)

    fun hmac(key: ByteArray, data: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(if (key.isEmpty()) ByteArray(32) else key, "HmacSHA256"))
        return mac.doFinal(data)
    }

    /** HKDF-SHA256 (RFC 5869). */
    fun hkdf(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
        val prk = hmac(if (salt.isEmpty()) ByteArray(32) else salt, ikm)
        val out = ByteArray(length)
        var previous = ByteArray(0)
        var pos = 0
        var counter = 1
        while (pos < length) {
            previous = hmac(prk, previous + info + byteArrayOf(counter.toByte()))
            val n = minOf(previous.size, length - pos)
            System.arraycopy(previous, 0, out, pos, n)
            pos += n
            counter++
        }
        return out
    }

    fun derive(ikm: ByteArray, info: String, length: Int): ByteArray =
        hkdf(ikm, salt, info.toByteArray(Charsets.UTF_8), length)

    fun roomId(secret: ByteArray): String = derive(secret, "room", 16).toHex()

    fun authKey(secret: ByteArray): ByteArray = derive(secret, "auth", 32)
}

fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it.toInt() and 0xff) }

enum class Role(val id: Int) { Host(1), Client(2) }

object Msg {
    const val VIDEO_FRAME = 21
    const val VIDEO_ACK = 22
    const val VIDEO_FAILED = 23
    const val UPDATE_CHECK = 40
    const val UPDATE_INFO = 41
    const val UPDATE_GET = 42
    const val UPDATE_CHUNK = 43
    const val UPDATE_ERROR = 44

    const val INFO = 1
    const val FRAME = 2
    const val ACK = 3
    const val INPUT = 4
    const val TEXT_FOCUS = 20
    const val PAIR_REQUEST = 5
    const val PAIR_ACCEPT = 6
    const val PAIR_REJECT = 7
    const val START = 8
    const val BYE = 9
    const val DESKTOP_LIST = 10
    const val DESKTOP_LIST_RESULT = 11
    const val DESKTOP_OPEN = 12
    const val DESKTOP_OPEN_RESULT = 13
    const val CHAT_REQUEST = 14
    const val CHAT_RESULT = 15
    const val DESKTOP_BACK = 16
    const val DESKTOP_BACK_RESULT = 17
    const val FOREGROUND_WINDOW = 18
    const val FOREGROUND_WINDOW_RESULT = 19
    // 100.017 (C13 push, spec §3.4): STREAM = 26, the first free value
    // on both sides (C# Msg.Stream = 26). The sealed stream frames
    // (stream.hello / subscribe / events / ack / resynced) travel as Msg.STREAM.
    const val STREAM = 26
}

private const val HELLO_MARKER = 0x01
private const val RECORD_MARKER = 0x02

class Handshake(private val role: Role, secret: ByteArray) {
    private val authKey = Kdf.authKey(secret)
    private val keyPair: KeyPair
    private val ecParams: ECParameterSpec
    private val helloBody = ByteArray(1 + 1 + 16 + 65)

    init {
        val gen = KeyPairGenerator.getInstance("EC")
        gen.initialize(ECGenParameterSpec("secp256r1"))
        keyPair = gen.generateKeyPair()
        val pub = keyPair.public as ECPublicKey
        ecParams = pub.params
        helloBody[0] = HELLO_MARKER.toByte()
        helloBody[1] = role.id.toByte()
        val nonce = ByteArray(16).also { SecureRandom().nextBytes(it) }
        nonce.copyInto(helloBody, 2)
        helloBody[18] = 0x04
        unsigned32(pub.w.affineX).copyInto(helloBody, 19)
        unsigned32(pub.w.affineY).copyInto(helloBody, 19 + 32)
    }

    fun createHello(): ByteArray = helloBody + Kdf.hmac(authKey, helloBody)

    /** Returns null for anything invalid, exactly like the C# side. */
    fun complete(peerHello: ByteArray): SecureSession? {
        if (peerHello.size != HELLO_LENGTH || peerHello[0].toInt() != HELLO_MARKER) return null
        val peerRole = peerHello[1].toInt()
        if (peerRole == role.id || (peerRole != Role.Host.id && peerRole != Role.Client.id)) return null
        val body = peerHello.copyOfRange(0, HELLO_LENGTH - 32)
        val mac = Kdf.hmac(authKey, body)
        if (!MessageDigest.isEqual(mac, peerHello.copyOfRange(HELLO_LENGTH - 32, HELLO_LENGTH))) return null
        if (peerHello[18].toInt() != 0x04) return null

        val shared: ByteArray
        try {
            val x = BigInteger(1, peerHello.copyOfRange(19, 51))
            val y = BigInteger(1, peerHello.copyOfRange(51, 83))
            val peerKey = KeyFactory.getInstance("EC").generatePublic(ECPublicKeySpec(ECPoint(x, y), ecParams))
            val ka = KeyAgreement.getInstance("ECDH")
            ka.init(keyPair.private)
            ka.doPhase(peerKey, true)
            shared = ka.generateSecret()
        } catch (e: Exception) {
            return null
        }

        val hostBody = if (role == Role.Host) helloBody else body
        val clientBody = if (role == Role.Host) body else helloBody
        val transcript = hostBody.copyOfRange(1, hostBody.size) + clientBody.copyOfRange(1, clientBody.size)
        val salt = MessageDigest.getInstance("SHA-256").digest(authKey + transcript)
        val okm = Kdf.hkdf(shared, salt, "deskhand-v1 session".toByteArray(Charsets.UTF_8), 68)
        val hostToClient = okm.copyOfRange(0, 32)
        val clientToHost = okm.copyOfRange(32, 64)
        val code = (ByteBuffer.wrap(okm, 64, 4).int.toLong() and 0xffffffffL) % 1_000_000L
        val confirm = "%06d".format(code)
        return if (role == Role.Host) SecureSession(hostToClient, clientToHost, confirm, role)
        else SecureSession(clientToHost, hostToClient, confirm, role)
    }

    private fun unsigned32(v: BigInteger): ByteArray {
        val raw = v.toByteArray()
        val out = ByteArray(32)
        val n = minOf(raw.size, 32)
        System.arraycopy(raw, raw.size - n, out, 32 - n, n)
        return out
    }

    companion object {
        const val HELLO_LENGTH = 1 + 1 + 16 + 65 + 32
    }
}

class SecureSession internal constructor(
    sendKey: ByteArray,
    receiveKey: ByteArray,
    val confirmCode: String,
    private val role: Role,
) {
    private val sendSpec = SecretKeySpec(sendKey, "AES")
    private val receiveSpec = SecretKeySpec(receiveKey, "AES")
    private var sendCounter = 0L
    private var lastReceived = -1L

    /** Callers must serialize seal+send so counters hit the wire in order. */
    @Synchronized
    fun seal(type: Int, payload: ByteArray): ByteArray {
        val counter = sendCounter++
        val plain = ByteArray(1 + payload.size)
        plain[0] = type.toByte()
        payload.copyInto(plain, 1)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, sendSpec, GCMParameterSpec(128, nonce(counter)))
        cipher.updateAAD(aad(role))
        val sealed = cipher.doFinal(plain)
        val record = ByteArray(9 + sealed.size)
        record[0] = RECORD_MARKER.toByte()
        ByteBuffer.wrap(record, 1, 8).putLong(counter)
        sealed.copyInto(record, 9)
        return record
    }

    /** Returns null if malformed, tampered with, or replayed. */
    @Synchronized
    fun open(record: ByteArray): Pair<Int, ByteArray>? {
        if (record.size < 1 + 8 + 1 + 16 || record[0].toInt() != RECORD_MARKER) return null
        val counter = ByteBuffer.wrap(record, 1, 8).long
        if (counter <= lastReceived) return null
        val plain = try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, receiveSpec, GCMParameterSpec(128, nonce(counter)))
            cipher.updateAAD(aad(if (role == Role.Host) Role.Client else Role.Host))
            cipher.doFinal(record, 9, record.size - 9)
        } catch (e: Exception) {
            return null
        }
        lastReceived = counter
        return plain[0].toInt() to plain.copyOfRange(1, plain.size)
    }

    private fun nonce(counter: Long): ByteArray {
        val n = ByteArray(12)
        ByteBuffer.wrap(n, 4, 8).putLong(counter)
        return n
    }

    private fun aad(sender: Role) = byteArrayOf(RECORD_MARKER.toByte(), sender.id.toByte())
}
