package com.deskhand.android

import android.content.Context
import android.content.SharedPreferences
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.json.JSONObject
import java.util.UUID

/** No local API bearer on the phone. Reuses Deskhand's paired encrypted link. */
object JetstreamPhone {
    private var prefs: SharedPreferences? = null
    private val calls = mutableMapOf<String, Pair<String, String?>>()
    internal var sender: (JSONObject) -> String? = { Model.chatRequest(it) }
    private var latestStatus: String? = null
    var status by mutableStateOf("Refresh to connect to Jetstream")
    var project by mutableStateOf<JSONObject?>(null)
    var jobs by mutableStateOf(listOf<JSONObject>())
    var detail by mutableStateOf<JSONObject?>(null)
    var draft by mutableStateOf("")
    var pending by mutableStateOf<JSONObject?>(null)
    private var pc = ""
    fun init(context: Context) { prefs = context.getSharedPreferences("jetstream-actions", Context.MODE_PRIVATE); pc = ""; calls.clear(); project = null; detail = null; jobs = emptyList(); pending = null }
    fun refresh() {
        val id = Model.viewerPc?.id?.toString() ?: return
        if (pc != id) { pc = id; calls.clear(); project = null; jobs = emptyList(); detail = null; pending = prefs?.getString(pc, null)?.let { runCatching { JSONObject(it) }.getOrNull() } }
        if (Model.viewerConnection != "Connected") { status = "Deskhand disconnected"; return }
        if (calls.size > 8) calls.clear()
        send(JSONObject().put("op", "jetstream_status"))
        pending?.let { send(JSONObject().put("op", "jetstream_outcome").put("requestId", it.getString("requestId"))) }
    }
    private fun send(command: JSONObject) {
        if (command.optString("op") in listOf("jetstream_jobs", "jetstream_job")) project?.let { command.put("projectId", it.optString("id")) }
        val rpc = sender(command)
        if (rpc != null) { calls[rpc] = pc to command.optString("projectId").takeIf { it.isNotBlank() }; if (command.optString("op") == "jetstream_status") latestStatus = rpc } else status = "Disconnected; refresh to check the existing action"
    }
    fun inspect(id: Long) { send(JSONObject().put("op", "jetstream_job").put("jobId", id)) }
    fun action(action: String, id: Long? = null) {
        if (pending != null || project == null || Model.viewerConnection != "Connected" || Model.viewerPc?.control != true) return
        val command = JSONObject().put("op", "jetstream_action").put("projectId", project!!.getString("id")).put("requestId", UUID.randomUUID().toString()).put("action", action)
        if (action == "submit") command.put("request", draft)
        if (action == "cancel") command.put("jobId", id)
        if (action == "reject") command.put("candidateId", id)
        if (prefs?.edit()?.putString(pc, command.toString())?.commit() != true) { status = "Could not save action; nothing was sent"; return }
        pending = command; status = "Waiting for action outcome"; send(JSONObject(command.toString()))
    }
    fun retry() { if (Model.viewerConnection != "Connected" || Model.viewerPc?.control != true) return; pending?.let { send(JSONObject(it.toString())) } }
    fun receive(response: JSONObject) {
        val origin = calls.remove(response.optString("rpc")) ?: return
        if (origin.first != Model.viewerPc?.id || origin.first != pc) return
        if (response.optString("op") == "jetstream_status" && response.optString("rpc") != latestStatus) return
        if (response.optString("op") in listOf("jetstream_jobs", "jetstream_job") && origin.second != project?.optString("id")) return
        if (!response.optBoolean("ok")) { status = response.optString("error", "Jetstream unavailable"); return }
        val result = response.optJSONObject("result") ?: return
        when (response.optString("op")) {
            "jetstream_status" -> {
                val next = result.optJSONObject("project"); if (next?.optString("id") != project?.optString("id")) { jobs = emptyList(); detail = null }; project = next; status = if (project == null) "Open a project on the PC" else "Connected to Jetstream"
                send(JSONObject().put("op", "jetstream_jobs"))
            }
            "jetstream_jobs" -> { val items = result.optJSONArray("jobs"); jobs = (0 until (items?.length() ?: 0)).mapNotNull { items?.optJSONObject(it) } }
            "jetstream_job" -> detail = result
            "jetstream_action", "jetstream_outcome" -> {
                if (result.optString("requestId") != pending?.optString("requestId")) return
                val state = result.optString("state"); status = "Action $state"
                if (state == "completed" || state == "failed") {
                    if (prefs?.edit()?.remove(pc)?.commit() == true) { if (pending?.optString("action") == "submit" && state == "completed") draft = ""; pending = null }
                }
                send(JSONObject().put("op", "jetstream_jobs"))
            }
        }
    }
}

@Composable
fun JetstreamPanel(onClose: () -> Unit) {
    LaunchedEffect(Model.viewerPc?.id, Model.viewerConnection) { while (Model.viewerConnection == "Connected") { JetstreamPhone.refresh(); kotlinx.coroutines.delay(5000) } }
    AlertDialog(onDismissRequest = onClose, confirmButton = { TextButton(onClick = onClose) { Text("Close") } }, title = { Text("Jetstream Coder") }, text = {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(if (Model.viewerConnection == "Connected") JetstreamPhone.status else "Deskhand disconnected")
            TextButton(onClick = { JetstreamPhone.refresh() }) { Text("Refresh status") }
            Text(JetstreamPhone.project?.optString("name") ?: "No project")
            JetstreamPhone.pending?.let {
                Text("Action ${it.optString("requestId")} is unresolved. Check its outcome before starting another action.")
                TextButton(onClick = { JetstreamPhone.retry() }) { Text("Retry same request ID") }
            }
            val enabled = Model.viewerConnection == "Connected" && Model.viewerPc?.control == true && JetstreamPhone.project != null && JetstreamPhone.pending == null
            OutlinedTextField(value = JetstreamPhone.draft, onValueChange = { JetstreamPhone.draft = it.take(10000) }, label = { Text("Coding task") })
            Button(enabled = enabled && JetstreamPhone.draft.isNotBlank(), onClick = { JetstreamPhone.action("submit") }) { Text("Submit job") }
            Text("Submitting records the job; worker availability is shown by Jetstream. Apply and remote approval are unavailable.")
            Row { TextButton(enabled = enabled, onClick = { JetstreamPhone.action("pause") }) { Text("Pause") }; TextButton(enabled = enabled, onClick = { JetstreamPhone.action("resume") }) { Text("Resume") } }
            JetstreamPhone.jobs.forEach { job ->
                Text("#${job.optLong("id")} ${job.optString("request")}\n${job.optString("status")}")
                Row { TextButton(onClick = { JetstreamPhone.inspect(job.optLong("id")) }) { Text("Details") }; TextButton(enabled = enabled && job.optString("status") != "cancelled", onClick = { JetstreamPhone.action("cancel", job.optLong("id")) }) { Text("Cancel") } }
            }
            JetstreamPhone.detail?.let { d ->
                Text("Job evidence")
                Text(d.optString("receipt"))
                d.optJSONObject("candidate")?.let { c ->
                    Text(c.optString("diff")); Text("Review: ${c.optJSONObject("review")}"); Text("Checks: ${c.optJSONObject("checks")}"); Text("Blocked: ${c.optJSONArray("blockedReasons")}")
                    TextButton(enabled = enabled, onClick = { JetstreamPhone.action("reject", c.optLong("id")) }) { Text("Reject candidate") }
                }
            }
        }
    })
}
