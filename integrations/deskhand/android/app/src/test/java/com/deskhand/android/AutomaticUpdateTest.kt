package com.deskhand.android

import android.os.Looper
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class AutomaticUpdateTest {
    @Test fun openingChecksSilentlyAndPromptsAgainForAvailableUpdate() {
        val requests = LinkedBlockingQueue<JSONObject>()
        AppUpdates.init(RuntimeEnvironment.getApplication())
        AppUpdates.opened()
        AppUpdates.connected { type, bytes ->
            if (type == Msg.UPDATE_CHECK) requests.offer(JSONObject(String(bytes)))
            true
        }
        fun request() = requests.poll(5, TimeUnit.SECONDS) ?: error("No update check sent")
        fun reply(id: String, available: Boolean) {
            val payload = JSONObject().put("requestId", id).put("available", available)
            if (available) payload.put("release", JSONObject(UpdateRelease("com.deskhand.android", 10000, "test", 1024, 28, "a".repeat(64)).json()))
            AppUpdates.message(Msg.UPDATE_INFO, payload.toString().toByteArray())
        }
        fun awaitStatus(expected: String) {
            repeat(200) {
                shadowOf(Looper.getMainLooper()).idle()
                if (AppUpdates.ui.status == expected) return
                Thread.sleep(10)
            }
            fail("Expected $expected, got ${AppUpdates.ui.status}")
        }
        val first = request()
        reply(first.getString("requestId"), false)
        awaitStatus("No update is published on this PC.")
        assertFalse(AppUpdates.ui.show)
        AppUpdates.opened()
        reply(request().getString("requestId"), true)
        awaitStatus("A Deskhand update is available.")
        assertTrue(AppUpdates.ui.show)
        AppUpdates.hide(); shadowOf(Looper.getMainLooper()).idle()
        AppUpdates.opened()
        reply(request().getString("requestId"), true)
        awaitStatus("A Deskhand update is available.")
        repeat(200) { shadowOf(Looper.getMainLooper()).idle(); if (AppUpdates.ui.show) return@repeat; Thread.sleep(10) }
        assertTrue("Opening again should prompt again", AppUpdates.ui.show)
        AppUpdates.disconnected()
        AppUpdates.hide()
    }
}
