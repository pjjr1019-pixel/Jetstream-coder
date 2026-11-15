package com.deskhand.android

import okhttp3.Dns
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.net.InetAddress
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit

/**
 * Uses the phone's normal DNS first. If that fails (fresh tunnel names, filtered or broken DNS),
 * asks Cloudflare's DNS-over-HTTPS by IP address. Only the relay's hostname is ever looked up.
 */
object FallbackDns : Dns {
    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .build()

    override fun lookup(hostname: String): List<InetAddress> {
        try {
            return Dns.SYSTEM.lookup(hostname)
        } catch (e: UnknownHostException) {
            for (resolver in listOf("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve")) {
                val found = runCatching { doh(resolver, hostname) }.getOrDefault(emptyList())
                if (found.isNotEmpty()) return found
            }
            throw e
        }
    }

    // B2-G low (B13): ask for A and AAAA (an IPv6-only relay hostname
    // failed the fallback before) and URL-encode the hostname (a raw
    // `&`/`?` in a user-typed or QR-scanned hostname could inject extra
    // query parameters). System DNS is still tried first.
    private fun doh(resolver: String, hostname: String): List<InetAddress> {
        val encoded = java.net.URLEncoder.encode(hostname, "UTF-8")
        val result = mutableListOf<InetAddress>()
        for ((type, name) in listOf(1 to "A", 28 to "AAAA")) {
            val request = Request.Builder()
                .url("$resolver?name=$encoded&type=$name")
                .header("accept", "application/dns-json")
                .build()
            runCatching {
                client.newCall(request).execute().use { response ->
                    if (response.isSuccessful) {
                        val answers = JSONObject(response.body?.string().orEmpty()).optJSONArray("Answer")
                        if (answers != null) {
                            for (i in 0 until answers.length()) {
                                val a = answers.getJSONObject(i)
                                if (a.optInt("type") == type) result += InetAddress.getByName(a.getString("data"))
                            }
                        }
                    }
                }
            }
        }
        return result
    }
}
