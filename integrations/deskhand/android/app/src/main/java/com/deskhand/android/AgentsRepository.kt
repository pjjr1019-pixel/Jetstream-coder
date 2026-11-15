package com.deskhand.android

import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone


/**
 * Muse-agent data for the Android right-side drawer (v100.003, VM prep
 * — ships as 100.003 from the drawer chat per Phil's reversal
 * 2026-11-08 ~22:37 EST; moved out of the polish run, now v100.007).
 *
 * The drawer shows what the Subagent Monitor space already shows in Muse:
 * how many Muse subagents are running, what each one is doing, and how
 * long it has been running.
 *
 * 100.017: agentsFromRuntimeEvents is restored here with the 100.018
 * design §A5 fixes (epoch `ts` first with ISO fallback, finished deduped
 * by agent_id, `subagent.dropped` removal, feed-health fields, chatLabel
 * from the event). Model.fetchAgentsSnapshot() and the push subscriber
 * feed it the whole `runtime_stream` response (events + sources +
 * counts). Before 100.017 this file was model + formatters only and the
 * drawer kept its honest empty state.
 */

// 100.017: agentsFromRuntimeEvents is RESTORED (B2-G removed it in 100.030
// — zero callers, feed unwired) with the 100.018 design §A5 fixes, and
// Model.fetchAgentsSnapshot() + the push subscriber call it. The drawer
// fetch (100.018 Part B) is absorbed into 100.017 (STEP0 §0.10).
// parseAgentsSnapshot stays removed: the Subagent Monitor snapshot lives
// in the Muse space, unreachable from the phone — the live path is the
// runtime_stream window only.

enum class AgentStatus { RUNNING, DONE, FAILED }

/** One Muse subagent, as shown on a drawer card. */
data class MuseAgent(
    /** Full agent id; [shortId] is the display form. */
    val id: String,
    /** Full task text (the card shows the first lines + "Show full task"). */
    val task: String,
    /** Friendly chat name when known ("Main chat", "DeskHand chat", …). */
    val chatLabel: String?,
    /** Epoch ms when the agent started (0 when unknown). */
    val startedAtMs: Long,
    /** Seconds since the agent last did something (null when unknown). */
    val lastActiveSec: Long?,
    val status: AgentStatus,
    /** Finished only: total run seconds. */
    val durationSec: Long?,
    /** Finished only: what the agent reported, first lines on the card. */
    val result: String?,
) {
    /** "c583512f" — first 8 of the id, the same short form the stream uses. */
    val shortId: String get() = id.take(8)
}

/** Everything the drawer renders in one poll. */
data class AgentsSnapshot(
    val running: List<MuseAgent>,
    val finished: List<MuseAgent>,
    /** Epoch ms the snapshot was built (0 when never fetched). */
    val updatedAtMs: Long,
    // 100.017 (100.018 design §A5d): feed health + latest activity, all
    // defaulted so existing call sites still compile. newestEventTsMs is
    // the max event `ts` seen in the window (0 when no events);
    // latestActivity is the newest non-subagent event as
    // "<type> — <summary>"; vmEventsSource is the op's
    // sources.vm_events ("ok" / "missing" / "error"); skippedBadLines is
    // counts.skipped_bad_lines.
    val newestEventTsMs: Long = 0L,
    val latestActivity: String? = null,
    val vmEventsSource: String = "missing",
    val skippedBadLines: Int = 0,
) {
    val runningCount: Int get() = running.size
    val totalCount: Int get() = running.size + finished.size

    companion object {
        val EMPTY = AgentsSnapshot(emptyList(), emptyList(), 0L)
    }
}

/** "6m 30s" for 390s; "1h 4m" past the hour; "45s" under a minute. */
fun formatElapsed(totalSec: Long): String {
    if (totalSec < 0) return "—"
    val h = totalSec / 3600
    val m = (totalSec % 3600) / 60
    val s = totalSec % 60
    return when {
        h > 0 -> "${h}h ${m}m"
        m > 0 -> "${m}m ${s}s"
        else -> "${s}s"
    }
}

/** "last active 2m ago" / "active now" (under 30s) / "—" when unknown. */
fun formatLastActive(lastActiveSec: Long?): String = when {
    lastActiveSec == null -> "—"
    lastActiveSec < 30 -> "active now"
    lastActiveSec < 60 -> "last active ${lastActiveSec}s ago"
    lastActiveSec < 3600 -> "last active ${lastActiveSec / 60}m ago"
    else -> "last active ${lastActiveSec / 3600}h ago"
}

