"""Pad parking over private app files; this never grants desktop authority."""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value),encoding='utf-8')
    os.replace(temporary,path)


class AwayMode:
    def __init__(self, bridge, data=None):
        self.bridge=bridge
        self.data=Path(data) if data else Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))/'JettPad'
        self.lock=asyncio.Lock()

    def status(self):
        try:
            state=json.loads((self.data/'away-state.json').read_text())
            owner=json.loads((self.data/'application.json').read_text())
            active=state.get('active') is True and state.get('app_pid')==owner.get('pid') and owner.get('status')=='running'
            return {'active':active,'parked':active,'since':state.get('since') if active else None}
        except (OSError,ValueError):return {'active':False,'parked':False,'since':None}

    async def set(self, active, window_id=None):
        if type(active) is not bool:raise ValueError('active must be boolean')
        if not active and window_id is not None:raise ValueError('window_id applies only when entering Away')
        async with self.lock:
            self.bridge.desktop._rate_limit()
            if active:
                self.bridge.permissions.check_session('desktop')
                self.bridge.desktop._ensure_authority('desktop_control')
                if window_id is not None:self.bridge.desktop._target(window_id)
            owner=json.loads((self.data/'application.json').read_text())
            if owner.get('status')!='running':raise ValueError('Jett Pad is not running')
            identifier=str(uuid.uuid4());deadline=time.time()+3
            command={'id':identifier,'active':active,'app_pid':owner['pid'],'bridge_pid':os.getpid(),'expires':deadline}
            await asyncio.to_thread(atomic,self.data/'away-command.json',command)
            try:
                while time.time()<deadline:
                    try:
                        ack=json.loads((self.data/'away-ack.json').read_text())
                        if ack.get('id')==identifier:
                            if ack.get('error'):raise ValueError(ack['error'])
                            break
                    except (OSError,json.JSONDecodeError):pass
                    await asyncio.sleep(.04)
                else:raise ValueError('Pad parking was not confirmed; check Pad before retrying')
                result={'active':active,'parked':active,'foreground':None}
                if active:
                    # Recheck authority after the UI handoff; parking never bypasses it.
                    self.bridge.desktop._ensure_authority('desktop_control')
                    if window_id is None:
                        rows=await asyncio.to_thread(self.bridge.desktop.list_windows)
                        candidates=[row for row in rows['windows'] if not row['protected'] and row['state']!='minimized' and row['title']]
                        preferred=next((row for row in candidates if row['is_foreground']),None)
                        if preferred is None:preferred=next(iter(candidates),None)
                        if preferred:window_id=preferred['window_id']
                    if window_id:
                        try:
                            result['foreground']=await self.bridge._local_operation(self.bridge._desktop_lock,self.bridge.desktop.activate,window_id,desktop=True)
                        except ValueError as error:result['activation_error']=str(error)
                await self.bridge._audit('control.away','Pad parking',True,json.dumps(result))
                self.bridge.state_changed()
                return result
            except BaseException:
                # Expired commands cannot park a later app instance.
                try:
                    pending=json.loads((self.data/'away-command.json').read_text())
                    if pending.get('id')==identifier:(self.data/'away-command.json').unlink()
                except (OSError,ValueError):pass
                raise
