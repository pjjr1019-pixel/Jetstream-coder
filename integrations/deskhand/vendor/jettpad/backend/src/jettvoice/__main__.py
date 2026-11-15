"""Isolated audio child: stdin lifetime, stdout events, no network listener."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading
import time

from .audio import Listener, Player
from .common import Emitter, QuietWarningReporter, atomic, load_settings
from .events import Events
from .queue import VoiceQueue
from .route import known_reason, refresh_heard, submit


def quiet_now(settings, now=None):
    start, end = settings.get('quiet_start'), settings.get('quiet_end')
    if start is None or end is None:
        return False
    def minute(value):
        hour, minute = map(int, value.split(':'))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError('Invalid quiet hour')
        return hour * 60 + minute
    start, end = minute(start), minute(end)
    now = now or datetime.now()
    current = now.hour * 60 + now.minute
    return start <= current < end if start <= end else current >= start or current < end


def hotkey(stop, toggle, emit):
    import ctypes
    from ctypes import wintypes as w
    u = ctypes.WinDLL('user32', use_last_error=True)
    if not u.RegisterHotKey(None, 4214, 0x4000 | 0x0001 | 0x0002 | 0x0004, ord('M')):
        emit('fault', reason='Ctrl+Alt+Shift+M could not be registered')
        return
    emit('event', event='voice.hotkey_registered', reason='Ctrl+Alt+Shift+M')
    message = w.MSG()
    try:
        while not stop.wait(.02):
            while u.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                if message.message == 0x0312:
                    toggle()
    finally:
        u.UnregisterHotKey(None, 4214)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--documents', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--pad', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.pad / 'backend/src'))
    from jettcrew.support import Ownership
    from musebridge.voice_provenance import resolve
    args.data.mkdir(parents=True, exist_ok=True)
    guard = Ownership(args.data / 'service.lock')
    if not guard.acquire():
        raise RuntimeError('An audio service already owns playback/microphone')
    emit = Emitter()
    quiet_warning=QuietWarningReporter(emit)
    # Native extension imports must not race Windows' DLL loader across threads.
    # Import here, in order, before microphone/control/event threads exist.
    import numpy
    import sounddevice
    import pocketsphinx
    import pythoncom
    import win32gui
    import win32process
    from pycaw.pycaw import AudioUtilities
    stop = threading.Event()
    settings_path = args.data / 'settings.json'
    if not settings_path.exists():
        atomic(settings_path, load_settings(settings_path))
    settings_lock = threading.RLock()
    player = Player(emit)
    listener = Listener(emit, player,
        lambda text, identity: submit(text, identity, args.documents, args.pad, emit), stop)
    current_muted = None

    def mute(value):
        nonlocal current_muted
        player.blocked = value
        if value:
            player.interrupt()
            listener.mute()
        else:
            if load_settings(settings_path,quiet_warning)['listening_enabled']:
                listener.muted.clear()
        emit('state', muted=value)
        current_muted = value
        emit('event', event='voice.mute_changed', reason='Local mute=' + str(value))

    def toggle():
        with settings_lock:
            settings = load_settings(settings_path,quiet_warning)
            settings['muted'] = not settings['muted']
            # Apply mute synchronously, before any potentially slow disk operation.
            mute(settings['muted'])
            atomic(settings_path, settings)

    def fault_mute():
        with settings_lock:
            settings = load_settings(settings_path,quiet_warning)
            settings['muted'] = True
            mute(True)
            atomic(settings_path, settings)
    listener.on_fault = fault_mute

    def control():
        for line in sys.stdin:
            try:
                value = json.loads(line)
                if value.get('command') == 'stop':
                    break
            except ValueError:
                pass
        stop.set()
        player.interrupt()
        listener.mute()

    threads = [threading.Thread(target=control, daemon=True),
               threading.Thread(target=listener.run, daemon=True),
               threading.Thread(target=hotkey, args=(stop, toggle, emit), daemon=True)]
    for thread in threads:
        thread.start()
    home = Path(os.environ['LOCALAPPDATA']) / 'MuseBridge'
    pad_data = Path(os.environ['LOCALAPPDATA']) / 'JettPad'
    queue = VoiceQueue(args.documents / 'jett-voice-inbox', args.data,
        lambda path: resolve(home, path), emit,
        lambda side: known_reason(side, args.documents, pad_data))
    events = Events(emit)
    fullscreen = None
    last_events = 0
    last_heartbeat = 0
    playback = None
    emit('event', event='voice.service_started', reason='PC-side isolated audio; microphone defaults muted')

    def play(item):
        name, side, audio = item
        try:
            success = player.play(audio)
            # Playback bookkeeping belongs to main thread (SQLite connection).
            outcome.append((name, side, success, 'audio drained' if success else 'barge_in_or_mute'))
        except Exception as e:
            outcome.append((name, side, False, type(e).__name__ + ': ' + str(e)))
    outcome = []
    try:
        while not stop.wait(.05):
            try:
                if time.monotonic()-last_heartbeat>=1:
                    emit('health', status='running')
                    last_heartbeat=time.monotonic()
                with settings_lock:
                    settings = load_settings(settings_path,quiet_warning)
                listener.settings = settings
                if settings['muted'] != current_muted:
                    mute(settings['muted'])
                if not settings['listening_enabled'] and not listener.muted.is_set():
                    listener.mute()
                    emit('state',muted=settings['muted'])
                elif settings['listening_enabled'] and not settings['muted'] and listener.muted.is_set():
                    listener.muted.clear()
                if time.monotonic() - last_events >= 2:
                    try:
                        import pythoncom
                        pythoncom.CoInitialize()
                        facts = events.poll()
                        fullscreen = facts['fullscreen']
                    finally:
                        pythoncom.CoUninitialize()
                    last_events = time.monotonic()
                    refresh_heard(args.documents, pad_data)
                if playback and not playback.is_alive():
                    playback.join()
                    for name, side, success, reason in outcome:
                        queue.receipt(name, side, completed=success, reason=reason)
                    outcome.clear()
                    playback = None
                if playback:
                    continue
                candidates = queue.candidates()
                hold = 'muted' if settings['muted'] else 'fullscreen' if fullscreen else 'quiet_hours' if quiet_now(settings) else None
                emit('state', voice_waiting=bool(candidates and hold))
                if hold:
                    for item in candidates:
                        queue.hold(item, hold)
                elif candidates:
                    item = candidates[0]
                    queue.claim(item)
                    playback = threading.Thread(target=play, args=(item,), daemon=True)
                    playback.start()
            except Exception as e:
                emit('event', event='voice.service_error', reason=type(e).__name__ + ': ' + str(e))
                stop.wait(2)
    finally:
        stop.set()
        player.interrupt()
        listener.mute()
        if playback:
            playback.join(2)
        for name, side, success, reason in outcome:
            queue.receipt(name, side, completed=success, reason=reason)
        queue.close()
        guard.close()
        emit('event', event='voice.service_stopped', reason='PC audio stopped, queued work retained')


if __name__ == '__main__':
    main()
