package com.deskhand.android

import android.content.Context
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class JetstreamPanelTest {
    @Test fun actionSurvivesRestartAndRetriesWithSameIdentity() {
        val context = RuntimeEnvironment.getApplication()
        context.getSharedPreferences("jetstream-actions", Context.MODE_PRIVATE).edit().clear().commit()
        Model.viewerPc = PairedPc("fixture", "ws://127.0.0.1:1", ByteArray(32) { 7 }, true)
        Model.viewerConnection = "Connected"
        val sent = mutableListOf<Pair<String, JSONObject>>()
        JetstreamPhone.sender = { c -> val rpc = "rpc-${sent.size}"; sent.add(rpc to JSONObject(c.toString())); rpc }
        try {
            JetstreamPhone.init(context); JetstreamPhone.refresh()
            val first = sent.last()
            JetstreamPhone.receive(JSONObject().put("rpc",first.first).put("op","jetstream_status").put("ok",true).put("result",JSONObject().put("project",JSONObject().put("id","a".repeat(64)).put("name","fixture"))))
            JetstreamPhone.draft = "add a test"; JetstreamPhone.action("submit")
            val action = sent.last().second
            assertEquals("a".repeat(64), action.getString("projectId"))
            JetstreamPhone.action("pause"); assertEquals(action.getString("requestId"), sent.last().second.getString("requestId"))
            JetstreamPhone.init(context); JetstreamPhone.refresh()
            assertEquals(action.getString("requestId"),JetstreamPhone.pending!!.getString("requestId"))
            JetstreamPhone.retry(); assertEquals(action.toString(), sent.last().second.toString())
            val rpc = sent.last().first
            JetstreamPhone.receive(JSONObject().put("rpc",rpc).put("op","jetstream_action").put("ok",true).put("result",JSONObject().put("requestId",action.getString("requestId")).put("state","completed")))
            assertNull(JetstreamPhone.pending)
            assertNull(context.getSharedPreferences("jetstream-actions",Context.MODE_PRIVATE).getString(Model.viewerPc!!.id,null))
        } finally { JetstreamPhone.sender = { Model.chatRequest(it) }; Model.viewerConnection = "Disconnected" }
    }

    @Test fun delayedResponseFromAnotherPcCannotReplaceTheProject() {
        val context=RuntimeEnvironment.getApplication();JetstreamPhone.init(context)
        Model.viewerConnection="Connected";Model.viewerPc=PairedPc("first","ws://127.0.0.1:1",ByteArray(32){1},true)
        JetstreamPhone.sender={"old-rpc"}
        try { JetstreamPhone.refresh();Model.viewerPc=PairedPc("second","ws://127.0.0.1:2",ByteArray(32){2},true)
            JetstreamPhone.receive(JSONObject().put("rpc","old-rpc").put("op","jetstream_status").put("ok",true).put("result",JSONObject().put("project",JSONObject().put("id","old-project"))))
            assertNull(JetstreamPhone.project)
        } finally { JetstreamPhone.sender={Model.chatRequest(it)};Model.viewerConnection="Disconnected" }
    }
}
