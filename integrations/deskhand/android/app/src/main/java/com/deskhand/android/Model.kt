package com.deskhand.android

import android.content.Context
import android.graphics.BitmapFactory
import android.os.Build
import android.util.Base64
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import org.json.JSONArray
import org.json.JSONObject

enum class Screen { Home, Pair, Viewer, Diagnostics }

internal fun computerTaskRequested(route: String, available: Boolean, selected: Boolean): Boolean =
    selected && route == "local" && available

/** Process-wide app state so a running session survives activity recreation (rotation). */
object Model {
    private var lastPcPreferences: android.content.SharedPreferences? = null
    private lateinit var store: PcStore

    var pcs by mutableStateOf(listOf<PairedPc>())
    // B2-G B5 (PcStore): set when the paired-PC file could not be read.
    // The raw file is preserved; the Home screen shows this instead of
    // silently showing "No PC paired yet".
    var pcsLoadError by mutableStateOf<String?>(null)
    // 100.015 (§11.3): set when load() dropped corrupt paired-PC records.
    var pcsCorruptNote by mutableStateOf<String?>(null)
    var screen by mutableStateOf(Screen.Home)

    var pairSerial by mutableStateOf("")
    var pairRelay by mutableStateOf("")
    var pairStatus by mutableStateOf("")
    var pairCode by mutableStateOf("")
    var pairBusy by mutableStateOf(false)

    // 100.012 H11: validation/status text for the manual relay-URL update.
    var relayEditStatus by mutableStateOf("")

    var viewerPc by mutableStateOf<PairedPc?>(null)
    var viewerStatus by mutableStateOf("")
    var viewerHost by mutableStateOf("")
    var viewerConnection by mutableStateOf("Disconnected")
    var viewerControl by mutableStateOf(false)
    // 100.015 (spec §11.4–11.5): the live transport. `transport` is the
    // transport of the current connection (null while not connected);
    // `tryingTransport` is the candidate currently being dialled;
    // `lastTransport` (+ `lastTransportPcId`) is the last-known transport,
    // kept across disconnects for the Home chip. `transportAllFailed` is
    // true while a full-list retry round is waiting; `transportFailedCount`
    // is how many candidates failed in that round (drives the wording).
    // Setters are internal (not private) so unit tests can drive the
    // derived text — same precedent as localAiTasksAvailable.
    var transport by mutableStateOf<TransportKind?>(null)
        internal set
    var tryingTransport by mutableStateOf<TransportKind?>(null)
        internal set
    var lastTransport by mutableStateOf<TransportKind?>(null)
        internal set
    var lastTransportPcId by mutableStateOf<String?>(null)
        internal set
    var transportAllFailed by mutableStateOf(false)
        internal set
    var transportFailedCount by mutableStateOf(0)
        internal set
    /** The indicator line (§11.5): "Trying Tailscale…", "Connected via Tunnel", "No transport — all three failed. Retrying…", or "" when idle. */
    val transportText: String
        get() = when {
            transport != null && viewerConnection == "Connected" -> transport!!.connectedLabel()
            transport != null -> transport!!.tryingLabel()
            tryingTransport != null -> tryingTransport!!.tryingLabel()
            transportAllFailed -> noTransportLabel(transportFailedCount)
            else -> ""
        }
    var frame by mutableStateOf<ImageBitmap?>(null)
    // 100.011: per-PC zoom memory (spec §8) + live data-saver zoom cap (spec §9).
    // zoomCap is ZOOM_MAX while no data saver exists; the saver sets it (1.25x
    // in Low) and the cap never writes to storage. contrastEnhance is the
    // §6.1 sharpen/contrast pass, default ON.
    private lateinit var zoomMemory: ZoomMemory
    var zoomCap by mutableStateOf(ZOOM_MAX)
        private set
    var contrastEnhance by mutableStateOf(true)
        private set
    var chatRoutes by mutableStateOf(listOf<JSONObject>())
    var chatTurns by mutableStateOf(listOf<JSONObject>())
    var pendingApprovals by mutableStateOf(listOf<JSONObject>())
        private set
    // 100.016 (E14 diagnostics): the last health snapshot served by the
    // PC-side `health` op, carried on the existing 2.5 s Viewer poll
    // (pollHealth, same loop as pollApprovals) — no second poll loop.
    // Null until the first snapshot arrives; a stale snapshot stays
    // visible with the offline banner, never as a fresh-looking board.
    // The setters are internal (not private) so unit tests can drive the
    // derived text — same precedent as transport / lastTransport above.
    var healthSnapshot by mutableStateOf<HealthSnapshot?>(null)
        internal set
    // 100.016 (E14): the last scrubbed export bundle received over the
    // session from a `diagnostics_export` reply (Worker A decision,
    // PATCH-REPORT-A §0: a separate op serves the export; the `health`
    // response does not carry it). Null until one arrives. The scrub
    // pass runs on the PC in the service before anything crosses the
    // session; the phone never holds an unscrubbed tail.
    var diagnosticsExportText by mutableStateOf<String?>(null)
        internal set
    var diagnosticsExportPending by mutableStateOf(false)
        internal set
    var diagnosticsExportError by mutableStateOf<String?>(null)
        internal set
    // Bumped when a requested bundle lands, so the Diagnostics screen
    // can open the share sheet exactly once per request.
    var diagnosticsExportNonce by mutableStateOf(0L)
        internal set
    // 100.017 (C13 push + 100.018 drawer fetch, absorbed §0.10): the drawer
    // feed state. Model owns it (design §B1) — the Activity reads it and
    // drops its local vars. A failed poll keeps the last good snapshot
    // (never blanks the drawer); the setters are internal so unit tests
    // can drive the derived text, same precedent as healthSnapshot.
    var agentsSnapshot by mutableStateOf(AgentsSnapshot.EMPTY)
        internal set
    var agentsLoading by mutableStateOf(false)
        internal set
    // 100.033 (message board, spec §7): board posts derived from the
    // SAME runtime_stream window as the Agents tab — one fetch, both
    // tabs (fetchBoardPosts() is fetchAgentsSnapshot(); no second fetch).
    // A failed poll keeps the last good posts (never blanks the board);
    // a resynced response replaces the list wholesale (spec §5.2);
    // otherwise new posts merge in by id (spec §9.7, mergeBoardPosts).
    var boardPosts by mutableStateOf<List<BoardPost>>(emptyList())
        internal set
    var boardUpdatedAtMs by mutableStateOf(0L)
        internal set
    // 100.037 (E12 watchdog, spec §7.4): the Muse-worker state derived
    // from `worker.*` stream events in the same runtime_stream window
    // as the Agents tab and the board — one fetch, three derivations.
    // UNKNOWN until the first worker.* event arrives; a failed poll
    // keeps the last good state (never blanks the line); the parser
    // folds new events over this state, so events scrolling out of the
    // window do not erase it. This is the Muse worker, NOT the relay:
    // the relay line above it in the drawer reads healthSnapshot.
    var museWorkerState by mutableStateOf(MuseWorkerState())
        internal set
    // 100.033: new-post dot — the newest post ts the user has seen with
    // the Board tab open, persisted in the "message-board" prefs (the
    // same named-prefs pattern as voicePrefs). 0 = never opened.
    var boardLastSeenTsMs by mutableStateOf(0L)
        internal set
    private var boardPrefs: android.content.SharedPreferences? = null
    private var pendingAgentsRpc: String? = null
    // Push transport (spec §6.1/§6.2): the drawer footer reads
    // streamTransport (Compose state) + the two timestamps below.
    // streamLastFrameMs = last stream.* frame of any kind; a frame
    // within 3 s on a live socket is push live. streamLastDeliveryMs =
    // last applied push batch or successful poll fetch; nothing delivered
    // for 6 s is STALE. Both start at 0 ("never") — the footer says so.
    var streamTransport by mutableStateOf(StreamTransport.POLLING)
        internal set
    internal var streamLastFrameMs: Long = 0L
    internal var streamLastDeliveryMs: Long = 0L
    // Last ack sent. The contiguous frontier itself lives in streamBuffer.
    internal var streamAckedSeq: Long = 0L
    internal var streamSubscribed: Boolean = false
    internal val streamBuffer = StreamEventBuffer()
    // The push-appended event window (capped at 100, newest-last). A poll
    // fetch replaces it wholesale with the op's window (rebase); push
    // frames append to it. The snapshot is rebuilt from it after every
    // applied frame, so push and poll render through the same parser.
    internal var streamEvents: List<JSONObject> = emptyList()
    internal var streamSources: JSONObject? = null
    // 100.016 (E14): when the current session became live (onSession),
    // for the phone-local "session duration" row. Null while no session
    // is live. Cleared on disconnect / session loss / BYE.
    var sessionConnectedAtMs by mutableStateOf<Long?>(null)
        internal set
    var assistantTasks by mutableStateOf(listOf<JSONObject>())
        private set
    var assistantActions by mutableStateOf(listOf<JSONObject>())
        private set
    private var pendingApprovalPoll: String? = null
    // 100.016 (E14 diagnostics): in-flight `health` / `diagnostics_export`
    // rpc ids. Same guard shape as pendingApprovalPoll; a timed-out health
    // poll just clears the guard (never a reconnect — health is advisory).
    private var pendingHealthPoll: String? = null
    private var pendingExportRpc: String? = null
    // B2-G low (B15): consecutive unanswered approval polls. A slow PC
    // used to force a full reconnect after a single 10 s timeout, so a
    // PC consistently slower than 10 s reconnected in a loop and approvals
    // never resolved. Three consecutive timeouts now force the reconnect.
    private var approvalPollTimeouts = 0
    private const val MAX_APPROVAL_POLL_TIMEOUTS = 3
    var chatStatus by mutableStateOf("Connecting assistant chat…")
    var chatRouteIndex by mutableStateOf(0)
    var chatEffort by mutableStateOf("")
    var chatRouteMode by mutableStateOf("Auto")
    var chatRouteReason by mutableStateOf("")
    private var pendingRouteSuggestRpc: String? = null
    private var pendingAutoSubmitText: String? = null
    private var pendingAutoSubmitSend: ((JSONObject) -> String?)? = null
    private var routerSettingsLoaded = false
    var chatDraft by mutableStateOf("")
    var localAiTasksAvailable by mutableStateOf(false)
        internal set
    var computerTaskMode by mutableStateOf(true)
        private set
    fun setAgenticMode(enabled: Boolean) {
        computerTaskMode = enabled
        lastPcPreferences?.edit()?.putBoolean("agentic-mode", enabled)?.apply()
    }
    fun applyChatRouteMode(mode: String) {
        if (mode !in listOf("Auto", "Fast", "Smart")) return
        chatRouteMode = mode
        if (mode != "Auto") chatRouteReason = ""
        // Batch 2 router: Fast/Smart persist the always_smart flag
        // server-side (Smart = true, Fast/Auto = false). Best-effort: a
        // failed push leaves the local mode, and the next submit still
        // works with the selected model.
        if (chatConnected) chatRequest(JSONObject().put("op", "router_settings").put("always_smart", mode == "Smart"))
    }
    private var chooseDefaultAssistant = true
    internal val desktopChat = DesktopChatState()
    // 100.036 Quick Reply Mode: voice toggle for spoken AI replies.
    // Default OFF — replies are text-only. The speaker (and its TTS
    // engine) is created lazily on first enable, never while off.
    var voiceReplies by mutableStateOf(false)
        private set
    private var appContext: Context? = null
    private var voicePrefs: android.content.SharedPreferences? = null
    private var voiceSpeaker: VoiceSpeaker? = null
    fun applyVoiceReplies(enabled: Boolean) {
        voiceReplies = enabled
        voicePrefs?.let { setVoiceRepliesEnabled(it, enabled) }
        val ctx = appContext ?: return
        if (enabled) {
            val speaker = voiceSpeaker ?: VoiceSpeaker(ctx).also { voiceSpeaker = it }
            speaker.enable()
        } else {
            voiceSpeaker?.disable()
        }
    }
    private fun speakReply(text: String) {
        voiceSpeaker?.speak(text)
    }
    internal var chatConnected by mutableStateOf(false)
    private var pendingChatRpc by mutableStateOf<String?>(null)
    var chatDeliveryUncertain by mutableStateOf(false)
        private set
    val chatSending: Boolean get() = pendingChatCommand != null
    private var submittedTurnId by mutableStateOf<String?>(null)
    var chatStopPending by mutableStateOf(false)
        private set
    var restartPending by mutableStateOf(false)
        private set
    var chatStopStatus by mutableStateOf<String?>(null)
        private set
    // 100.025 (S4/S13): agent-only stop — interrupts the running agent
    // turn and cancels queued agent turns, without touching chat
    // admissions. The service's `status` string is stored verbatim;
    // it is authoritative for the wording, including the
    // uncancellable-adapter wording ("STOP will prevent the next
    // action; the running one finishes"). Local + fallback adapters
    // have no cancel path, Muse stops only the wait loop, Codex is
    // cooperative — never claim STOP kills running work.
    var agentStopStatus by mutableStateOf<String?>(null)
        private set
    var agentStopPending by mutableStateOf(false)
        private set
    private var pendingAgentStopRpc: String? = null
    private var chatStopEpoch: Long? = null
    private var pendingStopRpc: String? = null
    private var pendingRestartRpc: String? = null
    private var autoResumeEpoch: Long? = null
    val chatActive: Boolean get() = chatSending || submittedTurnId != null || chatStopPending || chatTurns.any { it.optString("status") in setOf("queued", "dispatching") }
    val canSendChat: Boolean get() = chatConnected && !chatActive && chatStopStatus == null && chatDraft.isNotBlank() && chatRoutes.getOrNull(chatRouteIndex) != null
    // 100.016 (E14): the session the Diagnostics board reads from is
    // live, so the health snapshot is fresh. Anything else is the offline
    // state: last snapshot + banner, never a fresh-looking board.
    val diagnosticsSessionLive: Boolean get() = viewerConnection == "Connected" && chatConnected
    private var pendingChatText: String? = null
    private var pendingChatCommand by mutableStateOf<JSONObject?>(null)

    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private var viewerGeneration = 0
    private var videoDecoder: AdaptiveVideoDecoder? = null
    private var adaptiveVideo = false
    private var viewerInfoReceived = false
    var viewerAspect by mutableStateOf(16f / 9f)
    private var viewerLandscape = false
    fun setViewerLandscape(landscape: Boolean) {
        viewerLandscape = landscape
        if (screen == Screen.Viewer && viewerConnection == "Connected" && viewerInfoReceived) sendViewerStart()
    }
    private fun sendViewerStart() {
        val orientation: Byte = if (viewerLandscape) 1 else 0
        viewLink?.send(Msg.START, if (adaptiveVideo) byteArrayOf(orientation, 1) else byteArrayOf(orientation))
    }
    val desktopBrowser = DesktopBrowserController(
        send = { type, payload -> viewLink?.send(type, payload) ?: false },
        later = { delay, action -> main.postDelayed({ action() }, delay) }
    )

