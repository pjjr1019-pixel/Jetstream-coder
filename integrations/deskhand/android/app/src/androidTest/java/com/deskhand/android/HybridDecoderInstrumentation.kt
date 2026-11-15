package com.deskhand.android

import android.app.Activity
import android.app.Instrumentation
import android.os.Bundle
import java.io.DataInputStream
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicBoolean

/** Real-device hardware decoder test using only synthetic moving color patterns. */
class HybridDecoderInstrumentation : Instrumentation() {
    override fun onCreate(arguments: Bundle?) { super.onCreate(arguments); start() }
    override fun onStart() {
        val results = Bundle()
        try {
            val pictures = AtomicInteger()
            val acks = AtomicInteger()
            val failed = AtomicBoolean(false)
            val wrongSize = AtomicBoolean(false)
            val colors = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()
            var sent = 0
            val decoder = AdaptiveVideoDecoder(
                display = { epoch, bitmap ->
                    if ((epoch == 1280 && (bitmap.width != 1280 || bitmap.height != 720)) ||
                        (epoch == 720 && (bitmap.width != 720 || bitmap.height != 1280))) wrongSize.set(true)
                    colors.add(bitmap.getPixel(bitmap.width / 3, bitmap.height / 2))
                    pictures.incrementAndGet(); bitmap.recycle()
                },
                acknowledge = { acks.incrementAndGet() },
                failure = { failed.set(true) }
            )
            try {
                for (width in listOf(1280, 720)) {
                    val before = pictures.get()
                    DataInputStream(context.assets.open("video-$width.bin")).use { input ->
                        while (input.available() > 0) {
                            val length = input.readInt()
                            val packet = ByteArray(length); input.readFully(packet)
                            val deadline = android.os.SystemClock.uptimeMillis() + 3000
                            while (sent - acks.get() >= 8 && android.os.SystemClock.uptimeMillis() < deadline && !failed.get()) Thread.sleep(5)
                            check(sent - acks.get() < 8) { "Decoder acknowledgment timed out" }
                            decoder.submit(packet)
                            sent++
                            Thread.sleep(34)
                        }
                    }
                    Thread.sleep(500)
                    check(!failed.get()) { "Decoder reported failure" }
                    check(pictures.get() - before >= 45) { "Too few displayed frames: ${pictures.get() - before}" }
                    decoder.showStill()
                    Thread.sleep(150)
                    val settled = pictures.get()
                    Thread.sleep(200)
                    check(pictures.get() == settled) { "Video overwrote still state" }
                }
                check(acks.get() == 120) { "Missing acknowledgments: ${acks.get()}" }
                check(!wrongSize.get()) { "Rotation dimensions incorrect" }
                check(colors.size > 3) { "Decoded frames lack changing image content" }
            } finally { decoder.close() }
            results.putString("stream", "PASS hardware decode: ${pictures.get()} images, ${acks.get()} ACKs; landscape, portrait, and still transition verified\n")
            finish(Activity.RESULT_OK, results)
        } catch (error: Throwable) {
            results.putString("stream", "FAIL ${error.javaClass.simpleName}: ${error.message}\n")
            finish(Activity.RESULT_CANCELED, results)
        }
    }
}
