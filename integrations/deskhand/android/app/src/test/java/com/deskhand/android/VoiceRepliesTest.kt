package com.deskhand.android

import android.content.Context
import android.speech.tts.TextToSpeech
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class VoiceRepliesTest {
    private fun context(): Context = RuntimeEnvironment.getApplication().applicationContext

    private class FakeEngine : VoiceEngine {
        var onInit: ((Int) -> Unit)? = null
        val spoken = mutableListOf<String>()
        var stops = 0
        var shutdowns = 0
        override fun speak(text: String) {
            spoken.add(text)
        }
        override fun stop() {
            stops++
        }
        override fun shutdown() {
            shutdowns++
        }
    }

    private fun speakerWith(fake: FakeEngine): VoiceSpeaker =
        VoiceSpeaker(context()) { _, onInit -> fake.also { it.onInit = onInit } }

    @Test fun toggleDefaultsOffAndPersistsAcrossInstances() {
        val prefs = context().getSharedPreferences("voice-test-default", Context.MODE_PRIVATE)
        prefs.edit().clear().apply()
        // Default OFF on a fresh install.
        assertFalse(voiceRepliesEnabled(prefs))
        // Toggle on, then read it back through a new instance of the same
        // file — this is what an app restart sees.
        setVoiceRepliesEnabled(prefs, true)
        val reopened = context().getSharedPreferences("voice-test-default", Context.MODE_PRIVATE)
        assertTrue(voiceRepliesEnabled(reopened))
        setVoiceRepliesEnabled(reopened, false)
        assertFalse(voiceRepliesEnabled(context().getSharedPreferences("voice-test-default", Context.MODE_PRIVATE)))
    }

    @Test fun speakerCreatesNoEngineWhileDisabled() {
        val fake = FakeEngine()
        var factories = 0
        val speaker = VoiceSpeaker(context()) { _, onInit ->
            factories++
            fake.also { it.onInit = onInit }
        }
        assertFalse(speaker.isStarted)
        speaker.speak("hello")
        assertEquals(0, factories)
        assertTrue(fake.spoken.isEmpty())
    }

    @Test fun enableCreatesOneEngineAndSpeaksWhenReady() {
        val fake = FakeEngine()
        val speaker = speakerWith(fake)
        speaker.enable()
        speaker.enable()
        assertTrue(speaker.isStarted)
        fake.onInit?.invoke(TextToSpeech.SUCCESS)
        speaker.speak("hello")
        assertEquals(listOf("hello"), fake.spoken)
    }

    @Test fun replyBeforeInitIsHeldAndSpokenOnceReady() {
        val fake = FakeEngine()
        val speaker = speakerWith(fake)
        speaker.enable()
        speaker.speak("queued")
        assertTrue(fake.spoken.isEmpty())
        fake.onInit?.invoke(TextToSpeech.SUCCESS)
        assertEquals(listOf("queued"), fake.spoken)
        speaker.speak("next")
        assertEquals(listOf("queued", "next"), fake.spoken)
    }

    @Test fun latestPendingReplyWins() {
        val fake = FakeEngine()
        val speaker = speakerWith(fake)
        speaker.enable()
        speaker.speak("first")
        speaker.speak("second")
        fake.onInit?.invoke(TextToSpeech.SUCCESS)
        assertEquals(listOf("second"), fake.spoken)
    }

    @Test fun initFailureSpeaksNothingButToggleOffOnRetries() {
        val fake = FakeEngine()
        val speaker = speakerWith(fake)
        speaker.enable()
        speaker.speak("queued")
        fake.onInit?.invoke(TextToSpeech.ERROR)
        assertTrue(fake.spoken.isEmpty())
        speaker.speak("later")
        assertTrue(fake.spoken.isEmpty())
        speaker.disable()
        assertEquals(1, fake.stops)
        assertEquals(1, fake.shutdowns)
        assertFalse(speaker.isStarted)
        // Toggling back on creates a fresh engine (retry path).
        speaker.enable()
        assertTrue(speaker.isStarted)
        fake.onInit?.invoke(TextToSpeech.SUCCESS)
        speaker.speak("retry")
        assertEquals(listOf("retry"), fake.spoken)
    }

    @Test fun disableStopsShutsDownAndSuppressesFurtherSpeech() {
        val fake = FakeEngine()
        val speaker = speakerWith(fake)
        speaker.enable()
        fake.onInit?.invoke(TextToSpeech.SUCCESS)
        speaker.speak("one")
        speaker.disable()
        assertEquals(1, fake.stops)
        assertEquals(1, fake.shutdowns)
        assertFalse(speaker.isStarted)
        speaker.speak("two")
        assertEquals(listOf("one"), fake.spoken)
    }

    @Test fun blankTextIsNeverSpoken() {
        val fake = FakeEngine()
        val speaker = speakerWith(fake)
        speaker.enable()
        fake.onInit?.invoke(TextToSpeech.SUCCESS)
        speaker.speak("   ")
        speaker.speak("")
        assertTrue(fake.spoken.isEmpty())
    }
}
