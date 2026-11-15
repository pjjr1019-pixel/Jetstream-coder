"""App-owned local test runtime. No network listener or remote start endpoint."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
import base64,hashlib,http.client,ipaddress,json,os,re,secrets,ssl,subprocess,sys,threading,time
from .policy import EndpointPolicy,sunshine_configuration
from .manager import Preflight,StreamManager
from .launcher import WinLauncher
from .windows import SuspendedOwnedProcess,StopSignal,ProcessWitness

from tailscale_addresses import PC_TAILSCALE_IP as LOCAL, PHONE_TAILSCALE_IP as PHONE
EXE_HASH='9aaefc1b7b421ec1db54569d7950bff75f10d6c4888ecab411f34351dbf5c7d2'

def atomic(path,value):
    temp=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        temp.write_text(json.dumps(value,indent=2),encoding='utf-8')
        for attempt in range(5):
            try:os.replace(temp,path);break
            except PermissionError:
                if attempt==4:raise
                time.sleep(.02)
    finally:
        if temp.exists():temp.unlink()

def read_shared_json(path):
    """Allow atomic replacement while reading; retry brief delete-pending races."""
    def opener(name,flags):
        import ctypes,msvcrt
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.CreateFileW(str(name),0x80000000,7,None,3,0,None)
        if handle==wintypes.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        try:return msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
        except Exception:kernel.CloseHandle(handle);raise
    for attempt in range(4):
        try:
            with open(path,encoding='utf-8-sig',opener=opener if os.name=='nt' else None) as stream:return json.load(stream)
        except (PermissionError,FileNotFoundError):
            if attempt==3:raise
            time.sleep(.02)

def private_material(folder:Path):
    """Runs as the local user; keys/password never leave the protected directory."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    if (folder/'manager-secret.json').exists():
        for name in ('host.key','host.crt','admin.json'):
            if not (folder/name).is_file():raise ValueError('Incomplete private pairing state; do not overwrite existing identity')
        return
    if any((folder/name).exists() for name in ('host.key','host.crt','admin.json')):
        raise ValueError('Partial private identity exists; review before regenerating')
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Jett Pad Sunshine Test')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=3650))
          .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(LOCAL))]),critical=False).sign(key,hashes.SHA256()))
    (folder/'host.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
    (folder/'host.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    password=secrets.token_urlsafe(32);salt=secrets.token_hex(8);username='jettpad-local'
    # Pinned Sunshine util::hex(sha256) serializes bytes reversed and uppercase.
    atomic(folder/'admin.json',{'username':username,'salt':salt,'password':hashlib.sha256((password+salt).encode()).digest()[::-1].hex().upper()})
    atomic(folder/'manager-secret.json',{'username':username,'password':password,'certificate_sha256':hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()})

class Admin:
    def __init__(self,folder):self.folder=folder;self.lock=threading.Lock()
    def request(self,path,body=None,method=None):
        if path not in ('/api/pin','/api/clients/list','/api/clients/unpair','/api/apps/close','/api/config'):
            raise ValueError('Unsupported local administration operation')
        secret=json.loads((self.folder/'manager-secret.json').read_text())
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.check_hostname=False;context.verify_mode=ssl.CERT_NONE
        with self.lock:
            conn=http.client.HTTPSConnection(LOCAL,47990,timeout=2,context=context)
            try:
                conn.connect()
                if hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()!=secret['certificate_sha256']:
                    raise ValueError('Local Sunshine certificate mismatch; credentials not sent')
                auth=base64.b64encode((secret['username']+':'+secret['password']).encode()).decode()
                headers={'Authorization':'Basic '+auth,'Content-Type':'application/json'}
                conn.request(method or ('POST' if body is not None else 'GET'),path,body=json.dumps(body) if body is not None else None,headers=headers)
                response=conn.getresponse();raw=response.read(256*1024)
                if response.status!=200:raise ValueError('Local Sunshine administration refused ('+str(response.status)+')')
                result=json.loads(raw)
                if (body is not None and result.get('status') is not True) or result.get('status') is False:
                    raise ValueError('Sunshine did not confirm the local action')
                return result
            finally:conn.close()

def values(value):return value if isinstance(value,list) else [value]

def sockets_ready(sockets,elapsed):
    required={('tcp',LOCAL,p) for p in (47984,47989,47990,48010)}
    if required.issubset(sockets):return True
    if elapsed>=45:raise ValueError('Sunshine startup timed out before its required exact-address sockets were ready')
    return False
def ip_ranges(value):
    result=[]
    for raw in values(value):
        raw=str(raw)
        if raw.lower()=='any':return ['any']
        if '-' in raw:
            first,last=raw.split('-',1);result.extend(ipaddress.summarize_address_range(ipaddress.ip_address(first),ipaddress.ip_address(last)))
        else:result.append(ipaddress.ip_network(raw,strict=False))
    return sorted(str(network) for network in ipaddress.collapse_addresses([r for r in result if r.version==4]))+sorted(str(network) for network in ipaddress.collapse_addresses([r for r in result if r.version==6]))

def validate_rules(expected,actual):
    by_name={r['name']:r for r in actual}
    if len(by_name)!=len(expected):raise ValueError('Expected exact managed firewall rule set')
    for rule in expected:
        found=by_name.get(rule['Name'])
        if not found or found['enabled']!='True' or found['action']!=rule['Action'] or found['direction']!=rule['Direction']:
            raise ValueError('Required firewall rule is missing or inactive')
        if Path(found['program']).resolve()!=Path(rule['Program']).resolve():raise ValueError('Firewall rule points to a different program')
        for key,prop in (('LocalAddress','local'),('RemoteAddress','remote')):
            if ip_ranges(found[prop])!=ip_ranges(rule.get(key,'Any')):raise ValueError('Firewall address scope changed: '+rule['Name'])
        for key,prop in (('LocalPort','local_ports'),('RemotePort','remote_ports')):
            if sorted(map(str,values(found[prop])))!=sorted(map(str,values(rule.get(key,'Any')))):
                raise ValueError('Firewall port scope changed: '+rule['Name'])
        if sorted(values(found['interface']))!=sorted(values(rule.get('InterfaceAlias','Any'))):raise ValueError('Firewall interface scope changed')
        protocol={'6':'TCP','17':'UDP','256':'Any'}.get(str(found['protocol']),str(found['protocol']))
        if protocol.lower()!=rule['Protocol'].lower():raise ValueError('Firewall protocol changed')

class Runtime:
    def __init__(self,root:Path):
        self.root=root;self.private=root/'private';self.private.mkdir(exist_ok=True)
        self.exe=root/'vendor/Sunshine/sunshine.exe';self.policy=EndpointPolicy(LOCAL,PHONE,'Tailscale',self.exe)
        self.admin=Admin(self.private);self.last_error='';self.launcher=None;self.signal=None;self.closed=False
        self.last_local=None;self.last_local_at=0.;self.last_stats={};self.started_at=0.;self.was_busy=False;self.observer=None;self.server_ready=False
        self.start_lock=threading.Lock();self.operation_lock=threading.RLock();self.expected_stop_epoch=None
        from musebridge.lifecycle import local_request
        self.local=lambda action=None,payload=None:local_request(Path(os.environ['LOCALAPPDATA'])/'MuseBridge',action,payload,timeout=2)
        self.manager=StreamManager(stop_latched=self.stopped,preflight=self.preflight,launch=self.launch,journal=self.journal)
        self.ui_heartbeat()
    def journal(self,event,reason):
        with (self.private/'events.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps({'ts':time.time(),'event':event,'reason':reason})+'\n')
    def ui_heartbeat(self):atomic(self.private/'ui-heartbeat.json',{'ts':time.time(),'pid':os.getpid()})
    def stopped(self):
        if self.signal:
            try:
                if self.signal.stopped():return True
            except OSError:return True
        from viewing_access import blocked
        return blocked(self.last_local)
    def probe(self,pid=0,network_only=False):
        args=['powershell.exe','-NoProfile','-NonInteractive','-File',str(self.root/'host/os_probe.ps1'),'-TargetPid',str(pid)]
        if network_only:args.append('-NetworkOnly')
        result=subprocess.run(args,capture_output=True,text=True,timeout=8,creationflags=0x08000000)
        if result.returncode:raise ValueError('Windows network/firewall verification failed')
        return json.loads(result.stdout)
    def persistent_admitted(self):
        try:
            paths=[self.exe,self.private/'persistent-admission.json',self.private/'persistent-evidence.json',
                self.private/'discovery.pcapng',self.root/'vendor/Sunshine/JETTPAD_BUILD.json']
            signature=tuple((p.stat().st_size,p.stat().st_mtime_ns) for p in paths)
            if signature!=getattr(self,'_admission_signature',None):
                from .admission import verify
                verify(self.root);self._admission_signature=signature
            return True
        except (OSError,ValueError):return False
    def persistent_network_ready(self):
        if not self.persistent_admitted():raise ValueError('Persistent away network has not passed its local discovery verification')
        observed=self.probe();validate_rules(self.policy.firewall_plan(),observed['rules'])
        if not observed['network']:raise ValueError('Tailscale is unavailable')
        return True
    def preflight(self):
        check=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',str(self.root/'host/check_private_acl.ps1'),'-PrivatePath',str(self.private)],capture_output=True,text=True,timeout=5,creationflags=0x08000000)
        if check.returncode or json.loads(check.stdout).get('restricted') is not True:raise ValueError('Prepare the restricted private directory locally before writing host keys')
        private_material(self.private)
        state=self.local();self.last_local=state;self.last_local_at=time.monotonic()
        from viewing_access import blocked
        if blocked(state):raise ValueError('Approve paired-phone unattended viewing in Phone & streaming, or clear the legacy control STOP locally.')
        observed=self.probe()
        if not observed['network']:raise ValueError('The exact Tailscale interface is unavailable')
        validate_rules(self.policy.firewall_plan(),observed['rules'])
        if self.persistent_admitted():
            self.persistent_network_ready()
        else:
            setup=read_shared_json(self.private/'admin-result.json')
            if setup.get('state')!='ready':raise ValueError('Prepare network and packet observation locally before starting')
            if self.observer:self.observer.close()
            self.observer=ProcessWitness(int(setup['pid']),int(setup['created']))
        (self.private/'sunshine.conf').write_text(sunshine_configuration(self.policy,self.private),encoding='utf-8')
        atomic(self.private/'apps.json',{'env':{},'apps':[{'name':'Desktop','cmd':'','exclude-global-prep-cmd':True,'auto-detach':True}]})
        return Preflight(self.policy,EXE_HASH,True,True,True)
    def launch(self,proof,owned):
        status=self.local('stream_status_local')['result']
        if not status.get('available') or status.get('armed'):raise ValueError('Live STOP publisher is unavailable or already leased')
        if self.expected_stop_epoch is not None and status['epoch']!=self.expected_stop_epoch:raise ValueError('STOP cancelled the queued start; tap Start again')
        with (self.private/'process.log').open('ab',buffering=0) as log:
            process=SuspendedOwnedProcess([str(self.exe),str(self.private/'sunshine.conf')],self.exe.parent,log)
        try:
            lease=self.local('stream_arm_local',{'epoch':status['epoch'],'pid':process.process.pid,'created':process.created,'privacy_warning_accepted':True})['result']
            self.signal=StopSignal(lease['event'])
            token=self.manager.status()['generation']
            self.launcher=WinLauncher(self.root,lease['event'],self.private/'process.log',self.private/'sunshine.conf',
                publisher_identity=lease,on_failure=lambda reason:self.manager.observed_failure(reason,token))
            self.launcher.launch_suspended(process,owned,self.exe,expected_sha256=EXE_HASH)
            self.started_at=time.monotonic();self.was_busy=False;self.server_ready=False
            self.journal('stream.process',f'pid={process.process.pid}; exact Tailscale bind requested; no measured encoder yet')
        except Exception:
            process.terminate_now()
            if self.signal:self.signal.close();self.signal=None
            try:self.local('stream_release_local')
            except Exception:pass
            raise
    def start(self,expected_generation=None,origin='local',expected_stop_epoch=None):
        if not self.start_lock.acquire(False):raise ValueError('A stream operation is already in progress')
        try:
            self.expected_stop_epoch=expected_stop_epoch
            if self.manager.status()['phase']!='off':raise ValueError('Streaming is already starting or running')
            expected=self.manager.status()['generation'] if expected_generation is None else expected_generation
            if self.signal:self.signal.close();self.signal=None
            self.last_local=self.local();self.last_local_at=time.monotonic()
            self.manager.start_from_local_ui(privacy_warning_accepted=True,expected_generation=expected,origin=origin)
        finally:self.start_lock.release()
    def stop(self,reason='Local stream OFF'):
        self.manager.stop_now(reason)
        if self.signal:self.signal.close();self.signal=None
        if self.observer:self.observer.close();self.observer=None
        try:self.local('stream_release_local')
        except Exception:pass
        self.journal('stream.stop',reason)
    def monitor(self):
        state=self.manager.status()
        if state['phase'] not in ('listening','streaming'):return state
        token=state['generation']
        try:
            if self.signal is None or self.signal.stopped():raise ValueError('Stream lease ended; reconnect readiness required')
            if not self.persistent_admitted() and (self.observer is None or not self.observer.alive()):raise ValueError('Network observer process ended')
            if not self.launcher or not self.launcher.last_owned.alive():raise ValueError('Streaming process or indicator ended')
            if not self.persistent_admitted():
                setup=read_shared_json(self.private/'admin-result.json')
                if setup.get('state')!='ready':raise ValueError('Network observation ended')
                capture=read_shared_json(self.private/'capture-heartbeat.json')
                if capture.get('capture_running') is not True or time.time()-capture.get('ts',0)>3:raise ValueError('Packet capture is no longer proven active')
            observed=self.probe(self.launcher.last_owned.process.process.pid)
            if not observed['network']:raise ValueError('Tailscale interface lost')
            validate_rules(self.policy.firewall_plan(),observed['rules'])
            sockets={(r['protocol'],r['address'],int(r['port'])) for r in observed['sockets']}
            self.policy.validate_observed_sockets(sockets)
            self.last_stats={'ts':time.time(),'pid':self.launcher.last_owned.process.process.pid,'sockets':sorted(sockets),'binary_sha256':EXE_HASH}
            atomic(self.private/'observed-listeners.json',self.last_stats)
            if not sockets_ready(sockets,time.monotonic()-self.started_at):return self.manager.status()
            conn=http.client.HTTPConnection(LOCAL,47989,timeout=1)
            try:
                conn.request('GET','/serverinfo');response=conn.getresponse();raw=response.read(65536)
                if response.status==200:
                    self.server_ready=True
                    import xml.etree.ElementTree as ET
                    info=ET.fromstring(raw);busy=int(info.findtext('currentgame','0'))!=0
                    if busy:self.was_busy=True;self.manager.connected(token)
                    elif self.was_busy:raise ValueError('Phone ended its Desktop session; server is OFF')
                else:raise ValueError('Sunshine readiness check refused ('+str(response.status)+')')
            except (ConnectionError,OSError):
                if not self.server_ready and time.monotonic()-self.started_at<45:return self.manager.status()
                raise
            finally:conn.close()
        except Exception as error:
            if self.manager.status()['generation']==token:
                self.manager.observed_failure(str(error),token)
                self.last_error=str(error)
                try:self.local('stream_release_local')
                except Exception:pass
        return self.manager.status()
    def pending_pairings(self):
        rows=self.admin.request('/api/pin').get('pairings',[])
        return [row for row in rows if row.get('address','').removeprefix('::ffff:')==PHONE]
    def pair(self,pairing_id,pin):
        if not re.fullmatch('[0-9a-fA-F]{32}',pairing_id) or not re.fullmatch('[0-9]{4}',pin):raise ValueError('Select the phone request and enter its four-digit PIN locally')
        if self.manager.status()['phase'] not in ('listening','streaming') or self.signal is None or self.signal.stopped():raise ValueError('The local streaming toggle is OFF or STOP is set')
        if not any(row['id']==pairing_id for row in self.pending_pairings()):raise ValueError('That phone pairing request is no longer pending')
        self.admin.request('/api/pin',{'pairing_id':pairing_id,'pin':pin,'name':'Phil Jett Pad Android'})
        self.journal('stream.pair','Local PIN submitted for the one pinned phone; PIN not logged')
    def end_observation(self):
        self.stop('Local observation ended');(self.private/'end-observation').touch()
    def close(self):
        self.closed=True;self.end_observation()
