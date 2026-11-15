"""Real device I/O. No TTS and no pre-wake recording or general transcription."""
import io
import queue
import threading
import time
import uuid


class Player:
    def __init__(self, emit):
        self.emit = emit
        self.lock = threading.RLock()
        self.stream = None
        self.blocked = True
        self.interrupted = threading.Event()

    def interrupt(self):
        self.interrupted.set()
        with self.lock:
            if self.stream:
                self.stream.abort()

    def play(self, encoded):
        import av
        import numpy as np
        import sounddevice as sd
        if self.blocked:
            return False
        self.interrupted.clear()
        with av.open(io.BytesIO(encoded)) as container:
            resampler = av.AudioResampler(format='fltp', layout='stereo', rate=24000)
            blocks = []
            for frame in container.decode(audio=0):
                for chunk in resampler.resample(frame):
                    blocks.append(chunk.to_ndarray().T.copy())
                    if sum(len(block) for block in blocks) > 24000 * 300:
                        raise ValueError('Voice clip exceeds five minutes')
            for chunk in resampler.resample(None):
                blocks.append(chunk.to_ndarray().T.copy())
        if not blocks:
            raise ValueError('Uploaded voice audio has no decodable samples')
        samples = np.concatenate(blocks).astype('float32')
        # Bound memory and speech duration as well as the existing 16 MiB upload budget.
        if len(samples) > 24000 * 300:
            raise ValueError('Voice clip exceeds five minutes')
        with self.lock:
            if self.interrupted.is_set() or self.blocked:
                return False
            self.stream = sd.OutputStream(samplerate=24000, channels=2, dtype='float32', latency='low')
            stream = self.stream
            stream.start()
        self.emit('state', voice_playing=True, voice_amplitude=0.0)
        last_level=0.0
        try:
            # Blocking 20ms writes keep pending output/barging latency bounded.
            for offset in range(0, len(samples), 480):
                if self.interrupted.is_set():
                    return False
                block = samples[offset:offset + 480]
                underflow = stream.write(block)
                if underflow:
                    self.emit('event', event='voice.output_underflow', reason='Audio device reported an output underflow')
                if time.monotonic()-last_level>=.1:
                    self.emit('state', voice_playing=True,
                              voice_amplitude=min(1.0, float(np.sqrt(np.mean(block ** 2)))))
                    last_level=time.monotonic()
            # stop(), unlike abort(), waits for all pending samples to drain.
            stream.stop()
            return not self.interrupted.is_set()
        finally:
            with self.lock:
                stream.close()
                self.stream = None
            self.emit('state', voice_playing=False, voice_amplitude=0.0)


class Listener:
    def __init__(self, emit, player, submit, stop):
        self.emit, self.player, self.submit, self.stop = emit, player, submit, stop
        self.muted = threading.Event()
        self.muted.set()
        self.stream = None
        self.model = None
        self.model_key = None
        self.lock = threading.RLock()
        self.settings = {}
        self.on_fault = lambda: None

    def mute(self):
        self.muted.set()
        with self.lock:
            if self.stream:
                self.stream.abort()
                self.stream.close()
                self.stream = None
        self.emit('state', mic_active=False, wake_armed=False, listening=False)

    def run(self):
        import numpy as np
        import sounddevice as sd
        from pocketsphinx import Decoder
        while not self.stop.wait(.1):
            if self.muted.is_set():
                continue
            try:
                from faster_whisper import WhisperModel
                key = (self.settings['stt_model'], self.settings['stt_device'])
                if key != self.model_key:
                    self.emit('event', event='voice.stt_loading', reason=str(key))
                    # Download is a local recognition asset; never upload microphone audio.
                    self.model = WhisperModel(key[0], device=key[1], compute_type='int8')
                    self.model_key = key
                if self.muted.is_set():
                    continue
                decoder = Decoder(lm=None, samprate=16000, loglevel='ERROR', kws_threshold=self.settings['wake_threshold'])
                if decoder.lookup_word('jett') is None:
                    decoder.add_word('jett', 'JH EH T', update=False)
                decoder.add_keyphrase('wake', 'hey jett')
                decoder.activate_search('wake')
                decoder.start_utt()
                blocks = queue.Queue(maxsize=20)

                def callback(data, frames, stamp, status):
                    if self.muted.is_set():
                        return
                    try:
                        blocks.put_nowait(bytes(data))
                    except queue.Full:
                        # Drop oldest transient detector buffer, never persist it.
                        try:
                            blocks.get_nowait()
                        except queue.Empty:
                            pass

                with self.lock:
                    if self.muted.is_set():
                        continue
                    stream = sd.RawInputStream(samplerate=16000, channels=1, dtype='int16', blocksize=320,
                                               latency='low', callback=callback)
                    self.stream = stream
                    stream.start()
                self.emit('state', muted=False, mic_active=True, wake_armed=True, listening=False)
                capture = None
                last_voice = None
                capture_start = None
                voiced_samples = 0
                while not self.stop.is_set() and not self.muted.is_set():
                    try:
                        block = blocks.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if capture is None:
                        decoder.process_raw(block, False, False)
                        if decoder.hyp():
                            detected = time.monotonic()
                            self.player.interrupt()
                            stopped_ms=round((time.monotonic() - detected) * 1000, 3)
                            self.emit('event', event='voice.wake_detected',
                                      reason='PocketSphinx hey jett; output abort returned in '+str(stopped_ms)+' ms')
                            decoder.end_utt()
                            capture, capture_start, last_voice = [], time.monotonic(), time.monotonic()
                            voiced_samples = 0
                            # Discard all detector buffers; only NEW post-wake samples go to STT.
                            while not blocks.empty():
                                blocks.get_nowait()
                            self.emit('state', listening=True, wake_armed=False)
                        continue
                    samples = np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768
                    capture.append(samples)
                    if float(np.sqrt(np.mean(samples ** 2))) > .012:
                        last_voice = time.monotonic()
                        voiced_samples += len(samples)
                    if time.monotonic() - last_voice < 1.1 and time.monotonic() - capture_start < 25:
                        continue
                    self.emit('state', listening=True, wake_armed=False)
                    text=''
                    if voiced_samples>=4000:
                        segments, info = self.model.transcribe(np.concatenate(capture), language='en',
                            beam_size=3, condition_on_previous_text=False, vad_filter=True)
                        text = ' '.join(s.text.strip() for s in segments
                                        if s.no_speech_prob<.5 and s.avg_logprob>=-1.0).strip()
                    else:
                        self.emit('event',event='voice.empty_wake',reason='No sustained post-wake speech; nothing transcribed or sent')
                    capture.clear()
                    # Mute wins over a transcription already in progress.
                    if text and not self.muted.is_set() and not self.stop.is_set():
                        self.submit(text, str(uuid.uuid4()))
                    capture = None
                    while not blocks.empty():
                        blocks.get_nowait()
                    decoder.start_utt()
                    self.emit('state', listening=False, wake_armed=not self.muted.is_set())
            except Exception as e:
                self.emit('event', event='voice.input_error', reason=type(e).__name__ + ': ' + str(e))
                self.mute()
                self.on_fault()
                # A fault cannot turn the device back on without a fresh local enable.
                self.emit('fault', reason='Listening stopped: ' + str(e))
            finally:
                with self.lock:
                    if self.stream:
                        self.stream.close()
                        self.stream = None
                self.emit('state', mic_active=False, wake_armed=False, listening=False)