    val desktopBack = DesktopBackController(
        send = { type, payload -> viewLink?.send(type, payload) ?: false },
        later = { delay, action -> main.postDelayed({ action() }, delay) }
    )

    val foregroundWindow = ForegroundWindowController(
        send = { type, payload -> viewLink?.send(type, payload) ?: false },
        later = { delay, action -> main.postDelayed({ action() }, delay) }
    )

    private var pairLink: PcLink? = null
    private var viewLink: PcLink? = null
    private var pairSecret: ByteArray? = null
    private var pairRelayUrl = ""
    private var pairDone = false
    // 100.015: PcLink construction behind a factory so unit tests can drive
    // Model.connect with a fake link. The production default is the real PcLink.
    internal var linkFactory: (relay: String, secret: ByteArray, events: PcLink.Events) -> PcLink =
        { relay, secret, events -> PcLink(relay, secret, events) }

    fun init(context: Context) {
        JetstreamPhone.init(context)
        if (::store.isInitialized) return
        lastPcPreferences = context.getSharedPreferences("last-connected-pc", Context.MODE_PRIVATE)
        computerTaskMode = lastPcPreferences?.getBoolean("agentic-mode", true) ?: true
        AppUpdates.init(context)
        store = PcStore(context.applicationContext)
        pcs = store.load()
        pcsLoadError = store.loadError
        // 100.015 (§11.3): a dropped corrupt record surfaces as "pair again".
        pcsCorruptNote = if (store.droppedCorruptCount > 0) "A saved PC had a corrupt address list and was removed — pair again." else null
        // 100.011: open the per-PC zoom store. A corrupt map is discarded
        // (defaults used); only the failure flag is surfaced, never the map.
        zoomMemory = ZoomMemory.open(context.applicationContext)
        val zoomStore = zoomMemory.load()
        contrastEnhance = zoomStore.contrastOn
        // 100.036: load the voice-replies toggle (default OFF). The TTS
        // engine is NOT created here — only when the toggle is on.
        appContext = context.applicationContext
        voicePrefs = context.getSharedPreferences(VOICE_REPLIES_PREFS, Context.MODE_PRIVATE)
        voiceReplies = voicePrefs?.let { voiceRepliesEnabled(it) } ?: false
        if (voiceReplies) applyVoiceReplies(true)
        // 100.033: board last-seen timestamp for the new-post dot. A
        // missing/corrupt prefs file reads as 0 (never opened) — the dot
        // then shows while any post exists, and clears on first open.
        boardPrefs = context.getSharedPreferences("message-board", Context.MODE_PRIVATE)
        boardLastSeenTsMs = boardPrefs?.getLong("last_seen_post_ts_ms", 0L) ?: 0L
        // B2-G: the zoom-store load-failed flag was written but never read
        // by any UI, so it is gone; a corrupt map is still discarded here.
    }

    // ---- 100.011 zoom memory helpers (spec §8, §9, §10) ----
    /** The stored zoom entry for one PC, keyed by PairedPc.id (never the secret). Null = new PC. */
    fun zoomEntryFor(deviceId: String): ZoomEntry? =
        if (::zoomMemory.isInitialized) zoomMemory.entryFor(deviceId) else null

    /** Global default zoom for a PC with no entry (spec §8.2). */
    fun globalDefaultZoom(): Float =
        if (::zoomMemory.isInitialized) zoomMemory.globalDefaultZoom() else ZOOM_DEFAULT

    /** Set the global default (spec §10 "Default zoom for new PCs" stepper). */
    fun setGlobalDefaultZoom(zoom: Float) {
        if (!::zoomMemory.isInitialized) return
        zoomMemory.setGlobalDefault(zoom)
    }

    /** Save the connected PC's zoom + pan centre (spec §8.4). No-op while disconnected. */
    fun saveZoomForCurrentPc(zoom: Float, panCentre: Offset, frameW: Int, frameH: Int) {
        val id = viewerPc?.id ?: return
        if (!::zoomMemory.isInitialized) return
        val entry = ZoomEntry(
            zoom = snapZoom(zoom),
            panX = panCentre.x,
            panY = panCentre.y,
            frameWidth = frameW,
            frameHeight = frameH,
            updatedAt = System.currentTimeMillis(),
        )
        zoomMemory.saveEntry(id, entry, pcs.map { it.id }.toSet())
    }

    /** Delete the connected PC's entry — "Reset zoom for this PC" (spec §10). */
    fun resetZoomForCurrentPc() {
        val id = viewerPc?.id ?: return
        if (::zoomMemory.isInitialized) zoomMemory.delete(id)
    }

    /** Delete one PC's entry — on unpair (spec §8.5). */
    fun deleteZoomForPc(deviceId: String) {
        if (::zoomMemory.isInitialized) zoomMemory.delete(deviceId)
    }

    /** Contrast/sharpen pass on/off (spec §6.1, default ON). Persisted with the zoom store. */
    fun updateContrastEnhance(on: Boolean) {
        contrastEnhance = on
        if (::zoomMemory.isInitialized) zoomMemory.setContrastOn(on)
    }

    private var autoConnected = false

    /** Opens the most recently paired PC once per app launch so streaming starts immediately. */
    fun autoConnect() {
        if (autoConnected) return
        autoConnected = true
        if (screen == Screen.Home) {
            val last = lastPcPreferences?.getString("id", null)
            (pcs.firstOrNull { it.id == last } ?: pcs.lastOrNull())?.let { connect(it) }
        }
    }

    // 100.015 (§11.2): the Tailscale / LAN candidates carried by the QR.
    // pairRelay stays the primary (r=) address shown in the Pair screen.
    private var pairTailscaleRelay: String? = null
    private var pairLanRelay: String? = null
    private var pairRelayCandidates: List<String> = emptyList()

    fun openPair(serial: String = "", relay: String = "", tailscaleRelay: String? = null, lanRelay: String? = null) {
        stopPair()
        pairSerial = serial
        // Defensive: a QR with t= but no r= still pairs via Tailscale.
        val primary = relay.ifBlank { tailscaleRelay ?: "" }
        if (primary.isNotBlank() || pairRelay.isBlank()) pairRelay = primary
        pairTailscaleRelay = tailscaleRelay
        pairLanRelay = lanRelay
        pairRelayCandidates = emptyList()
        pairStatus = ""
        pairCode = ""
        screen = Screen.Pair
    }