/** "updated 40s ago" for the drawer header; "never" when never fetched. */
fun formatUpdatedAgo(updatedAtMs: Long, nowMs: Long): String {
    if (updatedAtMs <= 0) return "never updated"
    val ageSec = (nowMs - updatedAtMs) / 1000
    return "updated ${formatElapsed(ageSec)} ago"
}

/**
 * Parse an ISO-8601 timestamp to epoch ms. Accepts "…T…:…:…Z",
 * "…T…:…:….…Z", "+00:00" offsets (the `X` pattern), and — as a last
 * resort — a zone-less "...T...:..:.." read as UTC. Returns 0 when the
 * string is blank or unparseable: the caller treats 0 as "unknown", never
 * as a real time.
 */
internal fun parseIsoMs(iso: String): Long {
    if (iso.isBlank()) return 0L
    val patterns = listOf(
        "yyyy-MM-dd'T'HH:mm:ss.SSSX",
        "yyyy-MM-dd'T'HH:mm:ssX",
        "yyyy-MM-dd'T'HH:mm:ss",
    )
    for (pattern in patterns) {
        val parsed = runCatching {
            SimpleDateFormat(pattern, Locale.US)
                .apply { timeZone = TimeZone.getTimeZone("UTC") }
                .parse(iso)
        }.getOrNull()
        if (parsed != null) return parsed.time
    }
    return 0L
}

/** One event's `ts` as epoch ms — epoch seconds FIRST (the live service
 * emits a JSON number), ISO-8601 fallback (100.018 design §A5a). */
private fun eventTsMs(o: JSONObject): Long {
    val epoch = o.optDouble("ts", Double.NaN)
    if (epoch.isFinite()) return (epoch * 1000.0).toLong()
    return parseIsoMs(o.optString("ts", ""))
}

/**
 * Rebuild the running/finished set from one WHOLE `runtime_stream`
 * response (events + sources + counts) — 100.018 design §A5, restored in
 * 100.017 (STEP0 §0.10). The caller passes the full window every time
 * (no `since`): spawn/finish pairing needs the whole window, and a delta
 * would strand agents whose spawn fell outside it. Pure logic, covered
 * by StreamPushTest.
 *
 * - Cards come from `subagent.*` events only. `turn_` / `file_` events
 *   never become cards — the newest one is `latestActivity` (§C2).
 * - Finished is deduped by agent_id (keep last), capped at 8 newest
 *   first. `subagent.dropped` removes from running WITHOUT finishing
 *   (the outcome is unknown — a fabricated FAILED would be a lie).
 * - No turn→agent mapping, no dropping running agents by age:
 *   `subagent.dropped` is the only removal path (§A5e).
 * - A heartbeat for an agent with no spawn in the window is ignored:
 *   without a task there is no card to show, and inventing one would
 *   be a fabrication.
 */
