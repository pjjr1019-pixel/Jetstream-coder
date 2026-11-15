package com.deskhand.android

import okhttp3.OkHttpClient
import okhttp3.ConnectionSpec
import okhttp3.TlsVersion
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.util.concurrent.TimeUnit

/**
 * End-to-end encrypted link to one PC through the relay. The relay only ever sees opaque bytes.
 * Reconnects automatically until [close] is called.
 */
open class PcLink(
    relay: String,
    private val secret: ByteArray,
    private val events: Events,
) {
    interface Events {
        fun onConnection(relayUp: Boolean)
        fun onSession(confirmCode: String)
        fun onSessionLost()
        fun onMessage(type: Int, payload: ByteArray)
    }

    private val url = "${relay.trimEnd('/')}/ws/${Kdf.roomId(secret)}?role=client"
    private val http = OkHttpClient.Builder()
        .dns(FallbackDns)
        .connectTimeout(10, TimeUnit.SECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .apply {
            val target = runCatching { java.net.URI(relay.trim()) }.getOrNull()
            // B2-G H10: route wss:// CGNAT relays through the embedded node
            // exactly like ws:// — a wss://100.64.x.y relay bypassed the
            // node entirely and was dialled over the public internet, where
            // the CGNAT address is unroutable. The default port follows the
            // scheme (443 for wss, 80 for ws), not 80 for both.
            if ((target?.scheme == "ws" || target?.scheme == "wss") && target.host != null) {
                val defaultPort = if (target.scheme == "wss") 443 else 80
                EmbeddedTailscale.socketFactory(target.host, if (target.port == -1) defaultPort else target.port)?.let { socketFactory(it) }
            }
        }.build()
    private val tls12Http by lazy {
        http.newBuilder().connectionSpecs(listOf(
            ConnectionSpec.Builder(ConnectionSpec.MODERN_TLS).tlsVersions(TlsVersion.TLS_1_2).build(),
            ConnectionSpec.CLEARTEXT,
        )).build()
    }
    private val lock = Any()
    private var socket: WebSocket? = null
    private var handshake: Handshake? = null
    private var session: SecureSession? = null
    private var closed = false
    private var retryMs = 1000L
    private var retryWithTls12 = false
    private val retryThread = java.util.concurrent.Executors.newSingleThreadScheduledExecutor()

    private val host = runCatching { java.net.URI(relay.trim()).host }.getOrNull() ?: relay

    /** Plain-language reason for the last relay failure, shown to the user. */
    @Volatile
    var lastError: String = ""
        private set

    private fun describe(t: Throwable, code: Int?): String = when {
        code == 404 -> "The relay is up but the PC isn't on it yet (is Deskhand running and not paused?)"
        code != null -> "The relay answered with HTTP $code"
        t is java.net.UnknownHostException ->
            "This phone can't look up $host. New tunnel addresses can take a minute; a VPN, ad blocker or Private DNS that blocks trycloudflare.com also causes this"
        t is javax.net.ssl.SSLException -> "Secure connection to $host failed (${t.message})"
        t is java.net.SocketTimeoutException -> "Timed out reaching $host"
        else -> "Couldn't connect to $host (${t.javaClass.simpleName}: ${t.message})"
    }

    open fun start() = connect()

    private fun connect() {
        synchronized(lock) {
            if (closed) return
            val request = try {
                Request.Builder().url(url.replaceFirst("ws://", "http://").replaceFirst("wss://", "https://")).build()
            } catch (e: IllegalArgumentException) {
                events.onConnection(false)
                return
            }
            socket = (if (retryWithTls12) tls12Http else http).newWebSocket(request, Listener())
        }
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (synchronized(lock) { webSocket !== socket || closed }) return
            retryMs = 1000L
            retryWithTls12 = false
            events.onConnection(true)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            when (text) {
                "peer-joined" -> synchronized(lock) {
                    if (webSocket !== socket) return
                    val hadSession = session != null
                    session = null
                    val hs = Handshake(Role.Client, secret)
                    handshake = hs
                    webSocket.send(hs.createHello().toByteString())
                    if (hadSession) events.onSessionLost()
                }
                "peer-left" -> synchronized(lock) {
                    if (webSocket !== socket) return
                    handshake = null
                    if (session != null) {
                        session = null
                        events.onSessionLost()
                    }
                }
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            val data = bytes.toByteArray()
            val current = synchronized(lock) {
                if (webSocket !== socket || closed) return
                val s = session
                if (s == null) {
                    val established = handshake?.complete(data)
                    if (established != null) {
                        handshake = null
                        session = established
                        events.onSession(established.confirmCode)
                    }
                }
                s
            } ?: return
            val opened = current.open(data)
            if (opened != null) synchronized(lock) {
                if (webSocket === socket && current === session && !closed)
                    events.onMessage(opened.first, opened.second)
            }
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = down(webSocket)

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (synchronized(lock) { webSocket !== socket || closed }) return
            android.util.Log.w("Deskhand", "relay socket failed: ${t.javaClass.simpleName} http=${response?.code}")
            // Keep diagnostics limited to exception types; URLs, pairing codes,
            // credentials and certificate contents must not be logged.
            if (t is javax.net.ssl.SSLException) {
                val causes = generateSequence<Throwable>(t) { it.cause }.take(8).joinToString(" -> ") { it.javaClass.simpleName }
                android.util.Log.w("Deskhand", "relay TLS cause types: $causes")
            }
            // Some relays or networks close a TLS 1.3 handshake without a TLS
            // alert. Retry using authenticated TLS 1.2, never custom trust or
            // hostname verification. Certificate failures are not downgraded.
            if (t is javax.net.ssl.SSLHandshakeException && generateSequence<Throwable>(t) { it.cause }.take(8).any { it is java.io.EOFException }) {
                retryWithTls12 = true
            }
            lastError = describe(t, response?.code)
            down(webSocket)
        }
    }

    private fun down(webSocket: WebSocket) {
        synchronized(lock) {
            if (webSocket !== socket) return
            socket = null
            handshake = null
            val hadSession = session != null
            session = null
            if (hadSession) events.onSessionLost()
            if (closed) return
            events.onConnection(false)
            // B2-G B11: jittered, bounded backoff. The old loop retried
            // forever at 1/2/4/5 s with no jitter, so many phones hammered
            // a dead relay in lock-step. The cap is now 30 s; onOpen still
            // resets retryMs to 1 s on a successful connect.
            val wait = retryMs + java.util.concurrent.ThreadLocalRandom.current().nextLong(0, retryMs / 2 + 1)
            retryMs = minOf(30_000L, retryMs * 2)
            retryThread.schedule({ connect() }, wait, TimeUnit.MILLISECONDS)
        }
    }

    /** Reconnect when the encrypted peer stops responding to application requests. */
    open fun reconnect() {
        synchronized(lock) { socket?.cancel() }
    }

    /** Returns false if there is no live encrypted session. */
    open fun send(type: Int, payload: ByteArray = ByteArray(0)): Boolean {
        synchronized(lock) {
            val s = session ?: return false
            val ws = socket ?: return false
            return ws.send(s.seal(type, payload).toByteString())
        }
    }

    open fun close() {
        synchronized(lock) {
            if (closed) return
            closed = true
            session?.let { runCatching { socket?.send(it.seal(Msg.BYE, ByteArray(0)).toByteString()) } }
            socket?.close(1000, null)
            socket = null
            session = null
        }
        retryThread.shutdownNow()
        http.dispatcher.executorService.shutdown()
    }
}