    fun startPair() {
        val serial = Serial.normalize(pairSerial)
        if (serial == null) {
            pairStatus = "That code isn't valid. It has 20 letters and numbers."
            return
        }
        val relay = pairRelay.trim().trimEnd('/')
        if (!(relay.startsWith("ws://") || relay.startsWith("wss://"))) {
            pairStatus = "Relay address must start with ws:// or wss://"
            return
        }
        stopPair()
        pairDone = false
        pairBusy = true
        pairCode = ""
        pairStatus = "Looking for your PC… keep the pairing window open on the PC."
        pairSecret = Serial.toSecret(serial)
        pairRelayUrl = relay
        // 100.015 (§11.2): ordered candidates [t, r, l], deduped; only
        // ws(s):// values are kept. The list is stored for later connects.
        pairRelayCandidates = validRelayCandidates(buildRelayCandidates(pairTailscaleRelay, relay, pairLanRelay))
        // 100.040: never dial a Cloudflare tunnel — a stale pre-100.040
        // QR can still carry a tunnel r=. Dial the first non-tunnel
        // candidate instead; if every candidate is a tunnel URL there
        // is nothing safe to dial — ask for a fresh pairing.
        val dialRelay = if (isTunnelUrl(relay)) {
            val nonTunnel = pairRelayCandidates.firstOrNull { !isTunnelUrl(it) }
            if (nonTunnel == null) {
                pairBusy = false
                pairStatus = "That pairing code uses an old Cloudflare address. Pair again with a new code from the PC."
                return
            }
            nonTunnel
        } else relay
        pairLink = linkFactory(dialRelay, pairSecret!!, object : PcLink.Events {
            override fun onConnection(relayUp: Boolean) {
                if (!pairDone) pairStatus = if (relayUp) "Connected to relay. Waiting for the PC…" else "Can't reach the relay. Retrying…\n${pairLink?.lastError.orEmpty()}"
            }

            override fun onSession(confirmCode: String) {
                pairCode = confirmCode
                pairStatus = "Check the PC shows the same code, then approve it there."
                val name = (Build.MODEL ?: "Phone").take(40)
                pairLink?.send(Msg.PAIR_REQUEST, JSONObject().put("name", name).toString().toByteArray())
            }

            override fun onSessionLost() {}

            override fun onMessage(type: Int, payload: ByteArray) {
                when (type) {
                    Msg.PAIR_ACCEPT -> {
                        // B2-G low (B12): guard the parse like every
                        // neighbouring handler — a malformed PAIR_ACCEPT used
                        // to throw on the socket reader thread. The host
                        // issues a 32-byte device secret (HostEngine.cs), so
                        // anything under 16 bytes is rejected, not stored.
                        val pc = runCatching {
                            val o = JSONObject(String(payload, Charsets.UTF_8))
                            val secret = Base64.decode(o.getString("secret"), Base64.NO_WRAP)
                            require(secret.size >= 16) { "Pairing secret is too short." }
                            PairedPc(o.optString("host", "My PC").ifBlank { "My PC" }, secret, o.optBoolean("control"), pairRelayCandidates.ifEmpty { listOf(pairRelayUrl) })
                        }.getOrNull()
                        if (pc == null) {
                            pairDone = true
                            pairBusy = false
                            pairStatus = "The PC sent an invalid pairing reply. Try pairing again."
                            stopPair()
                            return
                        }
                        pairDone = true
                        pcs = pcs.filter { it.id != pc.id } + pc
                        store.save(pcs)
                        pairBusy = false
                        pairStatus = "Paired with ${pc.name}."
                        pairCode = ""
                        stopPair()
                        autoConnected = true
                        connect(pc)
                    }
                    Msg.PAIR_REJECT -> {
                        pairDone = true
                        pairBusy = false
                        pairStatus = "The PC rejected this pairing."
                        stopPair()
                    }
                }
            }
        }).also { it.start() }
    }

    fun stopPair() {
        pairLink?.close()
        pairLink = null
        pairBusy = false
    }

    fun connect(pc: PairedPc) {
        if (viewerPc?.id != pc.id) {
            desktopChat.reset()
            chatTurns = emptyList()
            assistantActions = emptyList()
            chatRoutes = emptyList()
            pendingChatRpc = null
            pendingChatCommand = null
            submittedTurnId = null
            chatStopPending = false
            chatStopStatus = null
            chatStopEpoch = null
            pendingStopRpc = null
            chatDeliveryUncertain = false
            chatStatus = "Connecting assistant chat…"
            // New PC, new ring: the old frontier belongs to the old PC.
            resetStreamState()
        }
        disconnect()
        viewerPc = pc
        viewerHost = pc.name
        viewerControl = false
        localAiTasksAvailable = false
        chooseDefaultAssistant = true
        pendingApprovals = emptyList()
        assistantTasks = emptyList()
        pendingApprovalPoll = null
        approvalPollTimeouts = 0
        frame = null
        viewerConnection = "Connecting"
        viewerStatus = "Connecting to relay…"
        screen = Screen.Viewer
        // 100.015 (§11.4): ordered fallback. Candidates are tried in list
        // order; a failure before any session advances, a drop after a
        // session stays on the same transport for the first failure and
        // only falls back when the same-transport reconnect also fails
        // (a fallback decision at reconnect time — never mid-session).
        // pc.relays is never empty (PairedPc enforces it); the fallback
        // to [pc.relay] is defence in depth only.
        // 100.040 (D8): Cloudflare is removed — tunnel URLs are filtered
        // out of the candidates and never dialled.
        val candidates = pc.relays.ifEmpty { listOf(pc.relay) }.filter { it.isNotBlank() && !isTunnelUrl(it) }
        if (candidates.isEmpty()) {
            // The stored pairing was tunnel-only: there is nothing to
            // dial. Do NOT construct OrderedFallback (it requires at
            // least one candidate) — show a clear "pair again" state and
            // return without connecting. The stored pairing is left
            // untouched (D10); re-pairing produces a Tailscale/LAN QR.
            transport = null
            tryingTransport = null
            transportAllFailed = false
            transportFailedCount = 0
            viewerConnection = "Disconnected"
            viewerStatus = "This pairing only has a Cloudflare address, which this version no longer uses. Pair again from the PC to connect via Tailscale or LAN."
            return
        }
        val fallback = OrderedFallback(candidates)
        transport = null
        tryingTransport = fallback.currentKind
        lastTransportPcId = pc.id
        transportAllFailed = false
        transportFailedCount = 0
        val generation = viewerGeneration
        fun update(action: () -> Unit) { main.post { if (generation == viewerGeneration) action() } }
        var retryRound = 0
        // Declared before the attempt funs below so no body references a
        // name declared after it; assigned before the first tryCandidate().
        lateinit var events: PcLink.Events
        fun tryCandidate() {
            // Close the previous attempt's link first: PcLink retries the
            // same relay on its own timer, and close() cancels that retry
            // so a failed candidate cannot redial itself behind us.
            viewLink?.close()
            val kind = fallback.currentKind
            tryingTransport = kind
            transport = null
            viewerConnection = if (retryRound == 0) "Connecting" else "Reconnecting"
            viewerStatus = kind.tryingLabel()
            viewLink = linkFactory(fallback.current, pc.secret, events)
            viewLink?.start()
        }
        fun scheduleFullRetry() {
            // Whole-list retry with the existing backoff (§11.4): 1 s,
            // doubling, 30 s cap, plus jitter — the PcLink pattern. The
            // generation guard cancels it on disconnect / reconnect.
            retryRound++
            val base = fullRetryDelayMs(retryRound - 1)
            val wait = base + java.util.concurrent.ThreadLocalRandom.current().nextLong(0, base / 2 + 1)
            main.postDelayed({
                if (generation == viewerGeneration) {
                    fallback.restart()
                    transportAllFailed = false
                    transportFailedCount = 0
                    tryCandidate()
                }
            }, wait)
        }
        events = object : PcLink.Events {
            override fun onConnection(relayUp: Boolean) = update {
                if (relayUp) {
                    // The relay answered on this candidate: this is the
                    // live transport (the encrypted session still follows).
                    fallback.markRelayUp()
                    transport = fallback.currentKind
                    lastTransport = transport
                    lastTransportPcId = pc.id
                    tryingTransport = null
                    transportAllFailed = false
                    viewerConnection = if (viewerConnection != "Reconnecting") "Connecting" else "Reconnecting"
                    viewerStatus = "Waiting for ${pc.name}… (is Deskhand running on the PC and not paused?)"
                    return@update
                }
                // Relay unreachable. Capture the reason before advancing:
                // tryCandidate() swaps viewLink.
                val lastErr = viewLink?.lastError.orEmpty()
                val next = fallback.advanceAfterFailure()
                if (next != null) {
                    tryCandidate()
                    return@update
                }
                if (fallback.shouldStayAfterFailure()) {
                    // A live session dropped: stay on this transport —
                    // PcLink retries the same relay itself. No hopping.
                    transport = null
                    viewerConnection = "Reconnecting"
                    viewerStatus = "Lost the PC. Reconnecting…"
                    return@update
                }
                // Every candidate failed: the whole list retries with the
                // existing backoff (§11.4). A one-item list keeps today's
                // wording (§11.6); a multi-candidate list says "No transport".
                transport = null
                tryingTransport = null
                transportAllFailed = candidates.size > 1
                transportFailedCount = candidates.size
                viewerConnection = "Reconnecting"
                viewerStatus = if (candidates.size > 1) noTransportLabel(candidates.size)
                    else "Can't reach the relay. Retrying…\n$lastErr"
                scheduleFullRetry()
            }

            override fun onSession(confirmCode: String) = update {
                fallback.markSessionLive()
                transport = fallback.currentKind
                lastTransport = transport
                lastTransportPcId = pc.id
                tryingTransport = null
                transportAllFailed = false
                desktopChat.reconnect()
                desktopBrowser.sessionLost(); desktopBack.sessionLost(); foregroundWindow.sessionLost()
                viewerConnection = "Connected"
                viewerStatus = "Secure link up. Waiting for the first frame…"
                // 100.016 (E14): a (re)connect starts a new session, so the
                // session clock restarts here too.
                sessionConnectedAtMs = System.currentTimeMillis()
                // 100.012 H9: re-read ground truth on every (re)connect so a
                // banner left over from a previous session is reconciled
                // against the server's current admission state instead of
                // trusting the phone's stale cache. (INFO also requests
                // history; this duplicate read is idempotent.)
                chatRequest(JSONObject().put("op", "history"))
            }

            override fun onSessionLost() = update {
                fallback.markSessionLost()
                // The live transport is gone; the last-known one stays for
                // the Home chip. PcLink retries this same relay itself.
                transport = null
                tryingTransport = null
                viewerInfoReceived = false
                // 100.016 (E14): the session is gone, so its clock stops.
                // The last health snapshot is KEPT — the Diagnostics screen
                // shows it with the offline banner, never a blank board.
                // The in-flight health poll is cleared so the next session's
                // first poll is not blocked by a stale guard.
                sessionConnectedAtMs = null
                pendingHealthPoll = null
                resetStreamSession()
                videoDecoder?.close(); videoDecoder = null
                AppUpdates.disconnected()
                markChatDeliveryUncertain()
                desktopChat.reconnect()
                desktopBrowser.sessionLost(); desktopBack.sessionLost(); foregroundWindow.sessionLost()
                viewerControl = false
                localAiTasksAvailable = false
                chooseDefaultAssistant = true
                pendingApprovals = emptyList()
                assistantTasks = emptyList()
                pendingApprovalPoll = null
                approvalPollTimeouts = 0
                autoResumeEpoch = null
                chatConnected = false
                viewerConnection = "Reconnecting"
                chatStatus = "Connection lost; chat will reconnect with the PC"
                viewerStatus = "Lost the PC. Reconnecting…"
                frame = null
            }

            override fun onMessage(type: Int, payload: ByteArray) {
                if (type == Msg.VIDEO_FRAME) {
                    update {
                        if (videoDecoder == null) {
                            videoDecoder = AdaptiveVideoDecoder(
                                display = { epoch, bitmap -> update {
                                    if (videoDecoder?.isCurrent(epoch) == true) {
                                        frame = bitmap.asImageBitmap(); viewerStatus = ""
                                    } else bitmap.recycle()
                                } },
                                acknowledge = { receipt -> update { viewLink?.send(Msg.VIDEO_ACK, receipt) } },
                                failure = { update { viewLink?.send(Msg.VIDEO_FAILED) } }
                            )
                        }
                        videoDecoder?.submit(payload)
                    }
                    return
                }
                // Keep JPEG decoding on the socket callback thread, as in the existing viewer.
                val decoded = if (type == Msg.FRAME) BitmapFactory.decodeByteArray(payload, 0, payload.size) else null
                update {
                when (type) {
                    Msg.UPDATE_INFO, Msg.UPDATE_CHUNK, Msg.UPDATE_ERROR -> AppUpdates.message(type, payload)
                    Msg.INFO -> {
                        lastPcPreferences?.edit()?.putString("id", pc.id)?.apply()
                        chatConnected = true
                        viewerConnection = "Connected"
                        // 100.016 (E14): INFO is the other "Connected" path;
                        // only start the clock here if onSession did not
                        // already do it (a reconnect restarts it there).
                        if (sessionConnectedAtMs == null) sessionConnectedAtMs = System.currentTimeMillis()
                        val o = runCatching { JSONObject(String(payload, Charsets.UTF_8)) }.getOrNull()
                        adaptiveVideo = Build.VERSION.SDK_INT >= 29 && o?.optInt("adaptiveVideo", 0) == 1
                        viewerInfoReceived = true
                        viewerHost = o?.optString("host", pc.name) ?: pc.name
                        viewerControl = o?.optBoolean("control") ?: false
                        localAiTasksAvailable = o?.optBoolean("localAiTasks") ?: false
                        val w = o?.optInt("w", 16) ?: 16
                        val h = o?.optInt("h", 9) ?: 9
                        viewerAspect = if (w > 0 && h > 0) w.toFloat() / h else 16f / 9f
                        desktopBrowser.info((o?.optInt("desktopBrowser", 0) ?: 0) >= 1, viewerControl,
                            (o?.optInt("fileBrowser", 0) ?: 0) >= 2, (o?.optInt("fileBrowser", 0) ?: 0) >= 3)
                        desktopBack.info(o?.optInt("desktopBack", 0) == 1, viewerControl)
                        foregroundWindow.info(o?.optInt("foregroundWindow", 0) == 1, viewerControl)
                        if (o?.optInt("androidUpdates", 0) == 1) {
                            viewLink?.let { link -> AppUpdates.connected { type, payload -> link.send(type, payload) } }
                        } else AppUpdates.unsupported()
                        sendViewerStart()
                        chatRequest(JSONObject().put("op", "catalog"))
                        chatRequest(JSONObject().put("op", "history"))
                    }
                    Msg.TEXT_FOCUS -> {
                        val result = runCatching { JSONObject(String(payload, Charsets.UTF_8)) }.getOrNull()
                        if (viewerControl && viewerConnection == "Connected" && result?.optLong("id") == focusCheckId && result.optBoolean("editable")) remoteKeyboardRequest++
                    }
                    Msg.FRAME -> {
                        videoDecoder?.showStill()
                        val bmp = decoded
                        if (bmp != null) {
                            frame = bmp.asImageBitmap()
                            viewerStatus = ""
                        }
                        viewLink?.send(Msg.ACK)
                    }
                    Msg.DESKTOP_LIST_RESULT, Msg.DESKTOP_OPEN_RESULT -> desktopBrowser.message(type, payload)
                    Msg.DESKTOP_BACK_RESULT -> desktopBack.message(payload)
                    Msg.FOREGROUND_WINDOW_RESULT -> {
                        foregroundWindow.message(payload)
                    }
                    Msg.BYE -> {
                        AppUpdates.disconnected()
                        markChatDeliveryUncertain()
                        desktopChat.reconnect()
                desktopBrowser.sessionLost(); desktopBack.sessionLost(); foregroundWindow.sessionLost()
                        viewerControl = false
                        // 100.015: the session ended, so no transport is live.
                        transport = null
                        // 100.016 (E14): the session clock stops; the last
                        // health snapshot is kept for the offline banner.
                        sessionConnectedAtMs = null
                        pendingHealthPoll = null
                        resetStreamSession()
                        chatConnected = false
                        viewerConnection = "Session ended"
                        chatStatus = "The PC ended the session"
                        viewerStatus = "The PC ended the session."
                        frame = null
                    }
                    Msg.CHAT_RESULT -> {
                        val response = runCatching { JSONObject(String(payload, Charsets.UTF_8)) }.getOrNull() ?: return@update
                        handleChatResponse(response)
                    }
                    // 100.017 (C13 push, spec §3.3): sealed stream frames.
                    // Display only — an event is a display row, never an
                    // action (§7.2). handleStreamFrame sends the subscribe /
                    // ack replies itself.
                    Msg.STREAM -> handleStreamFrame(payload)
                }
            }
            }
        }
        // 100.015: the first attempt starts here (tryCandidate owns the
        // link from now on — one PcLink per candidate, in list order).
        tryCandidate()
    }

