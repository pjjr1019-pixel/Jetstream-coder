"""App-owned Sunshine lifecycle. Local settings are never remotely writable."""
import json, threading, time
from companion_data import atomic, home, paired_certificates, settings, stream_root, pending_requests, complete_request
from tailscale_addresses import PC_TAILSCALE_IP, PHONE_TAILSCALE_IP

class CompanionService:
    def __init__(self):
        self.runtime=None;self.closed=False;self.busy=False;self.error='';self.lock=None
        self.thread=threading.Thread(target=self.run,name='Jett companion services',daemon=True)
        self.thread.start()
    def run(self):
        try:
            from musebridge.lifecycle import InstanceLock
            from sunshine.host.runtime import Runtime
            self.lock=InstanceLock(stream_root()/'private');self.lock.__enter__()
            self.runtime=Runtime(stream_root())
            from away_readiness import SleepRequest,power
            self.sleep=SleepRequest();self.boot_id=__import__('uuid').uuid4().hex
            self.monitoring=False;self.monitor_since=0;self.next_readiness=0;self.readiness={};self.readiness_busy=False
            try:self.stop_generation=json.loads((home()/'viewing-barrier.json').read_text()).get('generation',0)
            except (OSError,ValueError):self.stop_generation=0
            self.intent=None
            while not self.closed:
                self.runtime.ui_heartbeat()
                self.check_stop_markers()
                try:
                    bridge_boot=json.loads((home()/'companion-bridge-boot.json').read_text())['id']
                    if getattr(self,'bridge_boot',bridge_boot)!=bridge_boot:
                        self.cancel_viewing_intent('PC connection restarted; tap Start again')
                    self.bridge_boot=bridge_boot
                except (OSError,ValueError,KeyError):pass
                self.poll_commands()
                if getattr(self,'independent_running',False) and time.monotonic()>=getattr(self,'next_viewer_check',0):
                    self.next_viewer_check=time.monotonic()+1
                    from viewing_access import authorized
                    try:approved=authorized(self.viewer_fingerprint) and self.viewer_fingerprint in paired_certificates()
                    except (OSError,ValueError):approved=False
                    if not approved:self.cancel_viewing_intent('Unattended viewing approval revoked');self.independent_running=False
                state=self.runtime.manager.status()
                if state['phase'] in ('listening','streaming') and not self.monitoring and not self.busy:
                    self.monitoring=True;self.monitor_since=time.monotonic()
                    def monitor():
                        try:self.runtime.monitor()
                        finally:self.monitoring=False
                    threading.Thread(target=monitor,name='Bounded stream monitor',daemon=True).start()
                if self.monitoring and time.monotonic()-self.monitor_since>12:
                    self.runtime.manager.stop_now('Stream health monitor stalled')
                    self.error='Stream health monitor stalled; stream stopped'
                if not settings()['remote_start'] and getattr(self,'remote_running',False):
                    self.runtime.stop('Local phone-start permission revoked');self.remote_running=False
                    self.stop_generation+=1;self.intent=None
                    atomic(home()/'viewing-barrier.json',{'generation':self.stop_generation,'ts':time.time()})
                self.sleep.update(settings().get('away_access',False),power() is True)
                if time.monotonic()>=self.next_readiness and not self.readiness_busy:
                    self.next_readiness=time.monotonic()+10;self.readiness_busy=True
                    def check():
                        try:
                            from away_readiness import snapshot
                            self.readiness=snapshot(self.runtime)
                        except Exception as e:self.readiness={'ready':False,'blocks':['Readiness unavailable: '+str(e)]}
                        finally:self.readiness_busy=False
                    threading.Thread(target=check,name='Away readiness checks',daemon=True).start()
                state=self.runtime.manager.status();phase=state['phase']
                if phase=='listening' and not self.runtime.server_ready:phase='starting'
                atomic(home()/'companion-stream.json',dict(state,phase=phase,ts=time.time(),reason=self.error or state['reason'],
                    boot_id=self.boot_id,stop_generation=self.stop_generation,intent_id=self.intent,readiness=self.readiness))
                time.sleep(.25)
        except Exception as e:
            self.error=str(e)
            atomic(home()/'companion-stream.json',{'phase':'off','reason':'PC setup required: '+self.error,'ts':time.time()})
        finally:
            if getattr(self,'sleep',None):self.sleep.close()
            if self.runtime:self.runtime.close()
            if self.lock:self.lock.__exit__(None,None,None)
    def check_stop_markers(self):
        if not hasattr(self,'seen_stops'):self.seen_stops={}
        for marker in (self.runtime.private/'local-stop.json',home()/'control-viewing-stop.json'):
            try:
                value=json.loads(marker.read_text());stamp=value.get('ts',0)
                if self.seen_stops.get(str(marker))!=stamp:
                    self.seen_stops[str(marker)]=stamp;self.stop_generation+=1;self.intent=None
                    self.last_stop=max(getattr(self,'last_stop',0),stamp)
                    self.runtime.manager.stop_now('Local stream STOP');self.error='Local stream STOP; tap Start to request a new session'
                    atomic(home()/'viewing-barrier.json',{'generation':self.stop_generation,'ts':stamp})
            except (OSError,ValueError):pass
    def cancel_viewing_intent(self,reason):
        self.runtime.manager.stop_now(reason)
        self.stop_generation+=1;self.intent=None;self.remote_running=False
        atomic(home()/'viewing-barrier.json',{'generation':self.stop_generation,'ts':time.time()})
    def poll_commands(self):
        # STOP/pause are selected before starts, without parsing old receipts.
        requests = pending_requests(('stream.stop','stream.pause'))
        requests += pending_requests(('stream.start',))
        for path,value in requests:
            kind=value.get('kind');identifier=value.get('id')
            if kind not in ('stream.start','stream.stop','stream.pause') or not identifier:continue
            result=home()/'companion-results'/(identifier+'.json')
            if result.exists():continue
            try:
                if value['phone'] not in paired_certificates():raise ValueError('Phone no longer paired')
                if kind in ('stream.stop','stream.pause'):
                    self.last_stop=max(getattr(self,'last_stop',0),value['created'])
                    self.runtime.stop('Paired phone Stop' if kind=='stream.stop' else 'Phone background pause')
                    if kind=='stream.stop':
                        self.stop_generation=getattr(self,'stop_generation',0)+1;self.intent=None
                        atomic(home()/'viewing-barrier.json',{'generation':self.stop_generation,'ts':time.time()})
                else:
                    from viewing_access import enabled,authorized
                    if enabled() and not authorized(value['phone']):raise ValueError('Phone unattended-viewing approval was revoked')
                    if enabled() and type(value['payload'].get('_stream_stop_epoch')) is not int:raise ValueError('Fresh stream STOP epoch required')
                    bridge_boot=json.loads((home()/'companion-bridge-boot.json').read_text())['id']
                    if value['payload'].get('_bridge_boot_id')!=bridge_boot:raise ValueError('PC connection restarted; tap Start again')
                    if time.time()-value['created']>30 or value['created']<=getattr(self,'last_stop',0):raise ValueError('Start expired or cancelled; tap Start again')
                    if not settings()['remote_start']:raise ValueError('Phone stream starts are disabled locally')
                    if value['payload'].get('resume'):
                        if value['payload'].get('boot_id')!=self.boot_id or value['payload'].get('stop_generation')!=self.stop_generation or value['payload'].get('intent_id')!=self.intent:raise ValueError('Viewing was stopped or PC restarted; tap Start again')
                    else:self.intent=value['payload'].get('intent_id')
                    if not self.intent:raise ValueError('Viewing intent required')
                    if value['payload'].get('privacy_warning_accepted') is not True:raise ValueError('Acknowledge desktop visibility')
                    if self.busy or self.monitoring:raise ValueError('Previous stream operation is settling; try again')
                    if self.runtime.manager.status()['phase']!='off':
                        complete_request(identifier,{'ok':True});continue
                    self.error='';self.busy=True;self.remote_running=True
                    self.independent_running=enabled();self.viewer_fingerprint=value['phone']
                    generation=self.runtime.manager.status()['generation']
                    # Claim BEFORE the thread starts. A restart never replays an
                    # uncertain launch; the phone must make a fresh explicit request.
                    complete_request(identifier,{'ok':True,'state':'starting'})
                    def work(expected=generation,stop_epoch=value['payload'].get('_stream_stop_epoch'),identifier=identifier):
                        try:self.runtime.start(expected_generation=expected,origin='paired_phone',expected_stop_epoch=stop_epoch)
                        except Exception as e:
                            self.error='PC setup required: '+str(e)
                            self.runtime.journal('stream.start_error',self.error)
                            try:complete_request(identifier,{'ok':False,'error':self.error})
                            except Exception:pass
                        finally:self.busy=False
                    threading.Thread(target=work,name='Explicit phone stream start',daemon=True).start()
                    continue
                complete_request(identifier,{'ok':True})
            except Exception as e:
                self.error=str(e);complete_request(identifier,{'ok':False,'error':self.error})
    def close(self):
        self.closed=True
        if self.runtime:self.runtime.manager.stop_now('Jett Pad closed')