internal fun agentsFromRuntimeEvents(responseJson: String, nowMs: Long): AgentsSnapshot {
    val response = runCatching { JSONObject(responseJson) }.getOrNull() ?: return AgentsSnapshot.EMPTY
    val events = response.optJSONArray("events")
    val sources = response.optJSONObject("sources")
    val counts = response.optJSONObject("counts")
    val running = linkedMapOf<String, MuseAgent>()
    val finishedById = linkedMapOf<String, Pair<MuseAgent, Long>>()
    var newestEventTsMs = 0L
    var latestActivity: String? = null
    var latestActivityTsMs = -1L
    if (events != null) {
        for (i in 0 until events.length()) {
            val o = runCatching { events.getJSONObject(i) }.getOrNull() ?: continue
            val tsMs = eventTsMs(o)
            if (tsMs > newestEventTsMs) newestEventTsMs = tsMs
            val type = o.optString("type", "")
            val summary = o.optString("summary", "")
            // 100.017 deploy-time fix: the service emits underscore types
            // ("turn_done", "file_write") but the spec example and the
            // Worker A contract use dot types ("turn.done", "file.write")
            // — and the drawer-feed spec itself mixes them in one line.
            // Accept both separators so neither convention is dropped
            // from latestActivity; the type is echoed verbatim.
            if (type.startsWith("turn_") || type.startsWith("turn.") || type.startsWith("file_") || type.startsWith("file.")) {
                if (tsMs >= latestActivityTsMs) {
                    latestActivityTsMs = tsMs
                    latestActivity = "$type — $summary"
                }
                continue
            }
            if (!type.startsWith("subagent.")) continue
            val detail = o.optJSONObject("detail") ?: JSONObject()
            val agentId = detail.optString("agent_id").ifBlank { detail.optString("agent") }
            if (agentId.isBlank()) continue
            val chatLabel = detail.optString("chat_label")
                .ifBlank { detail.optString("chatLabel") }
                .takeIf { it.isNotBlank() }
            when (type) {
                "subagent.spawned" -> {
                    val task = detail.optString("task", summary)
                    val lastActiveSec = if (tsMs > 0) ((nowMs - tsMs) / 1000).coerceAtLeast(0) else null
                    running[agentId] = MuseAgent(
                        id = agentId, task = task, chatLabel = chatLabel,
                        startedAtMs = tsMs, lastActiveSec = lastActiveSec,
                        status = AgentStatus.RUNNING, durationSec = null, result = null,
                    )
                }
                "subagent.heartbeat" -> {
                    val current = running[agentId] ?: continue
                    val age = detail.optLong("last_active_age_sec", -1L)
                    val lastActiveSec = if (age >= 0) age
                        else if (tsMs > 0) ((nowMs - tsMs) / 1000).coerceAtLeast(0)
                        else current.lastActiveSec
                    running[agentId] = current.copy(lastActiveSec = lastActiveSec)
                }
                "subagent.finished", "subagent.failed" -> {
                    val prior = running.remove(agentId)
                    val task = detail.optString("task").ifBlank { prior?.task ?: summary }
                    val label = chatLabel ?: prior?.chatLabel
                    val startedAtMs = prior?.startedAtMs ?: tsMs
                    val duration = detail.optLong("duration_sec", -1L).takeIf { it >= 0 }
                        ?: if (prior != null && tsMs > 0 && prior.startedAtMs > 0) {
                            (tsMs - prior.startedAtMs) / 1000
                        } else null
                    val result = detail.optString("result").takeIf { it.isNotBlank() }
                    val status = if (type == "subagent.failed") AgentStatus.FAILED else AgentStatus.DONE
                    finishedById[agentId] = MuseAgent(
                        id = agentId, task = task, chatLabel = label,
                        startedAtMs = startedAtMs, lastActiveSec = null,
                        status = status, durationSec = duration, result = result,
                    ) to tsMs
                }
                "subagent.dropped" -> {
                    running.remove(agentId)
                    // Never added to finished: the outcome is unknown.
                }
            }
        }
    }
    val finished = finishedById.values.sortedByDescending { it.second }.take(8).map { it.first }
    return AgentsSnapshot(
        running = running.values.sortedBy { it.startedAtMs },
        finished = finished,
        updatedAtMs = nowMs,
        newestEventTsMs = newestEventTsMs,
        latestActivity = latestActivity,
        vmEventsSource = sources?.optString("vm_events", "missing") ?: "missing",
        skippedBadLines = counts?.optInt("skipped_bad_lines", 0) ?: 0,
    )
}

// ---------------------------------------------------------------------------
// Message board (100.033, spec §3, §4, §7.1) — BoardPost model + the
// event-leg parser boardPostsFromRuntimeEvents, following the
// agentsFromRuntimeEvents pattern exactly: pure logic, org.json only,
// never-throw (EMPTY on bad input), epoch-first eventTsMs for `ts` (the
// service emits ts as an epoch float, NOT ISO-8601 — the eventTsMs
// pattern above is copied, never re-derived).
//
// RE-SCOPE (Step 0 S0-6): the Monitor snapshot JSON is unreachable from
// the phone, so there is NO snapshot leg on this client and
// parseAgentsSnapshot is NOT resurrected. Derived spawn/result posts come
// from subagent.spawned / subagent.finished / subagent.failed events in
// the runtime_stream window — the same VM pipe as step notes and relays
// — so their origin is LIVE, never SNAPSHOT. Do NOT add a snapshot fetch
// (spec §12 #10). A second fetch for the same data is likewise forbidden:
// Model.fetchBoardPosts() rides fetchAgentsSnapshot()'s request.
// ---------------------------------------------------------------------------

/** Board author kinds (spec §3.1). The kind decides the chip colour, never the text. */
enum class BoardAuthorKind { SUBAGENT, OUTSIDE_AI, JETT, COORDINATOR, SYSTEM }

/** Board post kinds (spec §3.1). REPLY is stage 3 only — the kind exists so the shape does not change when stage 3 lands. */
enum class BoardPostKind { SPAWN, STEP, RESULT, RELAY, REPLY, SYSTEM }

/** Honesty label, shown on every post (spec §3.1, §9.2). SNAPSHOT is never emitted on this client (see above). */
enum class BoardOrigin { LIVE, SNAPSHOT, RELAY }

