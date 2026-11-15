package com.deskhand.android

import android.content.Context
import android.content.SharedPreferences
import android.speech.tts.TextToSpeech
import java.util.Locale

// Quick Reply Mode (100.036): AI replies are text-only by default. A voice
// toggle in the composer speaks new AI replies aloud (TTS) when on, and
// stays silent when off. This is the REPLY side only — it never touches
// voice input (B28/D18/F52, mic-in / speech-to-text).
internal const val VOICE_REPLIES_PREFS = "voice-replies"
internal const val VOICE_REPLIES_KEY = "voice_replies_enabled"
internal const val VOICE_REPLY_UTTERANCE_ID = "deskhand-reply"

/** The persisted voice toggle. Default OFF — replies are text-only. */
internal fun voiceRepliesEnabled(prefs: SharedPreferences): Boolean =
    prefs.getBoolean(VOICE_REPLIES_KEY, false)

internal fun setVoiceRepliesEnabled(prefs: SharedPreferences, enabled: Boolean) {
    prefs.edit().putBoolean(VOICE_REPLIES_KEY, enabled).apply()
}

/**
 * Minimal seam over the platform TTS engine so the speaker logic is
 * unit-testable without a real TextToSpeech service.
 */
internal interface VoiceEngine {
    fun speak(text: String)
    fun stop()
    fun shutdown()
}

/** Real engine: Android TextToSpeech. The language is set lazily on first speak. */
internal class AndroidVoiceEngine(context: Context, onInit: (Int) -> Unit) : VoiceEngine {
    private val tts = TextToSpeech(context.applicationContext) { status -> onInit(status) }
    private var languageSet = false

    override fun speak(text: String) {
        if (!languageSet) {
            val result = tts.setLanguage(Locale.getDefault())
            if (result == TextToSpeech.LANG_MISSING_DATA || result == TextToSpeech.LANG_NOT_SUPPORTED) {
                tts.setLanguage(Locale.US)
            }
            languageSet = true
        }
        tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, VOICE_REPLY_UTTERANCE_ID)
    }

    override fun stop() {
        tts.stop()
    }

    override fun shutdown() {
        tts.shutdown()
    }
}

/**
 * Speaks new AI replies aloud while the voice toggle is on.
 *
 * The engine is created lazily by [enable] and torn down by [disable], so
 * with the toggle off no TTS object exists and nothing is ever spoken.
 * [speak] is a no-op without an engine (toggle off) or for blank text, and
 * a reply that arrives before the engine finishes initialising is held and
 * spoken once it is ready (latest reply wins).
 */
internal class VoiceSpeaker(
    private val context: Context,
    private val engineFactory: (Context, (Int) -> Unit) -> VoiceEngine = { ctx, onInit ->
        AndroidVoiceEngine(ctx, onInit)
    },
) {
    private var engine: VoiceEngine? = null
    private var ready = false
    private var pending: String? = null

    /** True once an engine exists (created lazily by [enable]). */
    val isStarted: Boolean get() = engine != null

    fun enable() {
        if (engine != null) return
        ready = false
        engine = engineFactory(context) { status ->
            ready = status == TextToSpeech.SUCCESS
            if (ready) pending?.let { text -> pending = null; speakNow(text) }
        }
    }

    fun disable() {
        engine?.stop()
        engine?.shutdown()
        engine = null
        ready = false
        pending = null
    }

    fun speak(text: String) {
        if (engine == null || text.isBlank()) return
        if (!ready) {
            pending = text
            return
        }
        speakNow(text)
    }

    private fun speakNow(text: String) {
        engine?.speak(text)
    }
}