    internal fun handleChatResponse(response: JSONObject) {
        if (response.optString("op").startsWith("jetstream_")) { JetstreamPhone.receive(response); return }
        if (!response.optBoolean("ok")) {
            // 100.017 (100.018 design §B2): a failed drawer fetch clears
            // its guard and keeps the last good snapshot — and never
            // touches chatStatus (that is the chat's status line). The
            // drawer signals the failure itself: updatedAtMs stops
            // advancing and the >120 s stale warning fires.
            if (response.optString("op") == "runtime_stream") {
                if (response.optString("rpc") == pendingAgentsRpc) {
                    pendingAgentsRpc = null
                    agentsLoading = false
                }
                return
            }
            chatStatus = response.optString("error", "Chat request failed")
            if (response.optString("op") == "resume_chat") {
                autoResumeEpoch = null
                main.postDelayed({ if (chatConnected && chatStopStatus != null) chatRequest(JSONObject().put("op", "history")) }, 2500)
            }
            if (response.optString("op") == "approval_list" && response.optString("rpc") == pendingApprovalPoll) {
                pendingApprovalPoll = null
                approvalPollTimeouts = 0
                pendingApprovals = emptyList()
            }
            // 100.016 (E14): a failed health poll just clears the guard —
            // the next 2.5 s loop iteration retries, and the last snapshot
            // stays visible (with the offline banner if the session is
            // down). A failed export request surfaces the PC's error and
            // leaves the Share button usable for a retry.
            if (response.optString("op") == "health" && response.optString("rpc") == pendingHealthPoll) {
                pendingHealthPoll = null
            }
            if (response.optString("op") == "diagnostics_export" && response.optString("rpc") == pendingExportRpc) {
                pendingExportRpc = null
                diagnosticsExportPending = false
                diagnosticsExportError = response.optString("error", "Export request failed")
            }
            if (response.optString("op") == "route_suggest" && response.optString("rpc") == pendingRouteSuggestRpc) {
                // Suggestion failed: fall back to the selected model.
                pendingRouteSuggestRpc = null
                val text = pendingAutoSubmitText
                pendingAutoSubmitText = null
                val send = pendingAutoSubmitSend ?: ::chatRequest
                pendingAutoSubmitSend = null
                if (text != null) submitWithRoute(chatRoutes.getOrNull(chatRouteIndex), text, send)
            }
            if (response.optString("op") == "restart_chat" && response.optString("rpc") == pendingRestartRpc) {
                restartPending = false
                pendingRestartRpc = null
                chatStatus = "Restart unconfirmed: " + chatStatus
            }
            if (response.optString("op") == "stop_chat" && response.optString("rpc") == pendingStopRpc) {
                chatStopPending = false
                pendingStopRpc = null
                chatStopStatus = "Stop unconfirmed: " + chatStatus
            }
            if (response.optString("op") == "stop_agent_work" && response.optString("rpc") == pendingAgentStopRpc) {
                agentStopPending = false
                pendingAgentStopRpc = null
                agentStopStatus = "Agent stop unconfirmed: " + chatStatus
            }
            // 100.020 (C07 fail-fast): a gate refusal lands here — the
            // service returns {ok:false, error:<§5 refusal text>} for the
            // submit, and the C# side forwards it verbatim with the same
            // rpc. Show the server wording as-is (chatStatus + the
            // rejected bubble) and keep the draft: a refusal is not an
            // acknowledgement, so chatDraft is never cleared on this path
            // (the "submit" ack branch below clears it only for an
            // accepted turn). No §5 wording is hardcoded here — the text
            // shown is whatever the server sent.
            if (response.optString("rpc") == pendingChatRpc) {
                pendingChatCommand?.optString("id")?.let { desktopChat.rejected(it, chatStatus) { reply -> speakReply(reply.text) } }
                pendingChatRpc = null
                pendingChatCommand = null
                submittedTurnId = null
            }
            return
        }
        val result = response.optJSONObject("result") ?: return
        when (response.optString("op")) {
            "catalog" -> {
                val oldRoute = chatRoutes.getOrNull(chatRouteIndex)
                val oldEffort = chatEffort
                val routes = result.optJSONArray("routes")
                chatRoutes = if (routes != null) (0 until routes.length()).map { routes.getJSONObject(it) } else emptyList()
                val previous = chatRoutes.indexOfFirst { it.optString("assistant") == oldRoute?.optString("assistant") && it.optString("model") == oldRoute?.optString("model") && it.optString("account") == oldRoute?.optString("account") }
                val useDefault = chooseDefaultAssistant || previous < 0
                chatRouteIndex = if (!useDefault && previous >= 0) previous else 0
                chatEffort = if (!useDefault && previous >= 0) oldEffort else chatRoutes.getOrNull(chatRouteIndex)?.optString("effort") ?: ""
                chooseDefaultAssistant = chatRoutes.isEmpty()
                chatStatus = if (chatDeliveryUncertain) "Delivery unconfirmed; check History before sending again." else if (result.isNull("error")) "Ready" else result.optString("error")
                // Batch 2: fetch the persisted router setting once so the
                // selector reflects it (Smart when always_smart is on).
                if (!routerSettingsLoaded) {
                    routerSettingsLoaded = true
                    chatRequest(JSONObject().put("op", "router_settings"))
                }
            }
            "approval_list" -> {
                if (response.optString("rpc") != pendingApprovalPoll) return
                pendingApprovalPoll = null
                approvalPollTimeouts = 0
                val values = result.optJSONArray("approvals")
                pendingApprovals = if (values != null) (0 until values.length()).map { values.getJSONObject(it) } else emptyList()
                val tasks = result.optJSONArray("tasks")
                assistantTasks = if (tasks != null) (0 until tasks.length()).map { tasks.getJSONObject(it) } else emptyList()
            }
            "approval_resolve" -> { pollApprovals() }
            "history" -> {
                val actionHistory = result.optJSONArray("assistantActions")
                assistantActions = if (actionHistory != null) (0 until actionHistory.length()).map { actionHistory.getJSONObject(it) } else emptyList()
                val admission = result.optJSONObject("chatAdmission")
                val stopEpoch = chatStopEpoch
                if (!chatStopPending && admission != null) {
                    val epoch = admission.optLong("epoch", -1)
                    if (admission.optBoolean("stopped", false) && epoch >= 0 &&
                        (stopEpoch == null || epoch >= stopEpoch)) {
                        chatStopStatus = "Stop unconfirmed. Resume chat in assistant settings."
                        chatStopEpoch = epoch
                    } else if (!admission.optBoolean("stopped", true) && stopEpoch != null && epoch >= stopEpoch) {
                        // 100.012 H9: equality clears too. Every server-side stop bumps
                        // the epoch (deskhand_dispatch request_chat_stop /
                        // cancel_unstarted / stop all do admission_epoch += 1), so a
                        // stale pre-stop stopped=false admission always carries a
                        // strictly smaller epoch and can never satisfy >=.
                        chatStopStatus = null
                        chatStopEpoch = null
                    }
                }
                val turns = result.optJSONArray("turns")
                chatTurns = if (turns != null) (0 until turns.length()).map { turns.getJSONObject(it) } else emptyList()
                submittedTurnId?.let { id ->
                    val observed = chatTurns.firstOrNull { it.optString("id") == id }
                    if (observed != null && observed.optString("status") !in setOf("queued", "dispatching")) submittedTurnId = null
                }
                desktopChat.snapshot(chatTurns) { reply -> speakReply(reply.text) }
                if (!chatStopPending && !restartPending && admission?.optBoolean("stopped", false) == true &&
                    chatTurns.none { it.optString("status") in setOf("queued", "dispatching") }) {
                    val epoch = admission.optLong("epoch", -1)
                    if (epoch >= 0 && autoResumeEpoch != epoch && chatConnected && viewLink != null) {
                        autoResumeEpoch = epoch
                        if (chatRequest(JSONObject().put("op", "resume_chat")) == null) autoResumeEpoch = null
                    }
                }
                val pendingId = pendingChatCommand?.optString("id")
                if (chatDeliveryUncertain && pendingId != null && chatTurns.any { it.optString("id") == pendingId }) {
                    if (chatDraft == pendingChatText) chatDraft = ""
                    pendingChatCommand = null
                    pendingChatRpc = null
                    chatDeliveryUncertain = false
                    chatStatus = "Message received"
                }
            }
            "health" -> {
                // Any ok health reply is accepted — even one that does not
                // match the in-flight poll (a snapshot pushed with another
                // op is still fresh data; it carries its own as_of). A
                // matching reply clears the guard so the next loop
                // iteration polls again. The snapshot object is always
                // replaced, so the Diagnostics screen's ages re-render on
                // every poll tick.
                // Worker A decision (PATCH-REPORT-A §0, read 2026-11-10):
                // the health response does NOT carry the export bundle —
                // a separate `diagnostics_export` op serves it, so there
                // is no bundle field to read here.
                if (response.optString("rpc") == pendingHealthPoll) pendingHealthPoll = null
                parseHealthSnapshot(result)?.let { healthSnapshot = it }
            }
            "runtime_stream" -> {
                // 100.017 (100.018 design §B2): the drawer fetch reply.
                // Only a reply to our own in-flight fetch counts; a stale
                // or duplicate reply is ignored. On ok the parser rebuilds
                // the running/finished set from the FULL window (no `since`
                // was sent — spawn/finish pairing needs the whole window),
                // the push cursor re-bases on the polled latest_seq (V1:
                // seq is window-relative, re-numbered every call), and the
                // fetch counts as a delivery for the fallback state machine.
                if (response.optString("rpc") != pendingAgentsRpc) return
                pendingAgentsRpc = null
                agentsLoading = false
                val fetchedAtMs = System.currentTimeMillis()
                agentsSnapshot = agentsFromRuntimeEvents(result.toString(), fetchedAtMs)
                // 100.033 (message board): the same reply derives the
                // board posts — no second fetch. A resynced response
                // replaces the list wholesale (seq re-numbered under us,
                // spec §5.2); otherwise posts merge in by id, so history
                // survives events scrolling out of the window.
                boardPosts = boardPostsFromRuntimeEvents(result.toString(), fetchedAtMs).let { derived ->
                    if (result.optBoolean("resynced", false)) derived
                    else mergeBoardPosts(boardPosts, derived)
                }
                // 100.037 (E12 watchdog): the same reply derives the
                // Muse-worker state from worker.* events — no second
                // fetch. The parser folds over the previous state, so a
                // window with no worker.* events leaves it unchanged.
                museWorkerState = workerStateFromRuntimeEvents(result.toString(), fetchedAtMs, museWorkerState)
                boardUpdatedAtMs = fetchedAtMs
                streamLastDeliveryMs = fetchedAtMs
                rebaseStreamWindow(result, fetchedAtMs)
                refreshStreamTransport(fetchedAtMs)
            }
            "diagnostics_export" -> {
                // Only a reply to our own request counts; a stale or
                // duplicate reply is ignored. The expected shape is
                // result {"text": "<scrubbed bundle>"} — the scrub pass
                // runs on the PC in the service before anything crosses
                // the session, so the phone never holds an unscrubbed tail.
                if (response.optString("rpc") != pendingExportRpc) return
                pendingExportRpc = null
                diagnosticsExportPending = false
                val text = result.optString("text")
                if (text.isNotEmpty()) {
                    diagnosticsExportText = text
                    diagnosticsExportError = null
                    diagnosticsExportNonce++
                } else {
                    diagnosticsExportError = "The PC returned no diagnostics text"
                }
            }
            "route_suggest" -> {
                if (response.optString("rpc") != pendingRouteSuggestRpc) return
                pendingRouteSuggestRpc = null
                val text = pendingAutoSubmitText ?: return
                pendingAutoSubmitText = null
                val send = pendingAutoSubmitSend ?: ::chatRequest
                pendingAutoSubmitSend = null
                val picked = result.optJSONObject("route")
                if (picked == null) {
                    // No route suggested: fall back to the selected model.
                    submitWithRoute(chatRoutes.getOrNull(chatRouteIndex), text, send)
                    return
                }
                val kind = result.optString("kind", "")
                val reason = result.optString("reason", "")
                chatRouteReason = "Auto \u2192 $kind: $reason"
                submitWithRoute(picked, text, send, picked.optString("effort"))
            }
            "router_settings" -> {
                if (result.has("always_smart")) {
                    chatRouteMode = if (result.optBoolean("always_smart")) "Smart" else "Auto"
                }
            }
            "submit" -> {
                if (response.optString("rpc") == pendingChatRpc) {
                    pendingChatCommand?.optString("id")?.let { desktopChat.acknowledged(it) }
                    if (chatDraft == pendingChatText) chatDraft = ""
                    pendingChatRpc = null
                    pendingChatCommand = null
                    chatStatus = "Queued"
                }
            }
            "stop_chat" -> {
                if (response.optString("rpc") != pendingStopRpc || pendingStopRpc == null) return
                chatStopPending = false
                pendingStopRpc = null
                chatStopStatus = result.optString("status", "Stop unconfirmed")
                // 100.012 H9: keep the previous epoch when the result carries no
                // admission_epoch. Wiping it to null made the history epoch
                // comparison unable to ever clear the banner again.
                if (result.has("admission_epoch")) chatStopEpoch = result.optLong("admission_epoch")
                chatStatus = chatStopStatus!!
                if (viewLink != null) chatRequest(JSONObject().put("op", "history"))
            }
            // 100.025 (S4/S13): the service is authoritative for the
            // wording — store its `status` string verbatim. Never
            // rewrite it into a "stopped" claim: on Local / fallback
            // the running action finishes, on Muse only the wait
            // loop stops, on Codex the interrupt is cooperative.
            "stop_agent_work" -> {
                if (response.optString("rpc") != pendingAgentStopRpc || pendingAgentStopRpc == null) return
                agentStopPending = false
                pendingAgentStopRpc = null
                agentStopStatus = result.optString("status", "Agent stop unconfirmed")
            }
            "restart_chat" -> {
                restartPending = false
                pendingRestartRpc = null
                desktopChat.reset()
                chatTurns = emptyList()
                assistantActions = emptyList()
                pendingChatCommand = null
                submittedTurnId = null
                pendingChatRpc = null
                chatStopPending = false
                chatStopStatus = null
                agentStopPending = false
                pendingAgentStopRpc = null
                agentStopStatus = null
                chatStopEpoch = null
                chatDeliveryUncertain = false
                autoResumeEpoch = null
                chatDraft = ""
                chatStatus = "Chat restarted"
            }
            "resume_chat" -> {
                // 100.012 H9: a successful resume is authoritative — the
                // server-side latch is clear. Clear the banner directly
                // instead of waiting for the follow-up history to satisfy
                // the epoch comparison (which may never happen).
                chatStopStatus = null
                chatStopEpoch = null
                chatStatus = "Chat resumed"
                chatRequest(JSONObject().put("op", "history"))
            }
        }
    }

