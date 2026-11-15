"""Local settings and lifecycle only. Chat never uses this API."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import subprocess
import time


class BridgeControl:
    def __init__(self, request=None, launch=subprocess.Popen):
        if request is None:
            from musebridge.cli import default_home
            from musebridge.lifecycle import local_request
            home=default_home()
            request=lambda action=None,payload=None: local_request(home,action,payload,timeout=3)
        self.request,self.launch=request,launch
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='jett-settings')

    def ensure_running(self):
        try: return self.request()
        except RuntimeError: pass
        python=Path(__file__).resolve().parent/'backend'/'.venv'/'Scripts'/'pythonw.exe'
        if not python.is_file(): raise RuntimeError('Jett Pad backend runtime was not found')
        self.launch([str(python),'-m','musebridge.cli','run','--no-window'],
                    cwd=python.parents[2],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            try: return self.request()
            except RuntimeError: time.sleep(.2)
        raise RuntimeError('PC controls did not start. Open Settings and retry.')

    def state(self): return self.request()

    def action(self, action, payload=None):
        allowed={'start','stop','restart','reconnect','ping_muse','configure','desktop_control','clear_stop','revoke_day_authorization','session',
                 'stop_all','connection_details','rotate_bridge_token','export_audit','shutdown'}
        if action not in allowed: raise ValueError('Unknown settings action')
        return self.request(action,payload or {}).get('result',{})

    def submit(self, function, *args): return self.executor.submit(function,*args)

    def close(self, stop_backend=False):
        if stop_backend: self.executor.submit(self.action,'shutdown')
        self.executor.shutdown(wait=False,cancel_futures=False)


def required_confirmations(state, changes):
    """Mirror existing local acknowledgements; the backend remains authoritative."""
    result={}
    features={**state.get('features',{}),**changes.get('features',{})}
    for key,phrase in (('exec','ENABLE UNSANDBOXED EXECUTION'),('screenshots','ENABLE SCREENSHOTS'),
                       ('desktop_control','ENABLE DESKTOP CONTROL')):
        if features.get(key) and not state.get('features',{}).get(key): result[key]=phrase
    old=state.get('scopes',{})
    previous=[Path(row['path']).resolve() for row in old.values() if 'exec' in row.get('permissions',[])]
    for name,scope in changes.get('scopes',{}).items():
        path=Path(scope['path']).resolve()
        if 'exec' in scope.get('permissions',[]) and not any(path==root or path.is_relative_to(root) for root in previous):
            result['exec_scope']='ENABLE UNSANDBOXED EXECUTION'
    if set(changes.get('desktop_apps',[]))-set(state.get('desktop_apps',[])):
        result['desktop_apps']='TRUST SCREEN CONTENT'
    if changes.get('tier')==2 and state.get('tier')!=2: result['confirmation']='ENABLE WHOLE DRIVE'
    return result


def merged_features(state, **values): return {**deepcopy(state.get('features',{})),**values}