def add_settings(book,parent,services=None):
    import ctypes,subprocess
    import tkinter as tk
    from tkinter import ttk,messagebox
    from ui_components import help_label
    tab=ttk.Frame(book,padding=14);book.add(tab,text='Phone & streaming')
    ttk.Label(tab,text='Manage your paired phone and desktop viewing.',style='Muted.TLabel',wraplength=520).pack(anchor='w',pady=(0,12))
    from embedded_settings import add_settings as add_embedded_settings
    add_embedded_settings(tab,parent)
    values=settings();away=tk.BooleanVar(value=values.get('away_access',False));chat=tk.BooleanVar(value=values['chat']);start=tk.BooleanVar(value=values['remote_start'])
    def save():
        if start.get() and not settings()['remote_start']:
            if not messagebox.askyesno('Allow this phone to start desktop viewing?',
                'The paired phone can explicitly start Sunshine over Tailscale. Everything visible on the desktop is streamed, including sensitive and operator windows. A viewing session can resume after a brief drop or phone app switch. Explicit Stop and local STOP prevent resuming. Allow?',parent=parent):start.set(False)
        if not start.get() and services and getattr(services,'companion',None) and services.companion.runtime:
            services.companion.cancel_viewing_intent('Local phone-start permission revoked')
        try:current=json.loads((home()/'companion-settings.json').read_text())
        except (OSError,ValueError):current={}
        atomic(home()/'companion-settings.json',dict(current,chat=chat.get(),remote_start=start.get(),away_access=away.get()))
    ttk.Checkbutton(tab,text='Allow phone chat and PC-task requests',variable=chat,command=save).pack(anchor='w',pady=8)
    ttk.Checkbutton(tab,text='Allow phone to start desktop streaming',variable=start,command=save).pack(anchor='w',pady=8)
    def save_away():
        from away_guardian import configure_sign_in
        configure_sign_in(__import__('pathlib').Path(__file__).resolve().parent,away.get())
        save()
        atomic(home()/'guardian-policy.json',{'enabled':away.get(),'explicit_quit':False})
        if away.get():
            from away_guardian import start_guardian
            start_guardian(__import__('pathlib').Path(__file__).resolve().parent)
    help_label(tab,'Away access','away')
    ttk.Checkbutton(tab,text='Keep awake on AC and recover app services',variable=away,command=save_away).pack(anchor='w',pady=8)
    from viewing_access import enabled as view_enabled,configure as configure_view
    unattended=tk.BooleanVar(value=view_enabled())
    def save_view():
        if unattended.get() and not messagebox.askyesno('Allow unattended viewing from this paired phone?',
            'This paired phone can start viewing over Tailscale without enabling AI computer control on the PC. Everything visible on the desktop is shared. Emergency STOP cancels the current session; a fresh Start on the phone can request another. Uncheck this approval to disable future unattended viewing. Enable?',parent=parent):unattended.set(False);return
        try:
            configure_view(unattended.get())
            if unattended.get():away.set(True);start.set(True);save_away()
            else:
                start.set(False)
                if services and getattr(services,'companion',None):services.companion.cancel_viewing_intent('Unattended viewing approval revoked locally')
        except Exception as error:unattended.set(view_enabled());messagebox.showerror('Viewing approval',str(error),parent=parent)
    ttk.Checkbutton(tab,text='Allow this paired phone to view while I am away',variable=unattended,command=save_view).pack(anchor='w',pady=8)
    ttk.Label(tab,text='These approvals are saved locally for your paired phone.\nUnattended viewing is separate from AI control and its PIN/day permissions.\nEmergency STOP cancels the session; Start on the phone requests a fresh one.\nRevoke unattended approval here to block future starts.',wraplength=650).pack(anchor='w',pady=8)
    def prepare():
        runtime=getattr(getattr(services,'companion',None),'runtime',None)
        persistent=bool(runtime and runtime.persistent_admitted())
        if not messagebox.askyesno('Prepare restricted network',
            ('Install the verified persistent Tailscale-only network rules?' if persistent else 'Prepare Tailscale-only rules and the discovery verification capture (up to 30 minutes)?')+' Windows approval is required. No stream starts.',parent=parent):return
        root=stream_root();(root/'private/end-observation').unlink(missing_ok=True)
        from sunshine.host.policy import EndpointPolicy
        policy=EndpointPolicy(PC_TAILSCALE_IP,PHONE_TAILSCALE_IP,'Tailscale',root/'vendor/Sunshine/sunshine.exe')
        atomic(root/'private/firewall-plan.json',{'local':policy.local,'phone':policy.phone,'interface':policy.interface,'rules':policy.firewall_plan()})
        arguments=['-NoProfile','-ExecutionPolicy','Bypass','-File',str(root/'host/admin_setup.ps1')]
        if persistent:arguments.append('-Persistent')
        args=subprocess.list2cmdline(arguments)
        shell=ctypes.WinDLL('shell32',use_last_error=True)
        shell.ShellExecuteW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_int]
        shell.ShellExecuteW.restype=ctypes.c_void_p
        code=shell.ShellExecuteW(None,'runas','powershell.exe',args,str(root),0)
        if not code or code<=32:messagebox.showerror('Not prepared','Windows approval was cancelled. Streaming remains OFF.',parent=parent)
    help_label(tab,'Network preparation','network')
    ttk.Button(tab,text='Prepare restricted network…',command=prepare).pack(anchor='w',pady=8)
    def finish_discovery():
        if not messagebox.askyesno('Finish discovery verification',
            'Run this after at least one minute of viewing with the new Sunshine build. This stops the diagnostic stream, verifies captured positive controls and checks for discovery announcements. It cannot pass without the actual evidence.',parent=parent):return
        root=stream_root();(root/'private/end-observation').touch()
        def finish():
            try:
                from sunshine.host.runtime import read_shared_json
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    result=read_shared_json(root/'private/admin-result.json')
                    if result.get('state') in ('observation_complete','failed'):break
                    time.sleep(.25)
                from sunshine.host.admission import finalize
                finalize(root)
                parent.after(0,lambda:messagebox.showinfo('Discovery check passed','Actual positive controls were observed with no Sunshine announcements. The exact, already-approved persistent rules were reverified. Persistent readiness is now enabled; streaming remains OFF.',parent=parent))
            except Exception as error:
                text=str(error)
                parent.after(0,lambda:messagebox.showerror('Discovery check did not pass',text,parent=parent))
        threading.Thread(target=finish,name='Local discovery evidence gate',daemon=True).start()
    ttk.Button(tab,text='Finish discovery verification…',command=finish_discovery).pack(anchor='w',pady=8)
    status=tk.StringVar(value='PC streaming is OFF')
    ttk.Label(tab,textvariable=status,wraplength=650).pack(anchor='w',pady=12)
    def refresh():
        if not tab.winfo_exists():return
        try:
            state=json.loads((home()/'companion-stream.json').read_text())
            from sunshine.host.runtime import read_shared_json
            setup=read_shared_json(stream_root()/'private/admin-result.json')
            heartbeat=read_shared_json(stream_root()/'private/capture-heartbeat.json')
            ready=setup.get('state')=='ready' and heartbeat.get('capture_running') is True and 0<=time.time()-heartbeat.get('ts',0)<3
            network='Persistent network verified' if setup.get('state')=='persistent_ready' else 'Network READY (diagnostic)' if ready else 'Network preparation required'
            if setup.get('state')=='failed':network='Network setup failed: '+setup.get('error','unknown error')
            readiness=state.get('readiness',{})
            away='Ready for away access' if readiness.get('ready') else 'Away readiness: '+('; '.join(readiness.get('blocks',[])) or 'checking…')
            status.set(network+'\n'+state.get('phase','off').upper()+' · '+state.get('reason','')+'\n'+away)
        except (OSError,ValueError):status.set('Companion service unavailable; restart Jett Pad')
        tab.after(1000,refresh)
    refresh()