    internal fun markChatDeliveryUncertain() {
        if (pendingChatCommand != null) {
            pendingChatRpc = null
            chatDeliveryUncertain = true
            chatStatus = "Delivery unconfirmed; check History before sending again."
        }
    }

    // B2-G: unlockUncertainChat() and resetChatTracking() removed — they had
    // no production caller (test-only). The only in-app recovery from an
    // uncertain delivery is the automatic history-reconciliation path in
    // handleChatResponse ("history"); there is no manual unlock affordance.

    fun disconnect() {
        // 100.015: a disconnect ends the live transport (the last-known
        // one survives in lastTransport for the Home chip).
        transport = null
        tryingTransport = null
        transportAllFailed = false
        transportFailedCount = 0
        viewerInfoReceived = false
        // 100.016 (E14): the session clock stops; the in-flight health
        // poll is cleared so the next session's first poll is not
        // blocked. The last health snapshot is KEPT for the offline
        // banner — disconnect() never blanks the Diagnostics board.
        sessionConnectedAtMs = null
        pendingHealthPoll = null
        resetStreamSession()
        adaptiveVideo = false
        videoDecoder?.close(); videoDecoder = null
        markChatDeliveryUncertain()
        autoResumeEpoch = null
        if (chatStopPending) {
            chatStopPending = false
            pendingStopRpc = null
            chatStopStatus = "Stop unconfirmed: connection lost"
        }
        if (agentStopPending) {
            agentStopPending = false
            pendingAgentStopRpc = null
            agentStopStatus = "Agent stop unconfirmed: connection lost"
        }
        AppUpdates.disconnected()
        viewerGeneration++
        chatConnected = false
        viewerConnection = "Disconnected"
        desktopBrowser.sessionLost(); desktopBack.sessionLost(); foregroundWindow.sessionLost()
        viewerControl = false
        viewLink?.close()
        viewLink = null
        frame = null
    }

    fun leaveViewer() {
        disconnect()
        screen = Screen.Home
    }

    var remoteKeyboardRequest by mutableStateOf(0L)
        private set
    private var focusCheckId = 0L
    fun checkRemoteTextFocus() {
        focusCheckId++
        sendInput(JSONObject().put("t", "focus_check").put("id", focusCheckId))
    }
    fun invalidateRemoteTextFocus() { focusCheckId++ }

    fun sendInput(json: JSONObject) {
        if (viewerControl) viewLink?.send(Msg.INPUT, json.toString().toByteArray())
    }

    fun chatRequest(command: JSONObject): String? {
        val rpc = java.util.UUID.randomUUID().toString()
        command.put("rpc", rpc)
        if (viewLink?.send(Msg.CHAT_REQUEST, command.toString().toByteArray(Charsets.UTF_8)) != true) {
            chatConnected = false
            chatStatus = "Connection unavailable. Your draft has been kept."
            return null
        }
        return rpc
    }

    fun sendChat() = submitChat(::chatRequest)

    fun stopChat() = requestChatStop(::chatRequest)

    fun stopAgentWork() = requestAgentStop(::chatRequest)

