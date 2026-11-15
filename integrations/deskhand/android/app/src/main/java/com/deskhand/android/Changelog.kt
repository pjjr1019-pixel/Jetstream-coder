package com.deskhand.android

/** Shown when the version number is tapped on the Home screen. */
const val CHANGELOG_TEXT = """
Deskhand changelog

v100.050 — 2026-11-11
- Minimize, close, and stop are now one floating glass pill at the top-right — minimize (−), close (×), stop octagon last — instead of two stacked clusters.
- The toolbar fades with the rest of the viewer chrome and hides while a drawer is open; tapping any toolbar button resets the auto-hide timer first.
- Honest limit: the toolbar's glass comes from the frosted-glass version, which is live — the toolbar calls its GlassSurface. The live build/test proof is a PC item.
- Version stamp 100.050 / code 76.

v100.049 — 2026-11-11
- The Connected pill is now a live, tappable status readout: a pulsing dot while Connected, the frame-arrival interval next to the connection word, and a tap opens a bottom sheet with the relay address, relay host, last-frame-OK time, and frame interval.
- Honest limit: the interval is the gap between frame arrivals, not a network round trip; the pulse and the interval are verified on device, not by the unit suite. The live build/test proof is a PC item.
- Version stamp 100.049 / code 75.

v100.048 — 2026-11-11
- Built-in verify-this-build step: one op runs the whole Gate 4 checklist — hash the after files, probe the transfer, run the build, Specs, pytest, and Gradle commands, and check the gateway — and reports one structured pass/fail result.
- Honest limit: publish and relaunch are dry-run reports only — the op never publishes or relaunches. The live build/test proof is a PC item.
- Version stamp 100.048 / code 74.

v100.047 — 2026-11-11
- Byte-exact file transfer: a file now moves to the PC in chunks and arrives byte-for-byte, with its sha256 verified on arrival — CRLF, LF, mixed endings, and binary files all survive the trip intact instead of losing line endings.
- Honest limit: no new phone screen in this version — transfers are driven in the op reply. The live build/test proof is a PC item.
- Version stamp 100.047 / code 73.

v100.046 — 2026-11-11
- Real indexed code search: search for text, files by name, or symbol definitions across a tree — powered by ripgrep with a Python fallback that returns the same results. Finds files however deep they sit (the old search missed a file three levels down).
- Honest limit: no new phone screen in this version — results come back in the op reply. The live build/test proof is a PC item.
- Version stamp 100.046 / code 72.

v100.045 — 2026-11-11
- Long-running commands: a command can now run as long as it needs (default 1 h, max 24 h) instead of dying at the old timeout, and you can watch its output arrive live in the runtime feed, or check its result (exit code, output tails, duration) afterwards. Cancelling kills the whole process tree.
- Honest limit: no new phone screen in this version — output is watched in the existing runtime feed. The live build/test proof is a PC item.
- Version stamp 100.045 / code 71.

v100.044 — 2026-11-11
- Git, carefully: the repo is now committed in small slices (skeleton, Windows, service, Android, Go, docs, scripts, CI — one slice per commit, one version per publish) instead of one giant dump.
- Every commit is scanned for secrets before it lands: gitleaks over staged files plus a repo-owned grep pass. Any finding blocks the commit — no warn-and-continue, no --no-verify escape. A secret in history is fixed only by re-creating the repo and rotating the secret.
- Honest limit: the CI leg is not wired yet — the commit-time secret checks are local only until the CI leg lands.
- Version stamp 100.044 / code 70.

v100.043 — 2026-11-11
- Frosted glass controls: the floating chrome (Connected pill, drawer toggles, composer, Fit/Stretch/Zoom bar, foreground-window buttons, notice banner) now uses one shared glass surface — surface at 0.60 alpha with a 1.dp outline border at 0.30 alpha — instead of each chrome element carrying its own fill.
- The composer keeps its focus border (focused → primary at full strength; unfocused → the glass border), and the stop button stays solid red — neither changes in this version.
- Honest limit: this is translucent fill + border only, not a real backdrop effect — Compose has no backdrop effect, so the desktop stream stays sharp behind the chrome.
- Version stamp 100.043 / code 69.

v100.042 — 2026-11-10
- The desktop stream now fills the whole screen edge-to-edge, under the status and navigation bars; the viewer chrome (Connected pill, stop button, composer, drawer toggles, foreground-window buttons) floats over it.
- Chrome fades away after ~3 s without interaction and comes back on any tap — a tap on the desktop both shows the chrome and still clicks the PC at that point.
- The composer stays clear of the gesture navigation bar, and stays visible while the keyboard is up or a drawer is open. Rotating the phone brings the chrome back.
- Honest limit: worst case, stopping the stream takes two taps when chrome is hidden (tap to show, then tap stop); the stop action itself is unchanged.
- Version stamp 100.042 / code 68.

v100.041 — 2026-11-10
- Messages no longer wait up to 30 seconds on the PC health check.
- Lost replies are re-checked after ~5 seconds instead of 40.
- Dispatch is faster.
- Version stamp 100.041 / code 67.

v100.040 — 2026-11-10
- Cloudflare is gone completely: no more tunnel, no more cloudflared — the relay is Tailscale or LAN only.
- If your stored pairing still points at a tunnel address, the phone skips it and connects over Tailscale or LAN. If the stored pairing has nothing but the tunnel, the phone asks you to pair again instead of connecting.
- On the PC, a stored tunnel address is replaced by the LAN address on load, and upgraded to the Tailscale address at startup when it can be found.
- Honest limit: this does not fix the phone's Tailscale — that still needs a sign-in / stop-start on the phone, and until Tailscale works the phone can only reach the PC over LAN.
- Version stamp 100.040 / code 66.

v100.039 — 2026-11-10
- Chat messages now carry a send timestamp: the phone stamps sent_at when a message is sent, the PC stamps received_at on receipt, and both ride on the turn record (history rows + runtime-stream chat event) so the send-to-arrival delay splits into phone→PC and PC→cloud legs.
- The phone shows the send time under each sent bubble (HH:mm:ss, phone local time). Assistant bubbles are unchanged.
- Honest limit: phone and PC clocks are both NTP-synced in practice; 1–2 s of skew is fine for a minutes-scale delay question — no offset correction in v1.
- Honest limit: partials are NOT populated by the service yet — a long dispatching turn still shows silence until a partials version lands.
- Version stamp 100.039 / code 65.

v100.038 — 2026-11-10
- Tunnel re-mint can now actually succeed: the manager reads cloudflared's stderr as well as stdout, and extracts the tunnel URL from inside the box cloudflared prints it in. The old code only read stdout and required the URL alone on a line, so two live mint attempts timed out at 30 s with no URL found.
- The 3-in-24h mint cap resets once when the fixed logic first runs — the spawns consumed by the broken logic do not block the fixed mint; after the reset the cap applies normally.
- Windows-only behaviour change (Android = version stamp only).
- Honest limit: the live mint itself is a PC live test — the VM proves the URL extraction and the cap reset, not a real cloudflared spawn.
- Version stamp 100.038 / code 64.

v100.037 — 2026-11-10
- Worker-disconnected watchdog — Deskhand now notices within ~3 seconds when the Muse worker disconnects, and says so: "Worker disconnected since HH:MM, last attempt HH:MM, next in Ns", with a Reconnect button. Automatic reconnect retries on a backing-off schedule (2s/5s/10s/30s/60s, max 10 attempts) and then stops — the button stays available.
- A turn that was dispatching when the worker dropped is marked uncertain, never silently cleared and never re-sent. A message sent while the worker is down is refused immediately instead of sitting in "dispatching" for 10 minutes; the text stays as a draft to re-send.
- The worker line shows on the phone too (same state, via the runtime stream).
- Reconnect never restores a grant, never reuses an approval, and never touches the tunnel. There is no STOP latch in this version, so there is no STOP behaviour to describe.
- Honest limit: there is no gateway /status probe in this app — detection reads the worker transport's own ready state, not a gateway HTTP poll. If the gateway listener itself dies while the app stays up, that is a separate gap (gateway-listener recovery is a follow-up, not this version).
- Version stamp 100.037 / code 63.

v100.033 — 2026-11-10
- Agent message board: subagent posts appear in a new "Message board" panel below Runtime events on Windows, and as a "Board" tab in the Agents right drawer on Android. See what each agent was asked, when it started, and what it concluded.
- Live step notes (board.post) appear within ~2s, labelled "live". Outside-AI relay posts are quoted verbatim, labelled "relayed", with their source chat and said vs relayed time.
- The board is read-only — tapping a post expands it in place; it never approves, cancels, restarts, or messages anything.
- Honest gap, not claimed live: the event leg depends on the vm_events.jsonl feed being flushed to the PC (queued lines on the VM are awaiting an attended flush), so until a flush is proven on the PC the board shows the honest empty state and the footer says so.
- Note: the Jett-journal prune gap (E17 §2 #3) is unchanged — the prune does NOT execute (trigger conflict); this version does not fix it.
- Not yet verified live on a phone — the phone leg runs at the chain-end install.
- Version stamp 100.033 / code 59.

v100.030 — 2026-11-10
- AI desktop control: right-click, extended key chords (ctrl+shift+o opens a fresh chat, alt+tab, and more), and key hold/release with auto-release on task end, STOP, approval expiry, or any error.
- Verified evidence for move, drag, scroll, and key ops — the cursor is read back after a move, and a key is never "done" on "sent" alone.
- Move retry: the gate stays. The SS9.4 diagnosis is written (ss94-move-diagnosis.md), but the single bounded move retry is NOT enabled in this version — there is no recovery module to enable it in. Moves are single-attempt; a failed move is blocked.
- Over-long text is now refused with a clear error instead of silently truncated.
- window.wait_for now works end to end (host poll, bounded 30s).
- screenshot.monitor is parser-only in this version — host execution is deferred (no monitor selector after the repo fix removed BoundsFor()).
- Deferred (NOT in this version): router classify() entries, recovery decide() retry path, benchmark lying-host harness, screenshot.monitor host execution.
- Not in this version: middle-click, full IME (v2), UAC/secure-desktop/lock-screen control, fallback typing/pointer (gateway/host-only).
- Version stamp 100.030 / code 56.
- Note: the Jett-journal prune gap (E17 §2 #3) is unchanged — the prune does NOT execute (trigger conflict); this version does not fix it.
- Not yet verified live on a phone — the phone leg runs at the chain-end install.

v100.029 — 2026-11-10
- Share diagnostics now includes a [logcat] section: the phone's own recent log lines for the Deskhand app (INFO and above, last 200 lines), scrubbed on the phone before sharing — tokens are redacted the same way the PC scrubs its tails. If the log buffer has already rotated and nothing was captured, the share says so instead of sending an older capture. DEBUG lines are never in the phone share. If the session is down, the button still says so instead of sending a stale bundle.
- The Agents drawer footer now shows health, idle age, and configured log level per runtime source (agent, lifecycle, bridge, tunnel) — e.g. "agent: OK · idle 12s · INFO". A source that is down is named as down, never with a stale age dressed up as fresh, and a source with no reading yet says so.
- Log levels and structure landed: the service journal, event stream, lifecycle, agent, restart, tunnel-watchdog, and agent-watchdog logs are levelled (DEBUG / INFO / WARN / ERROR) and the diagnostics export shows level counts — the bridge log stays unstructured, and there is still no C# app log file. DEBUG is a one-hour session, never a setting, and never appears in the journal or the phone share.
- Not yet verified live on a phone — the phone leg runs at the chain-end install.

v100.026 — 2026-11-10
- Android: the session notification now asks for permission the first time you connect (Android 13+), instead of silently never appearing. If you deny it, the session still works — you just won't see the notification, and the Viewer shows a banner with a link to turn it on.
- Android: the session notification now shows how long you've been connected, and has a Disconnect button that ends the session.
- Android: if the system restarts the session service with no live session, it now stops itself within 60 seconds instead of sitting there holding the wake lock.
- Android: the partial wake lock that keeps the session alive while the phone dozes is new in this version — no wake lock existed in the build before.

v100.025 — 2026-11-10
- New "Stop agent" button in the message bar: while any turn is running, it asks the PC to stop agent work (computer-use tasks) without stopping ordinary chat. Queued agent turns are cancelled; chat messages already queued are left alone.
- What it can and cannot stop, honestly: the PC reports the outcome in its own words under the message bar. Local assistants and the laptop-agent fallback cannot be interrupted mid-action — the running action finishes and the next one is refused. Muse can only be stopped while its request is still waiting to be picked up; once sent, it finishes. Codex is asked cooperatively and the PC says whether that was confirmed. Nothing here kills running work, and there is no STOP latch anymore.
- "Not enabled" now counts as stopped in the chat-stop confirmation, so it no longer says "not fully confirmed" when there was simply nothing to stop on that side.
- Open question O6 (what each adapter's stop actually kills) is still unanswered by the adapter owners; this version ships the contracts as verified in code, not as a new kill path.

v100.020 — 2026-11-10
- Fail-fast on known-dead paths: the app now refuses a message immediately — instead of leaving it spinning — when the path it needs is already known dead, and keeps your message as a draft.
- Not yet verified live on a phone — the phone leg runs at the chain-end install.

v100.018 — 2026-11-10
- The Agents drawer finally shows live agents: running subagents appear with counting runtimes and their full task, and recently finished ones appear under "Recently finished". The feed refreshes every 5 seconds while the drawer is open.
- When no agents are running, the drawer says so honestly and shows the last activity instead of implying the feed is down. If the feed itself is not connected, or the feed data is bad, the drawer says that instead — it never shows agents that aren't there.
- A subagent's spawn/finish shows up a few minutes after it happens (the feed runs once a minute) — the times shown are the true event times.
- Not yet verified live on a phone — the phone leg runs at the chain-end install.

v100.017 — 2026-11-10
- Drawer events now arrive by push over the relay socket — the PC forwards new runtime events the moment they land instead of the phone polling once a second. The 1s poll stays as an automatic fallback if push drops, and the drawer footer says which transport is live.
- Turn delivery is now acknowledged — the phone confirms receipt by seq and the host records the ack time per session.
- Gateway /events deferred — relay push ships alone in this version.

v100.016 — 2026-11-10
- New Diagnostics screen: tap "Diagnostics" under the version on Home to see the health of all 13 Deskhand components (Windows app, service, gateway, worker, relay, tunnel, node, laptop agent, bridge, grant, Muse sign-in, lifecycle, paired phone) with status, last-OK age, version, and error text. It refreshes on the same 2.5s session poll as the Viewer — no second poll loop, no extra battery cost.
- Share diagnostics: shares the scrubbed plain-text bundle (health rows plus the last 50 log lines, tokens redacted on the PC before anything reaches the phone) through the share sheet. Nothing is written to storage. If the session is down, the button says so instead of sending a stale bundle.
- If the session drops, the screen keeps showing the last snapshot with a banner saying when it was taken. The Approvals section is reserved and shows "no approvals recorded" until the approvals feature lands.
- This screen is new in this build and has not yet been verified live on a phone — the PC unit tests and live checks run at deploy time.

v100.013 — 2026-11-09
- The red STOP button now stops the desktop video stream and returns the PC display to landscape, then takes you back to Home. It no longer has any safety role (the PC STOP latch was already removed in 100.012).
- The square button in the message bar now restarts the chat session fresh: it stops the running AI turn, clears the queued messages and the on-phone chat state (including any half-typed draft), and starts a new session. The plain stop-chat path is unchanged in settings.
- The desktop now opens fit-to-screen (whole desktop visible) at every stream start, on connect and reconnect, and stays fit when you rotate the phone. Your stored per-PC zoom is kept underneath — pinch, drag, or tap Fit off to return to it.
- Bigger fonts and icons across the whole app (about 1.2x): body text 17sp, titles and headings larger, composer and drawer icons larger. Touch targets stay 48dp. Note: this enlarges the phone's own text and icons; the streamed Windows desktop itself is the PC's pixels and is unchanged — pinch to zoom into it.
- Muse is now always on and ready: no more "Start connection" / "Resume Muse" / "Resume chat" steps. The connection starts itself, resumes itself after Muse sign-in, and re-establishes itself if it drops. "Stop connection" stays as the deliberate off-switch, and signing in to Muse is still a manual step in the Muse window.

v100.011 — 2026-11-09
- Phone video enlargement: the desktop view now opens zoomed in at 1.5x by default (adjustable 1.25x–2.0x with pinch), so text and buttons are bigger on the phone screen. Pinch to zoom, drag to pan, or tap Fit to see the whole desktop.
- Pan/Control toggle: Pan mode drags the view, Control mode drags the remote mouse (the default, as before). Tap always clicks in both modes.
- The phone now remembers the zoom and pan for each paired PC — a returning PC opens where you left it. "Zoom for this PC" and "Default zoom for new PCs" are in the Viewer settings, with a Reset zoom action.
- Sharpen is on by default (a subtle contrast lift on the zoomed video); turn it off in the Viewer settings if it looks worse.

v100.010 — 2026-11-09
- Version label is now easy to see: it moved from the bottom-right corner of the window (small grey text, easy to miss on a tall screen) to the top-right, next to the status line — larger, bold, dark text. It still opens this changelog when tapped.

v100.009 — 2026-11-08
- Frontend polish (look only, no behaviour change): one shared design system — muted teal/grey palette, one type scale (28sp title / 14sp body / 12sp small), consistent 16/8/6dp spacing, 8dp button radius, 48dp touch targets, bordered flat cards.
- Status states now read by colour + dot: green connected, amber needs attention, red only for failed — same wording as before.
- Version label stays tappable to open this changelog; changelog dialog title and padding tidied.
- Android: new shared DeskhandTheme.kt tokens (light theme only; dark values specified for future theme work, not applied).

v100.008 — 2026-11-09
- Batch 3 computer-use smarts:
- Fused grounding: screenshot + UI tree arrive as one observation with short-lived, task-scoped element IDs (15s). The AI points at IDs; the host resolves them to coordinates at execution time.
- Structured action channel: typed deskhand.task / propose / proposal / plan / receipt / ack / done messages replace exactly-one-JSON chat bubbles as the action channel. Grants are fixed at task open and validated strictly (one family per grant, task scope, 30-min max, STOP must be clear). The chat validator stays as backstop until the channel proves live.
- Plan ahead: the AI can propose a whole plan; the host approves it, steps still execute one by one with receipts. Independent read-only steps may be batched. Writes stay serial and phone-approved.
- Verified receipts: every receipt carries machine-checkable evidence (launch PID, write read-back match, type read-back, move destination, invoke post-observe, command exit code). A completed action without verified evidence is UNCERTAIN — never upgraded, never replayed.
- Recovery playbook: one bounded retry after a fresh re-observe for activate-declined / capture timeout / stale-or-denied. Move failures stay BLOCKED until the host diagnosis lands. Foreground lockout is never worked around. Uncertain is never replayed.
- App/file index: searchable index of installed apps and recent files (in-scope only, no file contents), so the AI stops listing folders one by one.
- Step router: each computer-use step is classified (grounding / plan / code / lookup / chat), hardware is probed (unknowns stay unknown), and plan/code steps need a strong route — otherwise blocked, not guessed.
- Resumable plans: checkpoints after each verified step; resume needs a fresh task, fresh grant, and fresh phone approval. Uncertain steps are never saved as done and never replayed. Tokens are invalid on resume.
- New tools: window.wait_for, clipboard (own grant family, contents never logged), filesystem.search, app launch by name (host app map only), screenshot.monitor.
- Desktop memory: Phil-approved facts only, with denied categories (dad's health, legal case, debts, family money fights) refused outright. Session snapshots are in-memory only and expire in 15 minutes.
- Skills: reviewed, versioned step sequences (Notepad write-verify, Calculator type-verify included). Every step still needs its grant; destructive steps need phone approval; a step without verified evidence stops the skill.
- Benchmark: fixed 5-task harness, disposable scope only, with a lying-host mode that proves verification catches false success.
- Multi-monitor: capture can select a monitor (0 = primary, the default). Old primary-only behaviour is unchanged for callers that never select.
- Unchanged by design: STOP semantics (phone STOP does not revoke host action authority) — Phil/Codex decision, not changed here.
- Muse-free direction — not decided here.
- Move retry stays disabled until the host diagnosis (SS9.4) is written.

v100.007 — 2026-11-09
- Batch 2 reply speed: replies stream token-by-token (Ollama SSE, Codex item/delta); the phone renders the reply as it arrives instead of waiting for the whole turn.
- Model router: trivial messages take the fast local route, hard ones take the smart route (UI override in settings, always-smart option).
- Independent read-only turns on different routes now run in parallel; anything that can change the PC stays strictly serial.
- Ollama models stay warm for 30 min after a turn; local models warm up at start so the first reply is not a cold load.
- New read-only runtime event feed (events / timing ops) powers the runtime panel and per-hop reply timing; gateway status is cached up to 5s on the fallback route.
- Phone router controls now work: the host allowlist was extended to the new read-only runtime ops (runtime_stream, route_suggest, router_settings, timing, warm_start), so the Android router UI reaches the service instead of being refused.
- Agents drawer live feed: NOT in this version — the runtime event feed is live server-side, but the drawer fetch and the subagent event supply it needs are not wired yet, so the drawer still shows its empty state.

v100.006 — 2026-11-09
- Batch 1 quick wins: warm Codex process (one per route, no fresh process per turn), per-account catalog cache (1 hour), catalog refresh passthrough, warm-process cleanup on shutdown.
- Stuck-turn watchdog (closes a 100.002 gap): a plain chat turn with no reply for 2 minutes is interrupted instead of showing "Waiting for assistant..." for the full 10-minute relay bound.

v100.005 — 2026-11-09
- Agents drawer rebuilt as a faithful copy of the file explorer drawer: same half-screen width (resizable by dragging its inner edge), same slide in and out (mirrored to the right edge), same layout — text Refresh and Close up top, "Muse agents" title, search box, agent rows with icons. Tap the ">" tab or swipe left from the right edge to open; the file drawer opens from its folder tab or a swipe right from the left edge. The drawer still shows the honest empty state until the agents feed lands in 100.007.

v100.004 — 2026-11-08
- Agents drawer fix: the drawer was unreachable — the app opens straight into the desktop viewer on launch, where there was no button. There is now an "Agents" tab on the right edge of the viewer (tap it, or swipe left from the right edge on any screen), plus the Home button. The drawer is now dark like the file explorer panel (black background, light text) and slides in and out smoothly (tween 300 ms, FastOutSlowInEasing). The drawer still shows the honest empty state until the agents feed lands in 100.006.

v100.003 — 2026-11-08
- Agents drawer: a right-side panel on the Home screen showing how many Muse agents are running, what each one is doing, and its runtime. Tap "Agents • N running" under the version number to open it.

v100.002 — 2026-11-08
- Version number now shows in both apps (Windows bottom-right, Android home screen) and is clickable to open this changelog.
- Windows: relay health line (last OK time, warns when the tunnel is down), embedded-node status, device list only rebuilds when devices change, settings apply without a full restart unless the relay changed, roomier layout.
- Windows: new tray icon (dark jet sphere, glowing core).
- Android: session stays alive in the background (foreground service), notification permission, version shown on the home screen.
- Specs: fixed the embedded-node test crash (missing Windows compatibility manifest).
- Tunnel watchdog: detects when the public relay URL goes stale and logs it.
- Dependency hash manifest: every key file fingerprinted for provenance.
- Runtime activity panel: now carries a timestamp so stale data can be flagged.
- In-flight guard wired: restarts wait for a turn to finish instead of being refused outright.
- Laptop agent: request counter added, so we can prove the agent was or wasn't called.
- F21 diagnosed: the flaky Desktop listing is a reply-ordering race (fix queued for Codex).

v100.001 — 2026-11-08
- First version label on the Windows app (bottom-right).
- Full PC access always-on; latch removed; sign-in deadlock fixed.
- Runtime activity panel added to the main window.
- Laptop-agent fallback: when the gateway worker is down, actions route through the laptop agent.
- Worker reconnect v2 + fallback hook v2 deployed.
"""
