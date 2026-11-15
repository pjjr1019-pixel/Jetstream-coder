package com.deskhand.android

/**
 * Transport classification + ordered fallback (100.015, spec §11).
 *
 * Pure Kotlin — no Android framework calls — so every function here is
 * unit-testable on the JVM (see TransportFallbackTest). The phone never
 * mints a tunnel and never writes a URL (§11.7); it only classifies the
 * address it is dialling, tries the QR's candidates in order, and
 * reports which transport is live.
 *
 * Classification mirrors RelayHealth on the PC (spec §11.1):
 *   100.64.0.0/10 (CGNAT, Tailscale) -> Tailscale
 *   RFC1918 IPv4 or loopback        -> Local relay
 *   anything else                    -> Relay (plain, no transport claim)
 *
 * 100.040: Cloudflare is removed — there is no Tunnel kind any more.
 * A tunnel URL falls through to Relay here, and isTunnelUrl() below is
 * the filter Model.connect uses to skip tunnel URLs so one is never dialled.
 */
enum class TransportKind { TAILSCALE, LOCAL, RELAY }

/** Classify a relay URL by its host. Unparseable / host-less URLs are Relay. */
fun classifyRelayUrl(url: String): TransportKind {
    val host = runCatching { java.net.URI(url.trim()).host }.getOrNull()?.lowercase() ?: return TransportKind.RELAY
    if (host == "localhost" || host == "::1" || host == "[::1]") return TransportKind.LOCAL
    val octets = host.split('.').mapNotNull { it.toIntOrNull() }
    if (octets.size == 4 && octets.all { it in 0..255 }) {
        val (a, b) = octets
        if (a == 100 && b in 64..127) return TransportKind.TAILSCALE
        if (a == 10 || a == 127 || (a == 172 && b in 16..31) || (a == 192 && b == 168)) return TransportKind.LOCAL
    }
    return TransportKind.RELAY
}

/**
 * True when the URL's host is a Cloudflare tunnel address (100.040).
 * The tunnel kind is gone from the enum, so this helper — not
 * classifyRelayUrl — is how callers detect a tunnel URL. Model.connect
 * filters these out of the candidates so a tunnel URL is never dialled.
 */
fun isTunnelUrl(url: String): Boolean {
    val host = runCatching { java.net.URI(url.trim()).host }.getOrNull()?.lowercase() ?: return false
    return host.endsWith(".trycloudflare.com") || host == "trycloudflare.com"
}

/** The fixed §11.1 wording — do not invent new transport names. */
fun TransportKind.label(): String = when (this) {
    TransportKind.TAILSCALE -> "Tailscale"
    TransportKind.LOCAL -> "Local relay"
    TransportKind.RELAY -> "Relay"
}

/** While-connecting attempt text (§11.5): "Trying Tailscale…". */
fun TransportKind.tryingLabel(): String = "Trying ${label()}…"

/** Connected text (§11.5): "Connected via Tailscale". */
fun TransportKind.connectedLabel(): String = "Connected via ${label()}"

/**
 * Build the ordered candidate list from a pairing QR (§11.2):
 * [t, r, l] — Tailscale, primary, LAN; the primary is Tailscale or LAN,
 * never a tunnel (100.040) — deduped (blanks dropped,
 * exact duplicates dropped after trimming + trailing-slash normalising).
 * An old QR carrying only r= produces a one-item list.
 */
fun buildRelayCandidates(tailscale: String?, primary: String?, lan: String?): List<String> {
    val seen = LinkedHashSet<String>()
    listOf(tailscale, primary, lan).forEach { raw ->
        val cleaned = raw?.trim()?.trimEnd('/')?.takeIf { it.isNotBlank() } ?: return@forEach
        seen.add(cleaned)
    }
    return seen.toList()
}

/** Keep only ws:// / wss:// candidates — a stray non-ws value from a QR never reaches PcLink. */
fun validRelayCandidates(candidates: List<String>): List<String> =
    candidates.filter { it.startsWith("ws://") || it.startsWith("wss://") }

/** All-candidates-failed text (§11.5). 100.040: the candidate list is now at most two (no tunnel), so a shorter list says "all failed". */
fun noTransportLabel(tried: Int): String =
    if (tried >= 3) "No transport — all three failed. Retrying…" else "No transport — all failed. Retrying…"

/**
 * Base delay before a full-list retry after every candidate has failed
 * (§11.4: "the whole list is retried with the existing backoff").
 * Mirrors PcLink's jittered bounded backoff (1 s, doubling, 30 s cap);
 * the caller adds the jitter. Round 0 = first exhaustion.
 */
fun fullRetryDelayMs(round: Int): Long =
    minOf(30_000L, 1_000L shl round.coerceIn(0, 5))

/**
 * The ordered-fallback decision state (§11.4). Pure logic: Model.connect
 * drives a PcLink per candidate and asks this object what to do when an
 * attempt fails.
 *
 * Rules:
 * - An attempt that fails before any session advances to the next
 *   candidate; after the last candidate the list is exhausted.
 * - Once a session is live, a failure is a drop, NOT a hop: the first
 *   failure after a drop stays on the same transport (PcLink retries it
 *   itself); only when that same-transport reconnect also fails does
 *   the next candidate get tried — a fallback decision at reconnect
 *   time, never mid-session.
 * - [restart] starts a fresh full-list retry round from candidate 0.
 */
class OrderedFallback(val candidates: List<String>) {
    init { require(candidates.isNotEmpty()) { "OrderedFallback requires at least one candidate" } }

    var index = 0
        private set

    /** True while an encrypted session is established on the current candidate. */
    var sessionLive = false
        private set

    /** True once a session was established on any attempt of this connect sequence. */
    var sessionEverLive = false
        private set

    /** Consecutive socket failures since the last live session / relay-up. */
    var failuresSinceLive = 0
        private set

    val current: String get() = candidates[index]
    val currentKind: TransportKind get() = classifyRelayUrl(current)
    val isLast: Boolean get() = index >= candidates.size - 1

    fun markSessionLive() { sessionLive = true; sessionEverLive = true; failuresSinceLive = 0 }
    fun markSessionLost() { sessionLive = false }
    fun markRelayUp() { failuresSinceLive = 0 }

    /**
     * Called when the current candidate's socket fails.
     * @return the next candidate URL to try, or null: null means stay on
     * the current candidate (a drop whose same-transport retry has not
     * failed yet), or that the list is exhausted. Use
     * [shouldStayAfterFailure] to tell the two apart.
     */
    fun advanceAfterFailure(): String? {
        if (sessionEverLive && !sessionLive) {
            failuresSinceLive++
            if (failuresSinceLive < 2) return null
        }
        if (isLast) return null
        index++
        sessionLive = false
        // The new candidate never had a session: the drop grace does not
        // carry over — its first failure advances immediately.
        sessionEverLive = false
        failuresSinceLive = 0
        return current
    }

    /** True when advanceAfterFailure() returned null because the phone must stay on the current transport (drop, same-transport retry pending). False = the list is exhausted. */
    fun shouldStayAfterFailure(): Boolean = sessionEverLive && !sessionLive && failuresSinceLive < 2

    fun restart() {
        index = 0
        sessionLive = false
        sessionEverLive = false
        failuresSinceLive = 0
    }
}