    fun restartChat() {
        if (!chatConnected || restartPending) return
        restartPending = true
        val rpc = chatRequest(JSONObject().put("op", "restart_chat"))
        pendingRestartRpc = rpc
        if (rpc == null) {
            restartPending = false
            pendingRestartRpc = null
            chatStatus = "Restart unconfirmed: connection unavailable"
        }
    }

    fun resumeChat() {
        if (chatConnected && chatStopStatus != null) chatRequest(JSONObject().put("op", "resume_chat"))
    }

    fun pollApprovals() {
        if (!chatConnected || pendingApprovalPoll != null) return
        val rpc = chatRequest(JSONObject().put("op", "approval_list")) ?: return
        pendingApprovalPoll = rpc
        main.postDelayed({
            if (pendingApprovalPoll == rpc) {
                pendingApprovalPoll = null
                approvalPollTimeouts++
                if (approvalPollTimeouts >= MAX_APPROVAL_POLL_TIMEOUTS) {
                    approvalPollTimeouts = 0
                    viewerStatus = "PC stopped responding. Reconnecting…"
                    viewLink?.reconnect()
                }
                // Otherwise the 2.5 s poll loop re-issues approval_list; a
                // slow PC gets two more chances before a reconnect is forced.
            }
        }, 10_000)
    }

    // 100.016 (E14 diagnostics): carry the `health` snapshot on the same
    // 2.5 s loop as pollApprovals() — the loop lives at the activity
    // level and calls both. No second poll loop, no extra battery cost.
    // Poll-carry decision (Worker A, PATCH-REPORT-A §0): the phone
    // issues {"op": "health"} on the existing loop; the `health` op
    // serves the 13-row snapshot only, and a separate
    // `diagnostics_export` op serves the export bundle on demand. The
    // phone never probes the PC directly and never runs its own health
    // check.
    fun pollHealth() {
        if (!chatConnected || pendingHealthPoll != null) return
        val rpc = chatRequest(JSONObject().put("op", "health")) ?: return
        pendingHealthPoll = rpc
        main.postDelayed({
            if (pendingHealthPoll == rpc) pendingHealthPoll = null
        }, 10_000)
    }

    // 100.017 (100.018 design §B1–B2): the drawer fetch. Sends
    // {"op": "runtime_stream", "limit": 100} with NO `since` — the parser
    // rebuilds the running/finished set from the full window each fetch.
    // Pending-rpc guard (no overlapping fetches, same shape as
    // pollApprovals); 10 s timeout clears the guard (the next poll
    // retries); a failed fetch keeps the last good snapshot (see the
    // "runtime_stream" branches of handleChatResponse). The caller is
    // the drawer loop (every 5 s while open, immediate when push drops)
    // and the Refresh button — never while the drawer is closed.
    fun fetchAgentsSnapshot() {
        if (!chatConnected || pendingAgentsRpc != null) return
        val rpc = chatRequest(JSONObject().put("op", "runtime_stream").put("limit", 100)) ?: return
        pendingAgentsRpc = rpc
        agentsLoading = true
        main.postDelayed({
            if (pendingAgentsRpc == rpc) {
                pendingAgentsRpc = null
                agentsLoading = false
            }
        }, 10_000)
    }

    // 100.033 (message board): the board rides the SAME runtime_stream
    // fetch as the Agents tab — one request, both tabs' data derived in
    // the "runtime_stream" branch of handleChatResponse. No second fetch
    // for the same data (spec §12 #10). Call this from the board's
    // Refresh path; it is fetchAgentsSnapshot() by another name, so the
    // drawer loop and the Refresh button keep fetching through one path.
    fun fetchBoardPosts() {
        fetchAgentsSnapshot()
    }

    // 100.033: mark the Board tab seen up to newestTsMs (clears the
    // new-post dot); persisted so the dot survives a restart. Never moves
    // the mark backwards.
    fun boardMarkSeen(newestTsMs: Long) {
        if (newestTsMs <= boardLastSeenTsMs) return
        boardLastSeenTsMs = newestTsMs
        boardPrefs?.edit()?.putLong("last_seen_post_ts_ms", newestTsMs)?.apply()
    }
    // are display-only (§7.2): nothing here acts, retries, or changes
    // safety state; the ack is a delivery receipt only (§5.2). Unknown
    // kinds and malformed payloads are ignored, like the neighbouring
    // handlers guard their parses.
    // 100.017 (C13 push, spec §3.3–§4): the sealed stream handler. Frames
    // are display-only (§7.2): nothing here acts, retries, or changes
    // safety state; the ack is a delivery receipt only (§5.2). Unknown
    // kinds and malformed payloads are ignored, like the neighbouring
    // handlers guard their parses.
    internal fun handleStreamFrame(payload: ByteArray) {
        val frame = parseStreamFrame(payload) ?: return
        val nowMs = System.currentTimeMillis()
        when (frame.kind) {
            "stream.hello" -> {
                // "I am the stream" — store the host's sources, then
                // subscribe from the phone's applied frontier (0 =
                // "everything"). The host catches us up, then goes live.
                streamLastFrameMs = nowMs
                streamSources = frame.sources
                streamSubscribed = true
                sendStreamSubscribe()
                refreshStreamTransport(nowMs)
            }
            "stream.events" -> {
                streamLastFrameMs = nowMs
                val applied = streamBuffer.applyEvents(frame.events, nowMs)
                if (applied.resyncNeeded || applied.gapDetected) {
                    // Two recoveries, one road (Worker A contract, 100.017):
                    // (1) a seq went backwards — the ring re-numbered under
                    // us (§4.3); (2) a gap — a frame was lost, and push
                    // never re-sends, so the gap heals ONLY via one full
                    // poll fetch, never by waiting for push to fill it.
                    // Either way the partial batch is discarded — the poll
                    // supersedes it — and no ack is sent past the frontier.
                    streamBuffer.reset()
                    fetchAgentsSnapshot()
                    refreshStreamTransport(nowMs)
                    return
                }
                if (applied.applied.isNotEmpty()) {
                    streamEvents = (streamEvents + applied.applied).takeLast(100)
                    agentsSnapshot = rebuildSnapshotFromStreamEvents(nowMs)
                    // 100.033 (message board): push frames derive board
                    // posts through the same parser as the poll path, so
                    // push and poll render identically. Merge, not
                    // replace — push is incremental; the poll path owns
                    // the resync replacement (spec §5.2).
                    boardPosts = mergeBoardPosts(boardPosts, rebuildBoardFromStreamEvents(nowMs))
                    // 100.037 (E12 watchdog): push frames derive the
                    // Muse-worker state through the same parser as the
                    // poll path, so push and poll render identically.
                    // Fold, not replace — push is incremental; the poll
                    // path owns the resync replacement (spec §5.2).
                    museWorkerState = rebuildWorkerFromStreamEvents(nowMs)
                    boardUpdatedAtMs = nowMs
                    streamLastDeliveryMs = nowMs
                    if (applied.ackSeq > streamAckedSeq) {
                        streamAckedSeq = applied.ackSeq
                        sendStreamAck(applied.ackSeq)
                    }
                }
                refreshStreamTransport(nowMs)
            }
            "stream.resynced" -> {
                // Our `since` predates the ring: one full fetch (§4.4).
                streamLastFrameMs = nowMs
                streamBuffer.reset()
                fetchAgentsSnapshot()
                refreshStreamTransport(nowMs)
            }
            // stream.ack / stream.subscribe / stream.ping are phone→host
            // (or not built in 100.017): never sent by the host — ignore.
            else -> Unit
        }
    }

    internal fun sendStreamSubscribe() {
        viewLink?.send(
            Msg.STREAM,
            JSONObject().put("kind", "stream.subscribe").put("since", streamBuffer.frontierSeq)
                .toString().toByteArray(Charsets.UTF_8)
        )
    }

    internal fun sendStreamAck(ackedSeq: Long) {
        // Ack ONLY what was actually applied, in order, never past a gap
        // (§5.2). The host records it; it never re-sends on ack alone.
        viewLink?.send(
            Msg.STREAM,
            JSONObject().put("kind", "stream.ack").put("acked_seq", ackedSeq)
                .toString().toByteArray(Charsets.UTF_8)
        )
    }

    // Re-base the push cursor on a polled window (V1: seq is
    // window-relative). The poll's event list becomes the stream window
    // and its keys seed the dedup set, so a push frame that re-delivers
    // a window event is recognised as a duplicate.
    private fun rebaseStreamWindow(result: JSONObject, nowMs: Long) {
        val eventsArr = result.optJSONArray("events")
        val window = if (eventsArr != null) {
            (0 until eventsArr.length()).mapNotNull { runCatching { eventsArr.getJSONObject(it) }.getOrNull() }
        } else emptyList()
        streamEvents = window
        result.optJSONObject("sources")?.let { streamSources = it }
        streamBuffer.rebase(result.optLong("latest_seq", 0L), window, nowMs)
    }

    private fun rebuildSnapshotFromStreamEvents(nowMs: Long): AgentsSnapshot {
        val response = JSONObject()
            .put("events", JSONArray(streamEvents))
            .put("sources", streamSources ?: JSONObject())
            .put("counts", JSONObject().put("skipped_bad_lines", 0))
        return agentsFromRuntimeEvents(response.toString(), nowMs)
    }

    // 100.033 (message board): the push-path twin of the function above —
    // the board parser over the same pushed window, with the same shape.
    private fun rebuildBoardFromStreamEvents(nowMs: Long): List<BoardPost> {
        val response = JSONObject()
            .put("events", JSONArray(streamEvents))
            .put("sources", streamSources ?: JSONObject())
            .put("counts", JSONObject().put("skipped_bad_lines", 0))
        return boardPostsFromRuntimeEvents(response.toString(), nowMs)
    }

    // 100.037 (E12 watchdog): the push-path twin of the function above —
    // the worker-state parser over the same pushed window, with the same
    // shape, folded over the current state so a window with no worker.*
    // events leaves it unchanged (same rule as the poll path).
    private fun rebuildWorkerFromStreamEvents(nowMs: Long): MuseWorkerState {
        val response = JSONObject()
            .put("events", JSONArray(streamEvents))
            .put("sources", streamSources ?: JSONObject())
            .put("counts", JSONObject().put("skipped_bad_lines", 0))
        return workerStateFromRuntimeEvents(response.toString(), nowMs, museWorkerState)
    }

    // Fallback state machine (spec §6.1). Called on every stream frame,
    // every successful poll, and every drawer tick (1 s).
    fun refreshStreamTransport(nowMs: Long = System.currentTimeMillis()) {
        streamTransport = evaluateStreamTransport(
            viewerConnection == "Connected", streamLastFrameMs, streamLastDeliveryMs, nowMs
        )
    }

    // A dropped session keeps the applied frontier (the next hello
    // re-subscribes from it, §4.6). There is nothing unapplied to drop:
    // gaps are never buffered (Worker A contract — a gap heals only via
    // a full fetch, never from push), so the subscribed flag is all that
    // clears. Disconnect / session loss / BYE all funnel through here.
    private fun resetStreamSession() {
        streamSubscribed = false
        // 100.017 deploy-time fix: a disconnect ends any in-flight agents
        // fetch — the reply will never arrive on a closed socket. Clear
        // the in-flight guard (and the loading flag) so the next
        // session's first fetch is not blocked for up to the 10 s
        // timeout. Without this, unit tests leak a stale guard across
        // tests (Model is a singleton) and the next session's fetch is
        // silently suppressed; in the app the same stale guard blocks the
        // first fetch after a reconnect.
        pendingAgentsRpc = null
        agentsLoading = false
    }