/**
 * One board post (spec §3.1). `text` is the FULL body — the UI applies
 * the per-kind display cap (boardTextCap / boardDisplayText) and the
 * "show more" expansion; the model never truncates, so truncation can
 * never paraphrase. `tsMs` is the TRUE event time (epoch ms), never
 * arrival time — a late event slots mid-list at its true time.
 * `saidTsMs` / `relayedTsMs` are relay-only (source message time vs
 * relay time); `status` / `elapsedSec` / `durationSec` are agent-linked
 * only (a relay post carries none — an outside AI has no runtime here).
 */
data class BoardPost(
    /** Stable dedup key, never re-minted (spec §3.1 — see boardPostId). */
    val id: String,
    /** True event time, epoch ms. */
    val tsMs: Long,
    val authorKind: BoardAuthorKind,
    /** Display name, short — never an id, never a hash. */
    val authorName: String,
    val chatId: String?,
    val chatLabel: String?,
    val kind: BoardPostKind,
    /** Full post body (uncapped — see above). */
    val text: String,
    /** Agent-linked posts: the agent id. */
    val agentId: String?,
    /** Agent-linked posts: the full task text (the row shows an excerpt). */
    val task: String?,
    /** Relay posts: the source chat id (required — a relay without it is dropped). */
    val sourceChatId: String?,
    /** Relay posts: the source message id (required — a relay without it is dropped). */
    val sourceMessageId: String?,
    val origin: BoardOrigin,
    /** Agent-linked posts only: mirrors the agent status. */
    val status: AgentStatus?,
    /** Agent-linked posts: seconds running at derivation time (ticks live on the client from tsMs). */
    val elapsedSec: Long?,
    /** Finished posts only: total run seconds. */
    val durationSec: Long?,
    /** Relay only: the source message's time (== tsMs when known). */
    val saidTsMs: Long?,
    /** Relay only: when the coordinator relayed it. */
    val relayedTsMs: Long?,
)

/** Display cap in chars per kind (spec §3.1 / §10.1 V5). The model stores full text; this caps only the collapsed row. */
fun boardTextCap(kind: BoardPostKind): Int = when (kind) {
    BoardPostKind.SPAWN -> 300
    BoardPostKind.STEP -> 500
    BoardPostKind.RESULT -> 500
    BoardPostKind.RELAY -> 800
    BoardPostKind.REPLY -> 500
    BoardPostKind.SYSTEM -> 500
}

/** The row text: full body when expanded, capped at the kind cap otherwise. Never paraphrases — a prefix of the full text. */
fun boardDisplayText(post: BoardPost, expanded: Boolean): String =
    if (expanded) post.text else post.text.take(boardTextCap(post.kind))

/** True when the collapsed row hides text behind "show more". */
fun boardNeedsShowMore(post: BoardPost): Boolean = post.text.length > boardTextCap(post.kind)

/**
 * Newest-first order with a deterministic tie-break (spec §3.2): equal
 * `ts` breaks by `id` ascending, so two clients render the same order.
 */
fun sortBoardPosts(posts: List<BoardPost>): List<BoardPost> =
    posts.sortedWith(compareByDescending<BoardPost> { it.tsMs }.thenBy { it.id })

/**
 * Merge two post lists, deduping by `id` (spec §9.7): the same post
 * arriving through both legs, twice through one leg, or after a resync
 * renders once. First-seen wins (same id = same post). Result is
 * newest-first, capped at the newest 100 (spec §3.4).
 */
fun mergeBoardPosts(existing: List<BoardPost>, incoming: List<BoardPost>): List<BoardPost> {
    val byId = linkedMapOf<String, BoardPost>()
    for (post in existing) byId.putIfAbsent(post.id, post)
    for (post in incoming) byId.putIfAbsent(post.id, post)
    return sortBoardPosts(byId.values.toList()).take(100)
}

/**
 * Stable post id (spec §3.1). VM-pipe posts use `vm:seq:<seq>` when the
 * event carries a seq; relay posts prefer
 * `relay:<sourceChatId>:<sourceMessageId>` (stable across a resync,
 * where seq is re-numbered). The fallback is content-derived
 * (`vm:<kind>:<agentId>:<tsMs>`), so an event without a seq still dedups
 * and a re-run of the same agent is a new post, not a collision.
 */
private fun boardPostId(seq: Long, kind: String, agentId: String, tsMs: Long, sourceChatId: String?, sourceMessageId: String?): String =
    if (!sourceChatId.isNullOrBlank() && !sourceMessageId.isNullOrBlank() && kind == "relay") {
        "relay:$sourceChatId:$sourceMessageId"
    } else if (seq >= 0) {
        "vm:seq:$seq"
    } else {
        "vm:$kind:$agentId:$tsMs"
    }

