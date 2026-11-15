"""Per-user background service. No network listener and no Jett-side supervision."""
import argparse
import os
from pathlib import Path
import signal
import threading
import time

from .runner import Runner
from .support import Ownership,atomic_json
from .state_service import StateService
from .chat_activity import ChatActivity,compose


class ConnectedState(StateService):
    """Crew may continue offline; the body still truthfully reports asleep."""
    connected=False
    base_state='idle'
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.audio_fields={}
        self.audio_fault=None
        self.chat_activity=None
        self.base_intensity=0
        self.executor_active=False
    def update(self,reason,**changes):
        with self.lock:
            if 'jett_state' in changes:self.base_state=changes['jett_state']
            if 'intensity' in changes:self.base_intensity=changes['intensity']
            changes.update(self.audio_fields)
            chat=self.chat_activity or {}
            changes['jett_state']=compose(self.base_state,chat.get('state'),self.connected,self.audio_fault,
                changes.get('listening',self.value['listening']),changes.get('voice_playing',self.value['voice_playing']))
            if chat.get('state') and changes['jett_state']==chat['state']:changes['intensity']=chat['intensity']
            else:changes['intensity']=self.base_intensity
            if not self.connected:changes.update(jett_state='asleep',seeing=False)
            if self.executor_active:
                urgent=('error' if self.audio_fault else
                    'listening' if changes.get('listening',self.value['listening']) else
                    'speaking' if changes.get('voice_playing',self.value['voice_playing']) else
                    'error' if self.base_state=='error' else
                    'needs_phil' if self.base_state=='needs_phil' or chat.get('state')=='needs_phil' else
                    'blocked' if self.base_state=='blocked' else None)
                soft=(chat.get('state')=='blocked' and chat.get('source')=='browser' and
                    (chat.get('reason','').startswith('Prompt queued; browser waiting:') or
                     chat.get('reason')=='Prompt sent; waiting for the external reply'))
                if urgent:changes['jett_state']=urgent
                elif changes['jett_state'] in ('idle','asleep','done_pulse') or soft:
                    changes.update(jett_state='thinking',intensity=55,progress=None)
            return super().update(reason,**changes)
    def executor_update(self,active):
        with self.lock:
            if self.executor_active!=active:
                self.executor_active=active
                return self.update('Actual local executor job started/stopped')
    def audio_update(self,**changes):
        with self.lock:
            self.audio_fields.update(changes)
            return self.update('Actual audio device state changed')
    def chat_update(self,activity):
        with self.lock:
            if activity==self.chat_activity:return
            self.chat_activity=activity
            self.journal('chat.activity',activity['reason'],activity['ref'])
            return self.update('Actual Pad queue activity changed')
    def connection(self,connected):
        self.connected=connected
        return self.update('Bridge connectivity changed',seeing=False)
    def set_audio_fault(self,reason):
        with self.lock:
            self.audio_fault=reason
            return self.update('Audio fault cleared' if reason is None else reason)



def main():
    parser=argparse.ArgumentParser(description='Jett local preparation crew')
    parser.add_argument('--documents',type=Path,default=Path.home()/'Documents')
    parser.add_argument('--data',type=Path,default=Path(os.environ.get('LOCALAPPDATA',Path.home()))/'JettCrew')
    parser.add_argument('--stop',action='store_true');parser.add_argument('--status',action='store_true')
    args=parser.parse_args();args.data.mkdir(parents=True,exist_ok=True)
    if args.status:
        print((args.data/'runtime.json').read_text() if (args.data/'runtime.json').exists() else 'Not started');return
    if args.stop:
        atomic_json(args.data/'stop-request.json',{'ts':time.time()});return
    guard=Ownership(args.data/'service.lock')
    if not guard.acquire():return
    state_guard=Ownership(args.documents/'jett-body-state-owner.lock')
    if not state_guard.acquire():
        guard.close();raise RuntimeError('Another state publisher owns the body feed; integrate before starting crew')
    args.data.joinpath('stop-request.json').unlink(missing_ok=True)
    stop=threading.Event();state=ConnectedState(args.documents);runner=None
    from .audio_supervisor import AudioSupervisor
    audio=AudioSupervisor(args.documents,state)
    chat=ChatActivity(Path(os.environ.get('LOCALAPPDATA',Path.home()))/'JettPad')
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    if hasattr(signal,'SIGINT'):signal.signal(signal.SIGINT,lambda *_:stop.set())

    def monitor():
        last_bridge=-float('inf')
        connected=False
        current={}
        while not stop.is_set():
            try:
                from musebridge.cli import default_home
                from musebridge.lifecycle import local_request
                if time.monotonic()-last_bridge>=.25:
                    last_bridge=time.monotonic()
                    current=local_request(default_home(),timeout=1)
                    connected=bool(current.get('connected'))
            except Exception:connected=False;current={}
            try:
                if connected!=state.connected:
                    state.connection(connected)
                state.update('Actual built-in screen capture activity',seeing=bool(current.get('screen_access_active',False)) if connected else False)
                state.executor_update(bool(current.get('executor',{}).get('running')))
                activity=chat.poll()
                if activity!=state.chat_activity:
                    state.chat_update(activity)
                    atomic_json(args.data/'chat-activity.json',dict(ts=time.time(),**activity))
                try:
                    audio.poll()
                except Exception as exc:
                    state.journal('voice.supervisor_fault',type(exc).__name__+': '+str(exc))
                state.publish()
            except OSError as e:
                fault=str(e)
                try:
                    state.journal('crew.monitor_fault',fault)
                except Exception as je:
                    combined_reason=f'{fault}; journaling failed: {str(je)}'
                    if runner:
                        try:
                            runner.status('error',reason=combined_reason)
                        except OSError:
                            pass
            if (args.data/'stop-request.json').exists():
                if runner:runner.stopping=True
                stop.set()
            stop.wait(.1)

    watcher=threading.Thread(target=monitor,name='crew-state',daemon=True)
    try:
        runner=Runner(args.documents,args.data,state)
        runner.recover();state.journal('crew.service_started','PC-side local crew; no raw execution, no Jett-side supervision')
        runner.status('idle');state.publish();watcher.start()
        backoff=1
        while not stop.is_set():
            try:
                runner.cycle();backoff=1
            except Exception as exc:
                reason=type(exc).__name__+': '+str(exc)
                state.journal('crew.service_error',reason)
                state.update(reason,jett_state='error',crew=[],progress=None)
                state.publish();runner.status('error',reason=reason)
                runner.recover()
                backoff=min(30,backoff*2)
            stop.wait(backoff)
    finally:
        stop.set()
        if watcher.is_alive():watcher.join(3)
        audio.close()
        chat.close()
        if runner:
            runner.stopping=True;runner.recover()
            runner.status('stopped');runner.close()
        state.journal('crew.service_stopped','Local crew stopped; PC bridge/Pad and Jett untouched')
        state.update('Crew stopped',jett_state='idle' if state.connected else 'asleep',crew=[],progress=None)
        time.sleep(.26);state.publish()
        state_guard.close();guard.close()


if __name__=='__main__':main()
