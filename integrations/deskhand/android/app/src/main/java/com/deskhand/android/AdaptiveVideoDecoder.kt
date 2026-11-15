package com.deskhand.android

import android.graphics.Bitmap
import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/** Bounded hardware decoding; existing bitmap viewer retains zoom and exact touch mapping. */
internal class AdaptiveVideoDecoder(
    private val display: (Int, Bitmap) -> Unit,
    private val acknowledge: (ByteArray) -> Unit,
    private val failure: () -> Unit
) {
    private val worker = HandlerThread("deskhand-h264").apply { start() }
    private val handler = Handler(worker.looper)
    private val closed = AtomicBoolean(false)
    private val queued = AtomicInteger(0)
    @Volatile private var visibleEpoch = 0
    private var epoch = 0
    private var codec: MediaCodec? = null
    private var texture: VideoTexture? = null
    private var timestamp = 0L
    private var width = 0
    private var height = 0
    // B2-G B6: bounded decoder recreation. One bad packet used to kill
    // hardware video for the whole session (fail() closed the codec and
    // the Model never recreated it). Recoverable failures now release
    // the codec and let the next packet re-configure it, up to
    // MAX_DECODER_RETRIES consecutive failures; only then is the failure
    // terminal (close + failure callback, as before). All fail() calls run
    // on the decoder worker thread, so this counter needs no synchronisation.
    private var consecutiveFailures = 0
    private companion object {
        const val MAX_DECODER_RETRIES = 3
    }

    fun submit(packet: ByteArray) {
        if (closed.get()) return
        // B2-G B6: a malformed packet is dropped, not fatal — one bad
        // packet must not kill hardware video for the session.
        if (packet.size !in 17..(4 * 1024 * 1024)) { android.util.Log.w("DeskhandVideo", "Dropping video packet with invalid size ${packet.size}"); return }
        val header = ByteBuffer.wrap(packet)
        val incomingEpoch = header.int
        header.int // sequence is echoed, never interpreted as authority
        val w = header.int
        val h = header.int
        if (incomingEpoch <= 0 || w !in 2..1280 || h !in 2..1280 || w % 2 != 0 || h % 2 != 0) { android.util.Log.w("DeskhandVideo", "Dropping video packet with invalid header epoch=$incomingEpoch ${w}x$h"); return }
        visibleEpoch = incomingEpoch
        // Queue-full is ordinary backpressure (UI thread stalled), not a
        // decoder failure: drop this packet and acknowledge it so the
        // host's flow does not stall behind it.
        if (queued.incrementAndGet() > 8) { queued.decrementAndGet(); acknowledge(packet.copyOfRange(0, 8)); return }
        handler.post {
            try {
                if (closed.get()) return@post
                if (visibleEpoch != incomingEpoch) { acknowledge(packet.copyOfRange(0, 8)); return@post }
                if (epoch != incomingEpoch) configure(incomingEpoch, w, h)
                require(w == width && h == height)
                val decoder = codec ?: error("Decoder missing")
                val deadline = android.os.SystemClock.uptimeMillis() + 500
                var index = decoder.dequeueInputBuffer(0)
                while (index < 0 && android.os.SystemClock.uptimeMillis() < deadline) {
                    drain()
                    index = decoder.dequeueInputBuffer(1000)
                }
                check(index >= 0) { "Decoder stalled" }
                val input = decoder.getInputBuffer(index) ?: error("No input buffer")
                input.clear()
                require(packet.size - 16 <= input.remaining())
                input.put(packet, 16, packet.size - 16)
                timestamp += 33333
                decoder.queueInputBuffer(index, 0, packet.size - 16, timestamp, 0)
                drain()
                acknowledge(packet.copyOfRange(0, 8))
            } catch (error: Exception) { fail(error) }
            finally { queued.decrementAndGet() }
        }
    }

    private fun configure(newEpoch: Int, w: Int, h: Int) {
        release()
        epoch = newEpoch; width = w; height = h; timestamp = 0
        val target = VideoTexture(w, h, handler,
            frame = { bitmap -> if (!closed.get() && visibleEpoch == newEpoch) display(newEpoch, bitmap) else bitmap.recycle() },
            failure = { fail(it) })
        texture = target
        val decoder = MediaCodec.createDecoderByType("video/avc")
        codec = decoder
        val format = MediaFormat.createVideoFormat("video/avc", w, h)
        format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 4 * 1024 * 1024)
        decoder.configure(format, target.surface, null, 0)
        decoder.start()
        consecutiveFailures = 0
        handler.removeCallbacks(poll)
        handler.post(poll)
    }

    private fun drain() {
        val decoder = codec ?: return
        val info = MediaCodec.BufferInfo()
        while (true) {
            val index = decoder.dequeueOutputBuffer(info, 0)
            if (index >= 0) decoder.releaseOutputBuffer(index, true)
            else if (index != MediaCodec.INFO_OUTPUT_FORMAT_CHANGED && index != MediaCodec.INFO_OUTPUT_BUFFERS_CHANGED) return
        }
    }
    private val poll = object : Runnable {
        override fun run() {
            if (closed.get() || codec == null) return
            try { drain(); handler.postDelayed(this, 10) } catch (error: Exception) { fail(error) }
        }
    }
    fun showStill() {
        visibleEpoch = 0
        handler.post { handler.removeCallbacks(poll); release() }
    }
    fun isCurrent(value: Int) = !closed.get() && visibleEpoch == value
    private fun release() {
        runCatching { codec?.stop() }; runCatching { codec?.release() }; codec = null
        runCatching { texture?.close() }; texture = null
        epoch = 0
    }
    private fun fail(error: Exception? = null) {
        if (closed.get()) return
        if (consecutiveFailures < MAX_DECODER_RETRIES) {
            consecutiveFailures++
            android.util.Log.w("DeskhandVideo", "Hardware decoder failed; recreating (attempt $consecutiveFailures/$MAX_DECODER_RETRIES)", error)
            // Release on the worker thread; epoch resets to 0, so the next
            // packet re-configures the codec (including a host resize sent
            // without an epoch bump, which used to be terminal).
            handler.post { if (!closed.get()) release() }
            return
        }
        android.util.Log.w("DeskhandVideo", "Hardware decoder unavailable", error)
        close()
        failure()
    }
    fun close() {
        if (!closed.compareAndSet(false, true)) return
        visibleEpoch = 0
        handler.post { handler.removeCallbacks(poll); release(); worker.quitSafely() }
    }
}