/** One event's `seq` when present (-1 when absent — never 0, which is a real seq). */
private fun eventSeq(o: JSONObject): Long = if (o.has("seq")) o.optLong("seq", -1L) else -1L

/**
 * One detail timestamp as epoch ms — epoch seconds FIRST (a JSON
 * number), ISO-8601 fallback, via the same eventTsMs pattern as the
 * agents parser. Returns 0 when the key is absent or unparseable.
 * Used for relay `saidTs` / `relayedTs` (spec §3.2), which the writer
 * may emit as either an epoch float or an ISO string.
 */
private fun detailTsMs(detail: JSONObject, vararg keys: String): Long {
    for (key in keys) {
        if (!detail.has(key)) continue
        val probe = JSONObject().put("ts", detail.opt(key))
        val ms = eventTsMs(probe)
        if (ms > 0) return ms
    }
    return 0L
}

/**
 * Derive board posts from one WHOLE `runtime_stream` response (events +
 * sources + counts) — the event leg only (100.033 re-scope, Step 0
 * S0-6). Pure logic, never throws: bad / empty / malformed JSON yields
 * an empty list. Derivation per spec §4.1, re-pointed at the event leg:
 *
 * - `subagent.spawned` → SPAWN post (task excerpt, origin LIVE, status
 *   RUNNING, elapsed ticking from the spawn time). An agent running in
 *   both windows derives no new post — dedup by id prevents it even if
 *   a client re-derives.
 * - `subagent.finished` / `subagent.failed` → RESULT post (result
 *   summary, or "no result reported" + task excerpt when no result was
 *   reported — never invented; status DONE/FAILED, duration set from
 *   detail.duration_sec or computed from the spawn in this window).
 * - `subagent.heartbeat` → no post (the elapsed time ticks client-side).
 * - `subagent.dropped` / an agent with no events in the window → no
 *   post. Disappearance is a feed gap, not an event — the board must
 *   not invent a finish.
 * - `board.post` with detail.kind "step" → STEP post (the agent's own
 *   note, origin LIVE). With detail.kind "relay" → RELAY post (the
 *   quoted words verbatim, origin RELAY, ts = the source message's
 *   saidTs, relayedTs visible in the detail). A relay post WITHOUT
 *   ref.sourceChatId + ref.sourceMessageId is dropped — an unattributed
 *   quote is a defect, never a post. Any other board.post kind is
 *   skipped (never invented).
 * - `turn_` / `file_` events never become posts.
 *
 * Result is newest-first (equal ts breaks by id). Spawn/finish pairing
 * needs the whole window, so the caller passes the full response, never
 * a delta — same rule as agentsFromRuntimeEvents.
 */