    // A new PC owns a new ring: drop everything, frontier included.
    private fun resetStreamState() {
        streamBuffer.reset()
        streamAckedSeq = 0L
        streamSubscribed = false
        streamEvents = emptyList()
        streamSources = null
        streamLastFrameMs = 0L
        streamLastDeliveryMs = 0L
        streamTransport = StreamTransport.POLLING
    }

    // 100.016 (E14): on-demand export request, for the Diagnostics
    // screen's Share button. One request per tap (the pending guard
    // blocks a second while one is in flight); the reply is handled in
    // handleChatResponse ("diagnostics_export"). The scrub pass runs on
    // the PC in the service before anything crosses the session. No file
    // is written on the phone — the received text goes straight to the
    // share sheet. If the session is down the button never sends a stale
    // or partial bundle; it says so instead (see shareButtonLabel).
    // The optional androidVersion ("100.016 (code 42)" shape, from
    // BuildConfig via the Diagnostics screen) fills the export header's
    // Android version (Worker A, PATCH-REPORT-A §0 — honoured when
    // present, never invented when absent). The phone sends no `config`:
    // the PC's config is the service's to report, not the phone's to guess.
    fun requestDiagnosticsExport(androidVersion: String? = null) {
        if (!chatConnected) {
            diagnosticsExportError = "Session down — export from the PC"
            return
        }
        if (diagnosticsExportPending) return
        diagnosticsExportPending = true
        diagnosticsExportError = null
        val command = JSONObject().put("op", "diagnostics_export")
        if (!androidVersion.isNullOrBlank()) command.put("android_version", androidVersion)
        val rpc = chatRequest(command)
        pendingExportRpc = rpc
        if (rpc == null) {
            diagnosticsExportPending = false
            diagnosticsExportError = "Connection unavailable"
        } else {
            main.postDelayed({
                if (pendingExportRpc == rpc) {
                    pendingExportRpc = null
                    diagnosticsExportPending = false
                    diagnosticsExportError = "No reply from the PC"
                }
            }, 10_000)
        }
    }

    fun resolveApproval(id: String, approved: Boolean) {
        if (!chatConnected) return
        chatRequest(JSONObject().put("op", "approval_resolve").put("approval_id", id).put("approved", approved))
    }

    fun stopStreamAndRestoreLandscape() {
        setViewerLandscape(true)
        disconnect()
        screen = Screen.Home
    }

    internal fun requestChatStop(send: (JSONObject) -> String?) {
        if (!chatConnected || chatStopPending) return
        chatStopPending = true
        chatStopStatus = "Requesting stop…"
        val rpc = send(JSONObject().put("op", "stop_chat"))
        pendingStopRpc = rpc
        if (rpc == null) {
            chatStopPending = false
            chatStopStatus = "Stop unconfirmed: connection unavailable"
        } else {
            main.postDelayed({ markStopUnconfirmed(rpc) }, 45_000)
        }
    }

    internal fun markStopUnconfirmed(rpc: String) {
        if (chatStopPending && pendingStopRpc == rpc) {
            chatStopPending = false
            pendingStopRpc = null
            chatStopStatus = "Stop unconfirmed: no response from PC"
        }
    }

    // 100.025 (S4/S13): agent-only stop, following the requestChatStop
    // pattern exactly (same guard, same "Requesting…" status, same
    // 45 s no-response timeout). Sends {"op": "stop_agent_work"}; the
    // service interrupts the running agent turn if it has one and
    // cancels queued agent turns, and returns its `status` string for
    // display. Chat admissions are untouched on this path.
    internal fun requestAgentStop(send: (JSONObject) -> String?) {
        if (!chatConnected || agentStopPending) return
        agentStopPending = true
        agentStopStatus = "Requesting agent stop…"
        val rpc = send(JSONObject().put("op", "stop_agent_work"))
        pendingAgentStopRpc = rpc
        if (rpc == null) {
            agentStopPending = false
            agentStopStatus = "Agent stop unconfirmed: connection unavailable"
        } else {
            main.postDelayed({ markAgentStopUnconfirmed(rpc) }, 45_000)
        }
    }

    internal fun markAgentStopUnconfirmed(rpc: String) {
        if (agentStopPending && pendingAgentStopRpc == rpc) {
            agentStopPending = false
            pendingAgentStopRpc = null
            agentStopStatus = "Agent stop unconfirmed: no response from PC"
        }
    }

    // 100.040 (§7): one-off history re-fetch for a submitted turn that
    // has been dispatching too long. It fires once, ~5 s after submit
    // (well before the 45 s unconfirmed deadline), and only while the same turn is still
    // submitted — it is NOT a poll loop (the 2.5 s loop still polls
    // approvals/health only, and no new loop is added). If the re-fetch
    // shows the turn resolved, the normal history path clears the gate;
    // if it shows the turn still dispatching (or the fetch fails), the
    // 45 s handler marks it unknown. The send is injectable, like
    // submitChat/requestAgentStop, so tests can observe the re-fetch
    // without a live link.
    internal fun refreshHistoryForSubmittedTurn(turnId: String, send: (JSONObject) -> String? = ::chatRequest) {
        if (submittedTurnId != turnId || !chatConnected) return
        send(JSONObject().put("op", "history"))
    }

    // 100.040 (§7): the 45 s no-response timeout for a submitted turn,
    // mirroring requestAgentStop/markAgentStopUnconfirmed exactly
    // (same 45 s, same postDelayed shape, same guard-then-clear
    // structure — keyed on the turn id, not the rpc, because the
    // submit ack already clears pendingChatRpc while submittedTurnId
    // persists). If the turn is still waiting, it is marked "unknown":
    // the turn's row in chatTurns (when present) takes status
    // "unknown" so the chatTurns clause of chatActive clears, the
    // "Delivery unconfirmed. Check before resending." notice surfaces
    // through desktopChat.unconfirmed(), and clearing
    // submittedTurnId/pendingChatCommand re-enables Send (assuming no
    // other turn is queued/dispatching). A late history still
    // supersedes the local unknown marking — the server order is
    // authoritative — and a resolved turn no-ops here because
    // submittedTurnId is already null.
    internal fun markSubmittedTurnUnconfirmed(turnId: String) {
        if (submittedTurnId != turnId) return
        submittedTurnId = null
        pendingChatCommand = null
        pendingChatRpc = null
        pendingChatText = null
        chatTurns = chatTurns.map { turn ->
            if (turn.optString("id") == turnId) JSONObject(turn.toString()).put("status", "unknown") else turn
        }
        desktopChat.unconfirmed(turnId) { reply -> speakReply(reply.text) }
        chatStatus = "Delivery unconfirmed. Check before resending."
    }

    internal fun submitChat(send: (JSONObject) -> String?) {
        if (!canSendChat || pendingRouteSuggestRpc != null) return
        // A selected Muse route is an explicit choice, even for short
        // messages that the automatic router would otherwise send locally.
        if (chatRouteMode == "Auto" && chatRoutes.getOrNull(chatRouteIndex)?.optString("assistant") != "muse") {
            // Batch 2 router: ask the service which route fits this
            // message first, so the picked route's reason can be shown.
            // The suggestion response submits with the picked route; a
            // missing or failed suggestion falls back to the selected
            // model. The draft stays until the submit is acknowledged.
            val rpc = send(JSONObject().put("op", "route_suggest").put("text", chatDraft))
            if (rpc == null) {
                submitWithRoute(chatRoutes.getOrNull(chatRouteIndex), chatDraft, send)
            } else {
                pendingRouteSuggestRpc = rpc
                pendingAutoSubmitText = chatDraft
                pendingAutoSubmitSend = send
                chatStatus = "Choosing route…"
                main.postDelayed({
                    if (pendingRouteSuggestRpc == rpc) {
                        pendingRouteSuggestRpc = null
                        pendingAutoSubmitText = null
                        pendingAutoSubmitSend = null
                        chatStatus = "Route suggestion timed out. Try sending again."
                    }
                }, 10_000)
            }
            return
        }
        submitWithRoute(chatRoutes.getOrNull(chatRouteIndex), chatDraft, send)
    }

    internal fun submitWithRoute(route: JSONObject?, text: String, send: (JSONObject) -> String?, effortOverride: String? = null) {
        val selected = route ?: return
        val command = JSONObject().put("op", "submit").put("id", java.util.UUID.randomUUID().toString())
            .put("sent_at", System.currentTimeMillis())
            .put("text", text).put("assistant", selected.getString("assistant"))
            .put("model", selected.getString("model")).put("account", selected.getString("account"))
            .put("effort", effortOverride?.takeIf { it.isNotBlank() } ?: chatEffort)
            .put("agent", computerTaskRequested(selected.getString("assistant"), localAiTasksAvailable, computerTaskMode))
        pendingChatText = text
        submittedTurnId = command.getString("id")
        pendingChatCommand = JSONObject(command.toString())
        pendingChatRpc = send(command)
        if (pendingChatRpc == null) {
            pendingChatCommand = null
            submittedTurnId = null
            chatStatus = "Message wasn't sent. Your draft has been kept."
        } else {
            desktopChat.submitted(command)
            chatStatus = "Sending…"
            // 100.040 (§7): the submitted-turn wait has a deadline.
            // The submit ack ("Queued") is not a resolution — only a
            // history update showing the turn out of queued/dispatching
            // clears submittedTurnId — so a turn whose history never
            // arrives locks Send forever (the chat freeze). At ~5 s a
            // one-off history re-fetch goes out (not a poll: it fires
            // once, only while this turn is still submitted); at 45 s —
            // the same no-response timeout as the agent-stop path
            // (requestAgentStop/markAgentStopUnconfirmed) — the turn is
            // marked unknown if it is still waiting.
            val turnId = command.getString("id")
            main.postDelayed({ refreshHistoryForSubmittedTurn(turnId, send) }, 5_000)
            main.postDelayed({ markSubmittedTurnUnconfirmed(turnId) }, 45_000)
        }
    }

    fun forget(pc: PairedPc) {
        pcs = pcs.filter { it.id != pc.id }
        store.save(pcs)
        // 100.011 (spec §8.5): unpairing deletes the PC's zoom entry too.
        deleteZoomForPc(pc.id)
    }

    /**
     * Manual relay-URL refresh (100.012 H11): replace one paired PC's stored
     * relay URL — e.g. after the PC's address changed — and
     * persist it through the existing PcStore save path. The pairing itself
     * (name, secret, control flag) is untouched and the pairing flow is not
     * involved. Returns false (and sets relayEditStatus) when the address
     * is not a ws:// or wss:// URL. If the edited PC is the one currently
     * on screen, it reconnects on the new URL immediately; otherwise the
     * next Connect uses it.
     */
    fun updateRelay(pc: PairedPc, relay: String): Boolean {
        val trimmed = relay.trim().trimEnd('/')
        if (!(trimmed.startsWith("ws://") || trimmed.startsWith("wss://"))) {
            relayEditStatus = "Relay address must start with ws:// or wss://"
            return false
        }
        // 100.015: a manual edit is a single address — the candidate list
        // collapses to it (relay is derived from relays.first()).
        val updated = pc.copy(relays = listOf(trimmed))
        pcs = pcs.map { if (it.id == pc.id) updated else it }
        store.save(pcs)
        relayEditStatus = ""
        if (screen == Screen.Viewer && viewerPc?.id == updated.id) connect(updated)
        return true
    }
}

