package com.deskhand.android

import org.junit.Assert.assertEquals
import org.junit.Test
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class EmbeddedTailscaleSocketTest {
    @Test fun `forward socket targets loopback and sends token`() {
        ServerSocket(0).use { listener ->
            val pool = Executors.newSingleThreadExecutor()
            try {
                val received = pool.submit<String> {
                    listener.accept().use { socket ->
                        val bytes = ByteArray(4)
                        java.io.DataInputStream(socket.getInputStream()).readFully(bytes)
                        socket.getOutputStream().write(42)
                        String(bytes, Charsets.US_ASCII)
                    }
                }
                val factory = EmbeddedTailscale.ForwardSocketFactory("100.64.1.2", 47801, listener.localPort, "test")
                factory.createSocket().use { socket ->
                    socket.connect(InetSocketAddress("100.64.1.2", 47801), 3000)
                    assertEquals(42, socket.getInputStream().read())
                }
                assertEquals("test", received.get(3, TimeUnit.SECONDS))
            } finally { pool.shutdownNow() }
        }
    }
}