fun boardPostsFromRuntimeEvents(responseJson: String, nowMs: Long): List<BoardPost> {
    val response = runCatching { JSONObject(responseJson) }.getOrNull() ?: return emptyList()
    val events = response.optJSONArray("events") ?: return emptyList()
    val byId = linkedMapOf<String, BoardPost>()
    // Spawn info seen in this window, for result durations + step elapsed.
    val spawnedAtMs = mutableMapOf<String, Long>()
    val spawnedTask = mutableMapOf<String, String>()
    val spawnedChatLabel = mutableMapOf<String, String?>()
    fun add(post: BoardPost) {
        byId.putIfAbsent(post.id, post)
    }
    for (i in 0 until events.length()) {
        val o = runCatching { events.getJSONObject(i) }.getOrNull() ?: continue
        val tsMs = eventTsMs(o)
        val seq = eventSeq(o)
        val type = o.optString("type", "")
        if (type.startsWith("turn_") || type.startsWith("turn.") || type.startsWith("file_") || type.startsWith("file.")) continue
        val detail = o.optJSONObject("detail") ?: JSONObject()
        val summary = o.optString("summary", "")
        val actor = o.optString("actor", "")
        val chatLabel = detail.optString("chat_label")
            .ifBlank { detail.optString("chatLabel") }
            .takeIf { it.isNotBlank() }
        val chatId = detail.optString("chat_id")
            .ifBlank { detail.optString("chatId") }
            .takeIf { it.isNotBlank() }
        val agentId = detail.optString("agent_id").ifBlank { detail.optString("agent") }
        val name = detail.optString("name").ifBlank { detail.optString("role") }
            .ifBlank { actor }.ifBlank { agentId.take(8) }
        val elapsedNow = if (tsMs > 0) ((nowMs - tsMs) / 1000).coerceAtLeast(0) else null
        when (type) {
            "subagent.spawned" -> {
                if (agentId.isBlank()) continue
                val task = detail.optString("task", summary)
                spawnedAtMs[agentId] = tsMs
                spawnedTask[agentId] = task
                spawnedChatLabel[agentId] = chatLabel
                add(
                    BoardPost(
                        id = boardPostId(seq, "spawn", agentId, tsMs, null, null),
                        tsMs = tsMs, authorKind = BoardAuthorKind.SUBAGENT,
                        authorName = name, chatId = chatId, chatLabel = chatLabel,
                        kind = BoardPostKind.SPAWN, text = task,
                        agentId = agentId, task = task,
                        sourceChatId = null, sourceMessageId = null,
                        origin = BoardOrigin.LIVE, status = AgentStatus.RUNNING,
                        elapsedSec = elapsedNow, durationSec = null,
                        saidTsMs = null, relayedTsMs = null,
                    )
                )
            }
            "subagent.finished", "subagent.failed" -> {
                if (agentId.isBlank()) continue
                val task = detail.optString("task").ifBlank { spawnedTask[agentId] ?: summary }
                val label = chatLabel ?: spawnedChatLabel[agentId]
                val startedAtMs = spawnedAtMs[agentId]
                val duration = detail.optLong("duration_sec", -1L).takeIf { it >= 0 }
                    ?: if (startedAtMs != null && tsMs > 0 && startedAtMs > 0) {
                        (tsMs - startedAtMs) / 1000
                    } else null
                val result = detail.optString("result").takeIf { it.isNotBlank() }
                val text = if (result != null) result
                    else if (task.isNotBlank()) "no result reported — $task"
                    else "no result reported"
                add(
                    BoardPost(
                        id = boardPostId(seq, "result", agentId, tsMs, null, null),
                        tsMs = tsMs, authorKind = BoardAuthorKind.SUBAGENT,
                        authorName = name, chatId = chatId, chatLabel = label,
                        kind = BoardPostKind.RESULT, text = text,
                        agentId = agentId, task = task,
                        sourceChatId = null, sourceMessageId = null,
                        origin = BoardOrigin.LIVE,
                        status = if (type == "subagent.failed") AgentStatus.FAILED else AgentStatus.DONE,
                        elapsedSec = null, durationSec = duration,
                        saidTsMs = null, relayedTsMs = null,
                    )
                )
            }
            "board.post" -> {
                val postKind = detail.optString("kind", "")
                val text = detail.optString("text").ifBlank { summary }
                when (postKind) {
                    "step" -> {
                        if (agentId.isBlank()) continue
                        val startedAtMs = spawnedAtMs[agentId]
                        val elapsed = if (startedAtMs != null && tsMs > 0 && startedAtMs > 0) {
                            ((nowMs - startedAtMs) / 1000).coerceAtLeast(0)
                        } else elapsedNow
                        add(
                            BoardPost(
                                id = boardPostId(seq, "step", agentId, tsMs, null, null),
                                tsMs = tsMs, authorKind = BoardAuthorKind.SUBAGENT,
                                authorName = name, chatId = chatId,
                                chatLabel = chatLabel ?: spawnedChatLabel[agentId],
                                kind = BoardPostKind.STEP, text = text,
                                agentId = agentId, task = spawnedTask[agentId],
                                sourceChatId = null, sourceMessageId = null,
                                origin = BoardOrigin.LIVE, status = AgentStatus.RUNNING,
                                elapsedSec = elapsed, durationSec = null,
                                saidTsMs = null, relayedTsMs = null,
                            )
                        )
                    }
                    "relay" -> {
                        val sourceChatId = detail.optString("sourceChatId").takeIf { it.isNotBlank() }
                        val sourceMessageId = detail.optString("sourceMessageId").takeIf { it.isNotBlank() }
                        // A relay post without its provenance is a defect:
                        // drop it rather than render an unattributed quote.
                        if (sourceChatId == null || sourceMessageId == null) continue
                        val saidMs = detailTsMs(detail, "saidTs", "said_ts")
                        val relayedMs = detailTsMs(detail, "relayedTs", "relayed_ts")
                            .takeIf { it > 0 } ?: if (tsMs > 0) tsMs else 0L
                        add(
                            BoardPost(
                                id = boardPostId(seq, "relay", "", saidMs, sourceChatId, sourceMessageId),
                                // Late relaying must not re-date the words:
                                // the post sorts by the source message's time.
                                tsMs = if (saidMs > 0) saidMs else tsMs,
                                authorKind = BoardAuthorKind.OUTSIDE_AI,
                                authorName = name, chatId = chatId, chatLabel = chatLabel,
                                kind = BoardPostKind.RELAY, text = text,
                                agentId = null, task = null,
                                sourceChatId = sourceChatId, sourceMessageId = sourceMessageId,
                                origin = BoardOrigin.RELAY, status = null,
                                elapsedSec = null, durationSec = null,
                                saidTsMs = if (saidMs > 0) saidMs else null,
                                relayedTsMs = if (relayedMs > 0) relayedMs else null,
                            )
                        )
                    }
                    else -> Unit // Unknown board.post kind: never invented.
                }
            }
            else -> Unit // Heartbeat, dropped, and anything else: no post.
        }
    }
    return sortBoardPosts(byId.values.toList())
}