// ---------------------------------------------------------------------------
// 100.017 (C13 push) — stream frame / fallback-state pure logic. Top-level
// (not Model members) so unit tests can cover them without a session.
// ---------------------------------------------------------------------------

/** Fallback state machine (spec §6.1): the drawer footer's transport fact. */
enum class StreamTransport { LIVE_PUSH, POLLING, STALE, TUNNEL_DOWN }

/** One parsed sealed stream frame (spec §3.4). kind is never blank. */
internal data class StreamFrame(
    val kind: String,
    val events: List<JSONObject> = emptyList(),
    val latestSeq: Long = 0L,
    val sources: JSONObject? = null,
    val since: Long = 0L,
    val ackedSeq: Long = 0L,
)

/** Parse one sealed stream payload. Null when malformed or kind-less. */
internal fun parseStreamFrame(payload: ByteArray): StreamFrame? {
    val o = runCatching { JSONObject(String(payload, Charsets.UTF_8)) }.getOrNull() ?: return null
    val kind = o.optString("kind")
    if (kind.isBlank()) return null
    val arr = o.optJSONArray("events")
    val events = if (arr != null) {
        (0 until arr.length()).mapNotNull { runCatching { arr.getJSONObject(it) }.getOrNull() }
    } else emptyList()
    return StreamFrame(
        kind = kind,
        events = events,
        latestSeq = o.optLong("latest_seq", 0L),
        sources = o.optJSONObject("sources"),
        since = o.optLong("since", 0L),
        ackedSeq = o.optLong("acked_seq", 0L),
    )
}

/**
 * Pure fallback evaluation (spec §6.1). socketOpen is "the session is
 * live" (Model.viewerConnection == "Connected"); lastFrameMs is the last
 * stream.* frame of any kind; lastDeliveryMs is the last applied push
 * batch or successful poll fetch. 3 s without a frame on a live socket
 * is a real outage (matches the worker watchdog's 3-miss rule); 6 s
 * with neither push nor poll delivering is STALE; both dead at once
 * names the tunnel (V4 shared fate — poll and push share the relay
 * socket, so a dead tunnel kills both).
 */
internal fun evaluateStreamTransport(
    socketOpen: Boolean,
    lastFrameMs: Long,
    lastDeliveryMs: Long,
    nowMs: Long,
): StreamTransport {
    if (socketOpen && nowMs - lastFrameMs <= 3_000) return StreamTransport.LIVE_PUSH
    if (nowMs - lastDeliveryMs <= 6_000) return StreamTransport.POLLING
    if (!socketOpen) return StreamTransport.TUNNEL_DOWN
    return StreamTransport.STALE
}

/** The footer's transport line (spec §6.2) — one fact, never blended. */
internal fun streamTransportText(transport: StreamTransport, nowMs: Long, lastDeliveryMs: Long): String {
    val age = formatElapsed(((nowMs - lastDeliveryMs).coerceAtLeast(0)) / 1000)
    return when (transport) {
        StreamTransport.LIVE_PUSH -> "Live push"
        StreamTransport.POLLING -> "Polling — push down"
        StreamTransport.STALE -> if (lastDeliveryMs <= 0) "STALE — no update yet"
            else "STALE — no update for $age"
        StreamTransport.TUNNEL_DOWN -> if (lastDeliveryMs <= 0) "STALE — no update yet — tunnel down (push and poll share the same connection)"
            else "STALE — no update for $age — tunnel down (push and poll share the same connection)"
    }
}

/**
 * 100.029 (E41 structured logging, spec §6.3): the runtime-stream
 * footer's per-source lines — health + idle age + configured level,
 * one line per source. The sources are the runtime stream's own
 * (agent, lifecycle, bridge, tunnel), in this order — not all 13
 * diagnostics rows, which would be a board, not a footer.
 *
 * Shape per source (the same "one fact per line" style as
 * streamTransportText / workerFooterLine):
 *   agent: OK · idle 12s · INFO
 *   bridge: DOWN · source down
 * Rules: the health word always sits next to the idle age — a bare
 * "idle 12s" never appears; a down source is named as down and never
 * carries a stale age dressed up as fresh; the age is the age of the
 * source's last good as_of (its last_ok), never the arrival time of
 * the snapshot — the footer never shows arrival time as event time.
 * The level is the source's CONFIGURED level from the health()
 * snapshot's `level` field (Worker B adds it service-side) — what the
 * source would log right now — not a per-line level. That is how a
 * DEBUG session left on gets caught: `agent: OK · idle 2s · DEBUG`
 * with no `debug.enabled` line in the log is a bug (§4).
 *
 * WIRING NOTE: these are the pure footer lines only. The drawer
 * composable (AgentsDrawer.kt, outside this worker's files) renders
 * them with one Text per line — sourceFooterText() is the single-block
 * form for that Text.
 */
internal val SOURCE_FOOTER_KEYS: List<String> = listOf("agent", "lifecycle", "bridge", "tunnel")

/** One footer line for one source. `row` is null when the snapshot has no row for the key (or there is no snapshot yet). */
internal fun sourceFooterLine(row: HealthComponentRow?, key: String, nowEpochSec: Long): String {
    val r = row ?: return "$key: UNKNOWN · no reading yet · level unknown"
    if (r.status == HealthStatus.DOWN) return "$key: DOWN · source down"
    val level = r.level.ifBlank { "level unknown" }
    val age = if (r.lastOkEpochSec <= 0) "no reading yet"
    else "idle ${formatAgeSeconds((nowEpochSec - r.lastOkEpochSec).coerceAtLeast(0))}"
    return "$key: ${r.status.name} · $age · $level"
}

/** The four per-source footer lines, in SOURCE_FOOTER_KEYS order. */
internal fun sourceFooterLines(snapshot: HealthSnapshot?, nowEpochSec: Long): List<String> {
    val byKey = snapshot?.components?.associateBy { it.key }.orEmpty()
    return SOURCE_FOOTER_KEYS.map { key -> sourceFooterLine(byKey[key], key, nowEpochSec) }
}

/** The four per-source footer lines as one block, for a single Text. */
internal fun sourceFooterText(snapshot: HealthSnapshot?, nowEpochSec: Long): String =
    sourceFooterLines(snapshot, nowEpochSec).joinToString("\n")

/**
 * The phone-side seq/dedup machine (spec §3.3 + §4 + §5.2, plus the
 * Worker A contract note, 100.017).
 *
 * - frontierSeq: highest CONTIGUOUS seq applied (the ack value). It is
 *   also "latestSeen": because gaps are never buffered, the highest seq
 *   ever seen can never run ahead of the contiguous frontier.
 * - Stale events (spec §3.3 step 3): an event with seq <= frontier is
 *   dropped INDIVIDUALLY when its (source, ts, type, path/command) key
 *   matches something applied recently (5 s window — the op's own dedup
 *   key). A stale event with an UNSEEN key is not a duplicate — the ring
 *   re-numbered (seq is window-relative, V1) — so a whole frame arriving
 *   backwards trips resyncNeeded and the phone does one full poll fetch
 *   (since=null, §4.3). On every reachable input this coincides with
 *   the contract note's line (a seen-key stale event always drops
 *   individually; a wholly-backwards frame with new content always
 *   resyncs); the unreachable corner — a stale UNSEEN-key event inside
 *   an otherwise fresh frame, which one op call can never produce —
 *   resyncs rather than silently dropping new content (see the report).
 * - Gaps (Worker A contract, 100.017): an event with seq > frontier+1
 *   means a frame was lost. The host never re-sends (drop-never-queue,
 *   §3.2 step 5), so the gap heals ONLY via one full poll fetch — the
 *   buffer never holds an out-of-order event waiting for push to fill
 *   the gap, and the ack never advances past it. gapDetected trips the
 *   same full-fetch recovery as resyncNeeded.
 * - Events without a seq are dropped (never acked).
 * - rebase() re-bases the cursor on a polled window (V1: seq is
 *   window-relative) and seeds the dedup set from the window, so a
 *   push frame that re-delivers a window event dedups cleanly.
 */
internal class StreamEventBuffer {
    var frontierSeq: Long = 0L
        private set
    private val seenKeys = ArrayDeque<Pair<String, Long>>()

    /** The op's dedup key (source, ts, type, path/command). */
    fun dedupKey(event: JSONObject): String {
        val detail = event.optJSONObject("detail")
        val path = detail?.optString("path")?.ifBlank { detail.optString("command") } ?: ""
        // ts is numeric (epoch seconds) but a JSON round-trip changes its
        // NUMBER TYPE: a Double 1762650000.0 serialises as 1762650000 and
        // parses back as Long — opt("ts").toString() is type-dependent
        // ("1.76265E9" vs "1762650000"), so two copies of the same event
        // dedup-miss and the frame resyncs. optDouble coerces both to the
        // same Double ("1.76265E9"), so the key is stable across the wire.
        val tsStr = if (event.has("ts")) event.optDouble("ts").toString() else ""
        return listOf(event.optString("source"), tsStr, event.optString("type"), path)
            .joinToString("|")
    }

    private fun pruneKeys(nowMs: Long) {
        while (seenKeys.firstOrNull()?.let { nowMs - it.second > 5_000 } == true) seenKeys.removeFirst()
    }

    private fun keySeen(key: String, nowMs: Long): Boolean {
        pruneKeys(nowMs)
        return seenKeys.any { it.first == key }
    }

    private fun markKey(key: String, nowMs: Long) {
        pruneKeys(nowMs)
        seenKeys.addLast(key to nowMs)
    }

    data class ApplyResult(
        val applied: List<JSONObject>,
        val ackSeq: Long,
        val resyncNeeded: Boolean,
        val gapDetected: Boolean,
        val droppedDuplicates: Int,
    )

    fun applyEvents(events: List<JSONObject>, nowMs: Long): ApplyResult {
        val applied = mutableListOf<JSONObject>()
        var dupes = 0
        for (event in events.sortedBy { it.optLong("seq", -1) }) {
            val seq = event.optLong("seq", -1)
            if (seq < 0) continue
            val key = dedupKey(event)
            if (seq <= frontierSeq) {
                // Stale: a duplicate drops individually; an unseen key at
                // or below the frontier means the ring re-numbered.
                if (keySeen(key, nowMs)) { dupes++; continue }
                return ApplyResult(applied, frontierSeq, resyncNeeded = true, gapDetected = false, droppedDuplicates = dupes)
            }
            if (seq > frontierSeq + 1) {
                // Gap: a frame was lost. Never buffer, never wait for push
                // to fill it — the full fetch heals it (see class doc).
                return ApplyResult(applied, frontierSeq, resyncNeeded = false, gapDetected = true, droppedDuplicates = dupes)
            }
            // seq == frontierSeq + 1: contiguous — apply in order.
            markKey(key, nowMs)
            applied.add(event)
            frontierSeq = seq
        }
        return ApplyResult(applied, frontierSeq, resyncNeeded = false, gapDetected = false, droppedDuplicates = dupes)
    }

    fun reset() {
        frontierSeq = 0L
        seenKeys.clear()
    }

    fun rebase(latestSeq: Long, window: List<JSONObject>, nowMs: Long) {
        reset()
        frontierSeq = latestSeq
        for (event in window) markKey(dedupKey(event), nowMs)
    }
}
