package com.deskhand.android

import android.content.Context
import android.net.ConnectivityManager
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONArray
import org.json.JSONObject
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.Socket
import java.net.SocketAddress
import java.io.IOException
import java.util.concurrent.Executors
import javax.net.SocketFactory
import tsbridge.Tsbridge

/** Process-scoped embedded node. The app's encrypted Deskhand session is unchanged. */
internal object EmbeddedTailscale {
    private val worker = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val forwards = mutableMapOf<String, SocketFactory>()
    private var started = false
    private var polling = false
    @Volatile private var currentState = "Stopped"
    @Volatile private var loginUrl = ""
    var status by mutableStateOf("Off")
        private set
    var signInAvailable by mutableStateOf(false)
        private set
    @Volatile private var requested = false
    // B2-G B4: bounded worker restarts (a transient failure used to latch
    // "Unavailable" forever). Attempts reset once the node reports Running
    // and when start() is called again by the user.
    private var startAttempts = 0
    private const val MAX_START_ATTEMPTS = 5
    private val START_RETRY_DELAYS_MS = longArrayOf(2_000L, 4_000L, 8_000L, 16_000L, 30_000L)

    fun authUrl(): String? = loginUrl.takeIf { it.startsWith("https://login.tailscale.com/") }

    @Synchronized fun start(context: Context) {
        requested = true
        if (started) return
        started = true
        startAttempts = 0
        // B2-G B3: a node restart recreates forwards with new ports/tokens;
        // drop any cached forward so the next connection re-resolves it.
        forwards.clear()
        val app = context.applicationContext
        worker.execute {
            try {
                val manager = app.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
                Tsbridge.setInterfaces {
                    manager.activeNetwork?.let { manager.getLinkProperties(it)?.interfaceName }?.let { Tsbridge.setDefaultInterface(it) }
                    val entries = JSONArray()
                    for (network in manager.allNetworks) {
                        val link = manager.getLinkProperties(network) ?: continue
                        val iface = link.interfaceName?.let { NetworkInterface.getByName(it) } ?: continue
                        val prefixes = JSONArray()
                        for (address in link.linkAddresses) {
                            // B2-G low (B10): a link address with a null host
                            // address threw NPE inside this callback.
                            val hostAddress = address.address.hostAddress ?: continue
                            prefixes.put(hostAddress.substringBefore('%') + "/" + address.prefixLength)
                        }
                        entries.put(JSONObject().put("Name", iface.name).put("Index", iface.index).put("MTU", iface.mtu).put("Prefixes", prefixes))
                    }
                    entries.toString()
                }
                Tsbridge.start(app.filesDir.resolve("embedded-tailscale").absolutePath)
                polling = true
                while (polling) {
                    val state = JSONObject(Tsbridge.statusJSON())
                    currentState = state.optString("state", "Starting")
                    if (currentState == "Running") startAttempts = 0
                    loginUrl = state.optString("authUrl", "")
                    val label = currentState
                    val canSignIn = authUrl() != null
                    main.post { status = label; signInAvailable = canSignIn }
                    Thread.sleep(2000)
                }
            } catch (e: Exception) {
                // B2-G B4: retry the worker on a transient failure instead of
                // latching "Unavailable" forever. Bounded: after
                // MAX_START_ATTEMPTS the status does latch "Unavailable"
                // until the user starts the node again (or the app restarts
                // with the preference set). The opt-in default is unchanged
                // (product decision — see MainActivity.kt:81).
                currentState = "Unavailable"
                startAttempts++
                if (startAttempts <= MAX_START_ATTEMPTS) {
                    val backoff = START_RETRY_DELAYS_MS[(startAttempts - 1).coerceAtMost(START_RETRY_DELAYS_MS.lastIndex)]
                    main.post { status = "Retrying…" }
                    runCatching { Thread.sleep(backoff) }
                    synchronized(this) { started = false }
                    start(app)
                } else {
                    main.post { status = "Unavailable" }
                    synchronized(this) { started = false }
                }
            }
        }
    }

    /**
     * B2-G B3: stop the embedded node (or as much of it as this layer owns).
     * Stops the status poll, clears every cached forward, and allows a later
     * start() to re-register. Residual: the snapshot has no Tsbridge stop
     * API (tsbridge.aar is absent), so the native node itself is not torn
     * down here — the poll and the forwards are. No caller uses stop() yet;
     * it exists so the node can be reset without killing the process.
     */
    @Synchronized fun stop() {
        requested = false
        polling = false
        started = false
        startAttempts = 0
        currentState = "Stopped"
        loginUrl = ""
        forwards.clear()
        main.post { status = "Off"; signInAvailable = false }
    }