// ---------------------------------------------------------------------------
// Muse-worker state (100.037, E12 watchdog, spec §7.4, §10.4) — the SECOND
// footer line in the drawer, derived from `worker.*` stream events in the
// same runtime_stream window as the board posts, following the
// boardPostsFromRuntimeEvents pattern exactly: pure logic, org.json only,
// never-throw, epoch-first timestamps (detailTsMs), whole window passed
// by the caller, previous state folded in so the state survives events
// scrolling out of the window.
//
// TWO FACTS, NEVER ONE BLENDED CLAIM (spec §7.4, drawer comment
// AgentsDrawer.kt:392-395): the existing workerFooterLine (relay-based,
// from the health snapshot's `worker` row) is the phone link. THIS state
// is the Muse worker (the gateway worker session) — a different session.
// The two lines are rendered separately and never merged into one claim.
//
// Event vocabulary (spec §10.4; the service emits via _append_vm_event):
// - worker_disconnected: detail.disconnected_since (first-miss time),
//   signal, cause. Fires on the 3rd consecutive miss.
// - worker_reconnect_attempt: detail.attempt (n of 10), kind
//   (auto/manual), next_delay_s.
// - worker_auto_reconnect_stopped: detail.attempts, reason
//   (cap / grant-off / grant-paused / reconcile-failing).
// - worker_reconnected: detail.down_seconds, reconnected_at. Emitted
//   ONLY on verified recovery (clean reconcile AND ready — spec §5.4),
//   so a CONNECTED state derived here is verified, never inferred.
// Underscore and dot spellings are both accepted ("worker_disconnected"
// and "worker.disconnected") — the same separator tolerance the 100.017
// parser applies to turn_/file_ events.
//
// NO phone-side Reconnect button in v1 (spec §10.3, out of scope) and
// no heartbeat is invented: there is no heartbeat event in the
// vocabulary, so the connected form is the verified-recovery form
// (spec §7.3), never a ticking "last heartbeat Ns ago" the phone cannot
// know. Before any worker.* event has ever been seen the state is
// UNKNOWN — "Muse worker status unknown" — not a guessed "connected".
// ---------------------------------------------------------------------------

/** Muse-worker connection, as last derived from `worker.*` events. */
enum class MuseWorkerConnection { UNKNOWN, CONNECTED, DISCONNECTED }

/**
 * The Muse-worker state for the drawer's second footer line. All times
 * are epoch ms (0 = unknown); `nextDelaySec` is null when no next
 * attempt is scheduled; `stoppedReason` is non-null only after a
 * `worker_auto_reconnect_stopped` event ("cap" / "grant-off" /
 * "grant-paused" / "reconcile-failing"); `lastEventTsMs` is the newest worker-event time
 * folded in, the guard against an older window regressing the state.
 */
data class MuseWorkerState(
    val connection: MuseWorkerConnection = MuseWorkerConnection.UNKNOWN,
    val disconnectedSinceMs: Long = 0L,
    val lastAttemptMs: Long = 0L,
    val lastAttemptNumber: Int = 0,
    val nextDelaySec: Long? = null,
    val attempts: Int = 0,
    val stoppedReason: String? = null,
    val reconnectedAtMs: Long = 0L,
    val downSeconds: Long? = null,
    val lastEventTsMs: Long = 0L,
)

