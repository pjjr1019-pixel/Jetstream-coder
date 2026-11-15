"""Single-writer state snapshots and locked, append-only backend journal."""
import json
import os
from pathlib import Path
import threading
import time

STATES={'idle','listening','thinking','researching','coding','speaking','blocked','error','needs_phil','done_pulse','asleep'}


class StateService:
    def __init__(self,documents,clock=time.monotonic):
        self.root=Path(documents);self.root.mkdir(parents=True,exist_ok=True)
        self.clock=clock;self.last=-float('inf');self.dirty=True;self.lock=threading.RLock()
        self.value=dict(contract='1.1',ts=time.time(),jett_state='asleep',intensity=0,progress=None,
            crew=[],seeing=False,voice_playing=False,voice_amplitude=0.0,voice_waiting=False,
            listening=False,mic_active=False,wake_armed=False,muted=True,
            gaze=dict(kind='none',x=None,y=None,label=None))

    def journal(self,event,reason,ref=None):
        row=dict(ts=time.time(),who='backend',event=event,reason=reason,ref=ref)
        encoded=(json.dumps(row,ensure_ascii=True)+'\n').encode()
        with self.lock:
            # A companion lock coordinates future frontend/orb appenders.
            with (self.root/'jett-body-journal.lock').open('a+b') as guard:
                if os.name=='nt':
                    import msvcrt
                    guard.seek(0,2)
                    if not guard.tell():guard.write(b'0');guard.flush()
                    guard.seek(0);msvcrt.locking(guard.fileno(),msvcrt.LK_LOCK,1)
                try:
                    with (self.root/'jett-body-journal.jsonl').open('ab') as stream:
                        stream.write(encoded);stream.flush();os.fsync(stream.fileno())
                finally:
                    if os.name=='nt':guard.seek(0);msvcrt.locking(guard.fileno(),msvcrt.LK_UNLCK,1)

    def update(self,reason,**changes):
        with self.lock:
            candidate={**self.value,**changes}
            if set(changes)-set(self.value):raise ValueError('Unknown state field')
            if candidate['jett_state'] not in STATES:raise ValueError('Unknown state')
            for key in ('intensity','progress'):
                v=candidate[key]
                if key=='progress' and v is None:continue
                if type(v) not in (int,float) or not 0<=v<=100:raise ValueError('Invalid '+key)
            if candidate['muted'] and any(candidate[k] for k in ('mic_active','wake_armed','listening')):
                raise ValueError('Muted microphone cannot be active')
            if any(self.value[k]!=v for k,v in changes.items()):
                self.journal('state_change',reason)
                self.value=candidate;self.dirty=True

    def publish(self):
        with self.lock:
            now=self.clock()
            if not self.dirty:
                # Unchanged state: only republish after 5 seconds (heartbeat).
                if now-self.last<5.0:return False
                self.value['ts']=time.time()
                target=self.root/'jett-body-state.json';temporary=target.with_suffix('.json.new')
                with temporary.open('w',encoding='utf-8') as stream:
                    json.dump(self.value,stream);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,target)
                self.last=now;self.dirty=False
                return True
            # Actual change: respect 4-updates-per-second (0.25s) cap.
            if now-self.last<.25:return False
            self.value['ts']=time.time()
            target=self.root/'jett-body-state.json';temporary=target.with_suffix('.json.new')
            with temporary.open('w',encoding='utf-8') as stream:
                json.dump(self.value,stream);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,target)
            self.last=now;self.dirty=False
            return True