    /** B2-G B3: drop one cached forward (e.g. after its socket fails, so the
     * next connection re-resolves a fresh port/token instead of retrying a
     * stale one forever). */
    @Synchronized internal fun invalidateForward(host: String, port: Int) {
        forwards.remove("$host:$port")
    }

    /** Select a private route even while the node is starting; retry after it is ready. */
    fun socketFactory(host: String, port: Int): SocketFactory? {
        val octets = host.split('.').mapNotNull { it.toIntOrNull() }
        if (!requested || octets.size != 4 || octets.any { it !in 0..255 } || octets[0] != 100 || octets[1] !in 64..127) return null
        return DeferredSocketFactory(host, port)
    }

    @Synchronized private fun readyFactory(host: String, port: Int): SocketFactory {
        if (currentState != "Running") throw IOException("Embedded Tailscale is not connected")
        val key = "$host:$port"
        return forwards.getOrPut(key) {
            val data = JSONObject(Tsbridge.forwardJSON(host, port.toLong()))
            ForwardSocketFactory(host, port, data.getInt("port"), data.getString("token"))
        }
    }

    private class DeferredSocketFactory(private val host: String, private val port: Int) : SocketFactory() {
        override fun createSocket(): Socket = DeferredSocket(host, port)
        override fun createSocket(host: String, port: Int): Socket = createSocket().apply { connect(InetSocketAddress(host, port), 10_000) }
        override fun createSocket(host: InetAddress, port: Int): Socket = createSocket(host.hostAddress ?: "", port)
        override fun createSocket(host: String, port: Int, local: InetAddress, localPort: Int): Socket = createSocket(host, port)
        override fun createSocket(host: InetAddress, port: Int, local: InetAddress, localPort: Int): Socket = createSocket(host, port)
    }

    private class DeferredSocket(private val host: String, private val port: Int) : Socket() {
        private var delegate: Socket? = null
        override fun connect(endpoint: SocketAddress, timeout: Int) {
            val destination = endpoint as? InetSocketAddress ?: throw IOException("Unexpected relay destination")
            if (destination.hostString != host || destination.port != port) throw IOException("Unexpected relay destination")
            val socket = readyFactory(host, port).createSocket()
            try { socket.connect(endpoint, timeout); delegate = socket } catch (e: Exception) { EmbeddedTailscale.invalidateForward(host, port); socket.close(); throw e }
        }
        override fun getInputStream() = delegate?.getInputStream() ?: throw IOException("Socket not connected")
        override fun getOutputStream() = delegate?.getOutputStream() ?: throw IOException("Socket not connected")
        override fun close() { delegate?.close(); super.close() }
        override fun setSoTimeout(timeout: Int) { delegate?.soTimeout = timeout }
        override fun getSoTimeout(): Int = delegate?.soTimeout ?: 0
        override fun isConnected(): Boolean = delegate?.isConnected == true
        override fun isClosed(): Boolean = delegate?.isClosed ?: super.isClosed()
    }

    internal class ForwardSocketFactory(private val host: String, private val port: Int, private val localPort: Int, private val token: String) : SocketFactory() {
        private inner class ForwardSocket : Socket() {
            override fun connect(endpoint: SocketAddress, timeout: Int) {
                val destination = endpoint as? InetSocketAddress ?: throw IOException("Unexpected relay destination")
                if (destination.hostString != this@ForwardSocketFactory.host || destination.port != this@ForwardSocketFactory.port)
                    throw IOException("Unexpected relay destination")
                super.connect(InetSocketAddress("127.0.0.1", this@ForwardSocketFactory.localPort), timeout)
                getOutputStream().write(this@ForwardSocketFactory.token.toByteArray(Charsets.US_ASCII))
            }
        }
        private fun open(name: String, requestedPort: Int): Socket {
            require(name == host && requestedPort == port) { "Unexpected relay destination" }
            return ForwardSocket().apply { connect(InetSocketAddress(host, port), 10_000) }
        }
        override fun createSocket(host: String, port: Int): Socket = open(host, port)
        override fun createSocket(host: InetAddress, port: Int): Socket = open(host.hostAddress ?: "", port)
        override fun createSocket(host: String, port: Int, local: InetAddress, localPort: Int): Socket = open(host, port)
        override fun createSocket(host: InetAddress, port: Int, local: InetAddress, localPort: Int): Socket = open(host.hostAddress ?: "", port)
        override fun createSocket(): Socket = ForwardSocket()
    }
}