/**
 * Derive the Muse-worker state from one WHOLE `runtime_stream` response
 * (events + sources + counts), folded over `previous` — the same
 * whole-window rule as agentsFromRuntimeEvents / boardPostsFromRuntimeEvents.
 * Pure logic, never throws: bad / empty / malformed JSON yields `previous`
 * (or a fresh UNKNOWN state when there is none); a window with no
 * `worker.*` events leaves the state unchanged, so the line survives
 * events scrolling out of the window; malformed events are skipped.
 *
 * Folding rules, in window order:
 * - `worker_disconnected` → DISCONNECTED, disconnected_since from the
 *   detail (epoch seconds first, ISO fallback; the event's own `ts`
 *   when the detail carries none). Starts a NEW episode: any prior
 *   attempt / stop / recovery facts are cleared.
 * - `worker_reconnect_attempt` → DISCONNECTED, last attempt time/number
 *   + next delay. Clears `stoppedReason`: a new attempt of either kind
 *   means reconnect is trying again, so the "stopped" claim no longer
 *   describes the present.
 * - `worker_auto_reconnect_stopped` → DISCONNECTED, attempts + reason,
 *   next delay cleared (no next attempt is scheduled).
 * - `worker_reconnected` → CONNECTED, reconnected_at (detail, else the
 *   event `ts`) + down_seconds. Clears the disconnect episode; the
 *   recovery facts are what the footer renders.
 * - Any other type (including `turn_` / `file_` / `subagent.*` /
 *   `board.post` and unknown `worker.*` types) is ignored.
 * An event older than the newest worker event already folded in is
 * skipped, so a resynced or older window never regresses the state;
 * re-applying the same event is idempotent.
 */
fun workerStateFromRuntimeEvents(
    responseJson: String,
    nowMs: Long,
    previous: MuseWorkerState = MuseWorkerState(),
): MuseWorkerState {
    val response = runCatching { JSONObject(responseJson) }.getOrNull() ?: return previous
    val events = response.optJSONArray("events") ?: return previous
    var state = previous
    for (i in 0 until events.length()) {
        val o = runCatching { events.getJSONObject(i) }.getOrNull() ?: continue
        val tsMs = eventTsMs(o)
        val type = o.optString("type", "").replace('.', '_')
        if (!type.startsWith("worker_")) continue
        if (tsMs > 0 && state.lastEventTsMs > 0 && tsMs < state.lastEventTsMs) continue
        val detail = o.optJSONObject("detail") ?: JSONObject()
        val appliedTsMs = if (tsMs > 0) tsMs else state.lastEventTsMs
        when (type) {
            "worker_disconnected" -> {
                val sinceMs = detailTsMs(detail, "disconnected_since")
                state = state.copy(
                    connection = MuseWorkerConnection.DISCONNECTED,
                    disconnectedSinceMs = if (sinceMs > 0) sinceMs else tsMs,
                    lastAttemptMs = 0L,
                    lastAttemptNumber = 0,
                    nextDelaySec = null,
                    attempts = 0,
                    stoppedReason = null,
                    reconnectedAtMs = 0L,
                    downSeconds = null,
                    lastEventTsMs = appliedTsMs,
                )
            }
            "worker_reconnect_attempt" -> {
                val attemptNo = detail.optLong("attempt", -1L)
                val delay = if (detail.has("next_delay_s")) {
                    detail.optLong("next_delay_s", -1L).takeIf { it >= 0 }
                } else null
                state = state.copy(
                    connection = MuseWorkerConnection.DISCONNECTED,
                    lastAttemptMs = if (tsMs > 0) tsMs else state.lastAttemptMs,
                    lastAttemptNumber = if (attemptNo >= 0) attemptNo.toInt() else state.lastAttemptNumber,
                    nextDelaySec = delay ?: state.nextDelaySec,
                    attempts = if (attemptNo >= 0) attemptNo.toInt() else state.attempts,
                    stoppedReason = null,
                    lastEventTsMs = appliedTsMs,
                )
            }
            "worker_auto_reconnect_stopped" -> {
                val attempts = detail.optLong("attempts", -1L)
                val reason = detail.optString("reason", "").takeIf { it.isNotBlank() }
                state = state.copy(
                    connection = MuseWorkerConnection.DISCONNECTED,
                    attempts = if (attempts >= 0) attempts.toInt() else state.attempts,
                    stoppedReason = reason ?: state.stoppedReason,
                    nextDelaySec = null,
                    lastEventTsMs = appliedTsMs,
                )
            }
            "worker_reconnected" -> {
                val down = if (detail.has("down_seconds")) {
                    detail.optLong("down_seconds", -1L).takeIf { it >= 0 }
                } else null
                val atMs = detailTsMs(detail, "reconnected_at")
                state = state.copy(
                    connection = MuseWorkerConnection.CONNECTED,
                    disconnectedSinceMs = 0L,
                    lastAttemptMs = 0L,
                    lastAttemptNumber = 0,
                    nextDelaySec = null,
                    attempts = 0,
                    stoppedReason = null,
                    reconnectedAtMs = if (atMs > 0) atMs else if (tsMs > 0) tsMs else state.reconnectedAtMs,
                    downSeconds = down ?: state.downSeconds,
                    lastEventTsMs = appliedTsMs,
                )
            }
            else -> Unit // Unknown worker.* type: never invented.
        }
    }
    return state
}

