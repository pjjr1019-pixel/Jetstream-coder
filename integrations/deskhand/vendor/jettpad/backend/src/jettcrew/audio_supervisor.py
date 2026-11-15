"""PC-only audio child ownership; all shared feed writes stay with the crew owner."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

HEALTHY_RESET_SECONDS = 600


class AudioSupervisor:
    def __init__(self, documents, state):
        self.documents, self.state = Path(documents), state
        self.pad = Path(__file__).resolve().parents[3]
        self.process = None
        self.rows = queue.Queue(maxsize=4096)
        self.last_start = -float('inf')
        self.closed = False
        self.failures = 0
        self.latest = dict(muted=True, mic_active=False, wake_armed=False, listening=False, voice_playing=False)
        self.last_runtime=0
        self.fault=None
        self.last_health=time.monotonic()
        self.seen_exit=False
        self.healthy_since=None

    def start(self):
        executable = self.pad / 'backend/.audio-venv/Scripts/python.exe'
        if not executable.exists():
            raise RuntimeError('Separate audio environment is missing')
        env = dict(os.environ, PYTHONPATH=str(self.pad / 'backend/src'))
        self.log = (self.pad / 'backend').joinpath('audio-errors.log').open('ab')
        self.process = subprocess.Popen([str(executable), '-u', '-B', '-m', 'jettvoice',
            '--documents', str(self.documents), '--data', str(Path(os.environ['LOCALAPPDATA']) / 'JettVoice'),
            '--pad', str(self.pad)], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        process = self.process
        def read():
            try:
                for line in process.stdout:
                    try:
                        self.rows.put({**json.loads(line),'_child_pid':process.pid}, timeout=2)
                    except (ValueError, queue.Full):
                        pass
            finally:
                process.stdout.close()
        threading.Thread(target=read, name='audio-events', daemon=True).start()
        self.last_start = time.monotonic()
        self.last_health = self.last_start
        self.seen_exit=False
        self.healthy_since=None
        self.fault=None
        self.state.set_audio_fault(None)
        self.state.journal('voice.child_started', 'PC-only audio pid=' + str(process.pid))

    def poll(self):
        if self.closed:
            return
        if self.process is None:
            self.start()
        elif self.process.poll() is not None:
            self.latest.update(voice_playing=False, voice_amplitude=0.0,
                               mic_active=False, wake_armed=False, listening=False)
            self.state.audio_update(**self.latest)
            if not self.seen_exit:
                self.state.journal('voice.child_failed','exit='+str(self.process.returncode))
                self.state.set_audio_fault('Audio child stopped unexpectedly')
                self.seen_exit=True
            if time.monotonic() - self.last_start < 30:
                from .support import atomic_json
                atomic_json(Path(os.environ['LOCALAPPDATA'])/'JettVoice/runtime.json',
                    dict(ts=time.time(),pid=None,status='Audio stopped; waiting to restart',**self.latest))
                return
            self.failures += 1
            self.state.journal('voice.child_failed', 'exit=' + str(self.process.returncode))
            self.log.close()
            if self.failures >= 3:
                self.closed = True
                self.state.journal('voice.needs_local_attention', 'Three audio crashes; automatic restarts suspended')
                return
            self.start()
        changes = {}
        stale_rows=0
        for _ in range(512):
            try:
                row = self.rows.get_nowait()
            except queue.Empty:
                break
            kind = row.pop('kind', None)
            child_pid=row.pop('_child_pid',None)
            if not self.process or child_pid!=self.process.pid:
                stale_rows+=1
                continue
            if kind == 'state':
                changes.update({k: v for k, v in row.items() if k != 'ts'})
            elif kind == 'event':
                self.state.journal(row['event'], row['reason'], row.get('ref'))
            elif kind == 'fault':
                self.fault=row['reason']
                self.state.set_audio_fault(row['reason'])
                self.state.journal('voice.fault', row['reason'])
            elif kind == 'health':
                now=time.monotonic()
                if self.healthy_since is None or now-self.last_health>30:
                    self.healthy_since=now
                self.last_health=now
                if self.failures and now-self.healthy_since>=HEALTHY_RESET_SECONDS:
                    self.failures=0
                    self.state.journal('voice.recovery_counter_reset',
                        'Current child supplied uninterrupted health for 600 seconds')
            elif kind == 'desktop_event':
                target = self.documents / 'jett-desktop-events.jsonl'
                with target.open('ab') as f:
                    f.write((json.dumps(row) + '\n').encode())
                    f.flush()
                self.state.journal('desktop.' + row['event'], json.dumps(row['data']), row['id'])
        if stale_rows:
            self.state.journal('voice.stale_child_rows_dropped',str(stale_rows)+' rows from non-current children discarded')
        if changes:
            if changes.get('mic_active') is True or changes.get('voice_playing') is True:
                self.fault=None;self.state.set_audio_fault(None)
            self.latest.update(changes)
            if self.latest.get('muted'):self.latest.update(mic_active=False,wake_armed=False,listening=False)
            self.state.audio_update(**self.latest)
        if self.process and self.process.poll() is None and time.monotonic()-self.last_health>30:
            # Kill an unresponsive child: OS closes its microphone and output handles.
            # Preserve playback as uncertain; a restarted service may not replay it.
            from jettvoice.common import atomic,load_settings
            settings=Path(os.environ['LOCALAPPDATA'])/'JettVoice/settings.json'
            values=load_settings(settings);values['muted']=True;atomic(settings,values)
            self.state.journal('voice.child_unresponsive','No main-loop heartbeat for 30s; muted locally and stopped')
            self.state.set_audio_fault('Audio child unresponsive')
            self.process.kill();self.process.wait(3)
            self.latest.update(muted=True,voice_playing=False,voice_amplitude=0.0,
                               mic_active=False,wake_armed=False,listening=False)
            self.state.audio_update(**self.latest)
        if time.monotonic()-self.last_runtime>=1:
            from .support import atomic_json
            status=('Muted - microphone closed' if self.latest['muted'] else
                'Listening to your request' if self.latest['listening'] else
                'Wake word armed' if self.latest['wake_armed'] else 'Preparing local recognition model')
            from jettvoice.common import load_settings
            settings=Path(os.environ['LOCALAPPDATA'])/'JettVoice/settings.json'
            warnings=[]
            config=load_settings(settings,warnings.append)
            if not self.latest['muted'] and not config['listening_enabled']:
                status='Speech ready - microphone disabled locally'
            if warnings and warnings[0]:status+='; '+warnings[0]
            if self.fault:status+='; '+self.fault
            atomic_json(Path(os.environ['LOCALAPPDATA'])/'JettVoice/runtime.json',
                dict(ts=time.time(),pid=self.process.pid if self.process else None,status=status,**self.latest))
            self.last_runtime=time.monotonic()

    def close(self):
        self.closed = True
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(b'{"command":"stop"}\n')
                self.process.stdin.flush()
                self.process.stdin.close()
                self.process.wait(8)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(3)
        if hasattr(self, 'log'):
            self.log.close()
        self.state.audio_update(voice_playing=False, voice_amplitude=0.0, mic_active=False,
                                wake_armed=False, listening=False)
