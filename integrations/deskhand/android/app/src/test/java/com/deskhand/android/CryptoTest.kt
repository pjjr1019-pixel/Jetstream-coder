package com.deskhand.android

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class CryptoTest {
    private val serial = "ABCDEFGHJKMNPQRSTVWX"

    @Test
    fun kdfMatchesCSharpVector() {
        val secret = Serial.toSecret(serial)
        assertEquals("7fa5db4d5e7b12098c13657574a6e875", Kdf.roomId(secret))
        assertEquals(
            "0e31d77d1784e3fe8939fed2da43152b3111b3d785b40fa664ef6570943a20f8",
            Kdf.authKey(secret).toHex(),
        )
    }

    @Test
    fun serialNormalization() {
        assertEquals(serial, Serial.normalize("abcde-fghjk-mnpqr-stvwx"))
        assertEquals("01110" + "ABCDE" + "FGHJK" + "MNPQR", Serial.normalize("OIL1O ABCDE FGHJK MNPQR"))
        assertNull(Serial.normalize("ABCDE"))
        assertNull(Serial.normalize("UUUUUUUUUUUUUUUUUUUU"))
    }

    @Test
    fun handshakeAndRecords() {
        val secret = Serial.toSecret(serial)
        val host = Handshake(Role.Host, secret)
        val client = Handshake(Role.Client, secret)
        val hh = host.createHello()
        val ch = client.createHello()
        assertEquals(Handshake.HELLO_LENGTH, hh.size)
        val hs = host.complete(ch)
        val cs = client.complete(hh)
        assertNotNull(hs)
        assertNotNull(cs)
        assertEquals(hs!!.confirmCode, cs!!.confirmCode)

        val record = cs.seal(Msg.INPUT, "hi".toByteArray())
        val opened = hs.open(record)
        assertEquals(Msg.INPUT, opened!!.first)
        assertArrayEquals("hi".toByteArray(), opened.second)
        assertNull(hs.open(record))

        val back = hs.seal(Msg.FRAME, ByteArray(5000) { it.toByte() })
        assertEquals(5000, cs.open(back)!!.second.size)
        assertNull(cs.open(hs.seal(Msg.ACK, byteArrayOf()).also { it[20] = (it[20].toInt() xor 1).toByte() }))
    }

    @Test
    fun wrongSecretRejected() {
        val host = Handshake(Role.Host, Serial.toSecret(serial))
        val other = Handshake(Role.Client, Serial.toSecret("0123456789ABCDEFGHJK"))
        assertNull(host.complete(other.createHello()))
    }
}
