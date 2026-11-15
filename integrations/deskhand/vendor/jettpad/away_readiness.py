"""Readiness metadata and an AC-only reversible system-sleep request."""
import ctypes,ipaddress,json,os,re,subprocess,time
from ctypes import wintypes
from companion_data import home,settings,paired_certificates
from tailscale_addresses import PHONE_TAILSCALE_IP

class SleepRequest:
    def __init__(self):self.active=False
    def update(self,enabled,on_ac):
        active=bool(enabled and on_ac)
        if active!=self.active:
            if os.name=='nt':
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.SetThreadExecutionState.argtypes=[wintypes.DWORD];kernel.SetThreadExecutionState.restype=wintypes.DWORD
                if not kernel.SetThreadExecutionState(0x80000001 if active else 0x80000000):raise OSError('Unable to request system awake')
            self.active=active
    def close(self):self.update(False,False)

def power():
    class Status(ctypes.Structure):
        _fields_=[('ac',ctypes.c_ubyte),('flag',ctypes.c_ubyte),('percent',ctypes.c_ubyte),('reserved',ctypes.c_ubyte),('life',wintypes.DWORD),('full',wintypes.DWORD)]
    value=Status()
    if os.name!='nt' or not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(value)):return None
    return value.ac==1

def snapshot(runtime):
    from musebridge.session_watch import interactive_session
    blocks=[];on_ac=power();local=runtime.local()
    if not settings().get('away_access',False):blocks.append('Enable Away access locally before leaving')
    if not settings()['chat']:blocks.append('Enable phone chat locally for Jett task requests')
    if on_ac is not True:blocks.append('Plug the laptop into AC power')
    if not interactive_session():blocks.append('Windows must stay signed in with an available desktop')
    if not settings()['remote_start']:blocks.append('Phone stream starts are disabled locally')
    try:
        if not paired_certificates():blocks.append('Pair the phone locally')
    except Exception:blocks.append('Pairing state unavailable')
    try:
        result=subprocess.run([str(__import__('pathlib').Path(os.environ['ProgramFiles'])/'Tailscale/tailscale.exe'),'status','--json'],capture_output=True,text=True,timeout=3,creationflags=0x08000000)
        vpn=json.loads(result.stdout);peer=next((p for p in vpn.get('Peer',{}).values() if PHONE_TAILSCALE_IP in p.get('TailscaleIPs',[])),{})
        network={'running':vpn.get('BackendState')=='Running','phone_online':peer.get('Online',False),'path':'unknown'}
        if network['running'] and network['phone_online']:
            try:
                route=subprocess.run([str(__import__('pathlib').Path(os.environ['ProgramFiles'])/'Tailscale/tailscale.exe'),'ping','--c','1','--timeout','2s','--until-direct=false',PHONE_TAILSCALE_IP],capture_output=True,text=True,timeout=3,creationflags=0x08000000)
                network.update(measured_path(route.stdout));network['path_probe_ts']=time.time()
            except (OSError,subprocess.TimeoutExpired):pass
        if not network['running']:blocks.append('Connect Tailscale on the PC')
        if not network['phone_online']:blocks.append('Phone is not online in Tailscale')
    except Exception:network={'running':False,'path':'unknown'};blocks.append('Tailscale status unavailable')
    try:
        browser=json.loads((home()/'browser-health.json').read_text());browser_state=browser.get('status','unknown')
        browser_ready=browser.get('cloud_ready') is True and 0<=time.time()-browser.get('ts',0)<15
    except Exception:browser_state='unknown';browser_ready=False
    chat_blocks=[] if browser_ready else ['Check Muse browser login for cloud replies; paired viewing does not require it']
    from viewing_access import blocked,enabled
    if blocked(local):blocks.append('Approve unattended phone viewing before leaving, or unlock legacy PC control')
    try:runtime.persistent_network_ready()
    except Exception as error:blocks.append(str(error))
    return {'ts':time.time(),'ready':not blocks,'blocks':blocks,'chat_blocks':chat_blocks,'viewing_independent':enabled(),'on_ac':on_ac,'network':network,'browser':browser_state,
            'authorization':{k:local.get(k) for k in ('day_authorized','day_authorization_expires_at','remote_unlocked','unlock_expires_at')}}

def measured_path(output):
    match=re.search(r'\bvia (.+?) in ([0-9.]+)ms',output)
    if not match:return {'path':'unknown'}
    route,delay=match.groups();kind='unknown'
    if route.startswith(('DERP(','peer-relay')):kind='relayed'
    else:
        try:
            ipaddress.ip_address(route.rsplit(':',1)[0].strip('[]'));kind='direct'
        except ValueError:pass
    return {'path':kind,'path_latency_ms':float(delay),'path_source':'single Tailscale ping; route may change'}
