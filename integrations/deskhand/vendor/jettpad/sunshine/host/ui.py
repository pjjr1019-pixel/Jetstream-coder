"""Temporary local playback gate UI; same lifecycle/STOP path as final integration."""
from pathlib import Path
import ctypes,hashlib,json,os,queue,subprocess,sys,threading,time,zipfile
import tkinter as tk
from tkinter import messagebox,simpledialog,ttk
from .policy import WARNING
from .runtime import Runtime,atomic,EXE_HASH,read_shared_json,LOCAL,PHONE

ZIP_HASH='233008e46f4c0e501a586cbfd6c4fd4a4c0d414a0b5fc7f13c070eb92ec3824b'

class App:
    def __init__(self,root):
        self.root=root;self.home=Path(__file__).resolve().parents[1]
        self.runtime=Runtime(self.home);self.events=queue.Queue();self.busy=False;self.monitoring=False;self.closing=False;self.stopping=False;self.action_epoch=0
        root.title('Jett Pad — Live viewing setup (test gate)');root.geometry('700x660');root.configure(bg='#0A0A0A')
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('.',background='#141414',foreground='#F5F0E6',fieldbackground='#1A1A1A')
        style.configure('TButton',background='#C9A227',foreground='#0A0A0A',padding=9)
        style.map('TButton',background=[('active','#D4AF37')])
        panel=ttk.Frame(root,padding=20);panel.pack(fill='both',expand=True)
        ttk.Label(panel,text='JETT PAD · LIVE DESKTOP VIEW',font=('Segoe UI',19,'bold'),foreground='#D4AF37').pack(anchor='w',pady=10)
        ttk.Label(panel,text='Temporary playback test · OFF by default · View only',wraplength=640).pack(anchor='w')
        ttk.Label(panel,text=WARNING,wraplength=640,foreground='#D4AF37').pack(anchor='w',pady=14)
        self.status=tk.StringVar(value='OFF · Preparing controls; no stream has started.')
        ttk.Label(panel,textvariable=self.status,wraplength=640,font=('Segoe UI',11)).pack(anchor='w',pady=10)
        self.access=tk.StringVar(value='Network setup has not run. Streaming is OFF.')
        ttk.Label(panel,textvariable=self.access,wraplength=640).pack(anchor='w',pady=8)
        ttk.Button(panel,text='1. Prepare restricted network (Windows approval)',command=self.setup).pack(fill='x',pady=5)
        ttk.Button(panel,text='2. Turn streaming ON',command=self.start).pack(fill='x',pady=5)
        ttk.Button(panel,text='STOP STREAM / turn OFF',command=self.stop).pack(fill='x',pady=5)
        ttk.Button(panel,text='3. Pair phone — enter its PIN locally',command=self.pair).pack(fill='x',pady=5)
        ttk.Button(panel,text='Finish observation and check for discovery leaks',command=self.finish).pack(fill='x',pady=5)
        ttk.Button(panel,text='Remove test network rules (Windows approval)',command=self.remove).pack(fill='x',pady=5)
        ttk.Label(panel,text='Phone app PC address: '+LOCAL+r'\nOnly phone '+PHONE+r' is allowed.\nIf STOP is latched: clear it yourself in Jett Pad → Settings → Windows control.\nThis window cannot clear STOP or change grants. Nothing starts automatically.',wraplength=640).pack(anchor='w',pady=14)
        self.note=tk.StringVar(value='Live playback, NVENC output and latency have not yet been verified.')
        ttk.Label(panel,textvariable=self.note,wraplength=640).pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW',self.close);root.after(100,self.tick)
        self.background(self.refresh_local)
    def background(self,function,allow_stopping=False):
        if self.busy:return
        self.busy=True
        epoch=self.action_epoch
        def work():
            try:
                if epoch!=self.action_epoch or (self.stopping and not allow_stopping):raise ValueError('Stop cancelled the queued action')
                value=function();self.events.put(('ok',value))
            except Exception as error:self.events.put(('error',str(error)))
            finally:self.events.put(('idle',None))
        threading.Thread(target=work,daemon=True).start()
    def refresh_local(self):
        state=self.runtime.local();self.runtime.last_local=state
        return 'STOP is latched — clear it locally before streaming.' if state.get('stop_latched') else 'Local STOP is clear. Streaming stays OFF until you click Turn streaming ON.'
    def elevate(self,remove=False):
        args=['-NoProfile','-ExecutionPolicy','Bypass','-File',str(self.home/'host/admin_setup.ps1')]
        if remove:args.append('-Remove')
        shell=ctypes.WinDLL('shell32',use_last_error=True)
        shell.ShellExecuteW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_int]
        shell.ShellExecuteW.restype=ctypes.c_void_p
        result=shell.ShellExecuteW(None,'runas',str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'),subprocess.list2cmdline(args),str(self.home),0)
        if not result or result<=32:raise ValueError('Windows administrator approval was cancelled; streaming remains OFF')
    def setup(self):
        if self.busy or self.stopping or self.runtime.manager.status()['phase']!='off':
            self.note.set('Finish the current operation or stop viewing before preparing the network.');return
        if not messagebox.askyesno('Prepare Tailscale-only playback test',
            'Install the bundled Sunshine test component and rules for ONLY this phone?\n\nWindows will ask for administrator approval for those app-specific firewall rules and a thirty-minute network-discovery observation. Closing these controls ends observation sooner. No broad firewall policy changes. No stream starts.\n\nPacket observation is limited to discovery traffic (UDP5353); it includes Windows DNS-SD so leaks cannot hide behind SYSTEM.',parent=self.root):return
        def prepare():
            private=self.runtime.private
            # Restrict private state before creating keys/admin credentials or packet logs.
            import csv
            sid=next(csv.reader(subprocess.check_output(['whoami','/user','/fo','csv','/nh'],text=True,creationflags=0x08000000).splitlines()))[1]
            subprocess.run(['icacls',str(private),'/inheritance:r','/grant:r',f'*{sid}:(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],check=True,stdout=subprocess.DEVNULL,creationflags=0x08000000)
            archive=self.home/'Sunshine-Windows-AMD64-lite.zip'
            if hashlib.sha256(archive.read_bytes()).hexdigest()!=ZIP_HASH:raise ValueError('Bundled Sunshine archive checksum mismatch')
            vendor=self.home/'vendor';vendor.mkdir(exist_ok=True)
            if not self.runtime.exe.exists():
                with zipfile.ZipFile(archive) as source:
                    for member in source.infolist():
                        target=(vendor/member.filename).resolve()
                        if not target.is_relative_to(vendor.resolve()) or member.external_attr>>16 & 0o170000==0o120000:raise ValueError('Unsafe archive member')
                    source.extractall(vendor)
            if hashlib.sha256(self.runtime.exe.read_bytes()).hexdigest()!=EXE_HASH:raise ValueError('Sunshine binary checksum mismatch')
            atomic(private/'firewall-plan.json',{'local':self.runtime.policy.local,'phone':self.runtime.policy.phone,'interface':self.runtime.policy.interface,'rules':self.runtime.policy.firewall_plan()})
            end=private/'end-observation'
            if end.exists():end.unlink()
            self.runtime.ui_heartbeat();self.elevate()
            return 'Approve the Windows prompt. Then wait for Network observation READY; streaming remains OFF.'
        self.background(prepare)
    def start(self):
        if self.busy or self.monitoring or self.stopping:return
        if not messagebox.askyesno('Turn desktop streaming ON?',WARNING+'\n\nThis starts the view-only test server and visible PC badge. Local STOP stops it. Start now?',parent=self.root):return
        self.background(lambda:(self.runtime.start() or 'Starting the authorized view-only server…'))
    def stop(self):
        # Kill synchronously before any dialog, network work or logging.
        if self.stopping:return
        self.stopping=True
        self.action_epoch+=1
        self.runtime.manager.stop_now('Local OFF button')
        def stop_work():
            try:self.runtime.stop('Local OFF button')
            finally:self.events.put(('stopped',None))
        threading.Thread(target=stop_work,daemon=True).start()
        self.status.set('OFF · stream stopped locally')
    def pair(self):
        if self.busy or self.stopping:return
        def pending():
            rows=self.runtime.pending_pairings()
            self.events.put(('pair',rows))
        self.background(pending)
    def show_pair(self,rows):
        if len(rows)!=1:
            self.note.set('Open Jett Pad on the phone, enter PC address '+LOCAL+' and tap Pair. Then click this pairing button again. Exactly one request from the pinned phone is required.');return
        pin=simpledialog.askstring('Local Sunshine pairing','Enter the four-digit PIN displayed in the Jett Pad phone app.\nIt stays local and is never logged or sent through chat.',show='•',parent=self.root)
        if pin is None:return
        identity=rows[0]['id']
        self.background(lambda:(self.runtime.pair(identity,pin) or 'PIN submitted locally. The phone should confirm pairing.'))
    def finish(self):
        def work():
            self.runtime.end_observation()
            path=self.runtime.private/'discovery.pcapng'
            deadline=time.monotonic()+15
            while not path.exists() and time.monotonic()<deadline:time.sleep(.25)
            from .discovery import analyze
            result=analyze(path);atomic(self.runtime.private/'discovery-result.json',result)
            if not result['no_announcements']:raise ValueError('Discovery leak observed. Stream is OFF; a reviewed no-discovery build is required before further viewing.')
            return 'Observation complete: no Sunshine announcements in the recorded NIC discovery traffic. This result still needs review alongside capture scope and runtime.'
        self.background(work)
    def remove(self):
        if not messagebox.askyesno('Remove test rules','Stop viewing and remove only this test component’s firewall rules? Windows will ask for administrator approval.',parent=self.root):return
        self.stop();self.background(lambda:(self.elevate(True) or 'Test-rule removal requested locally.'),allow_stopping=True)
    def tick(self):
        if self.closing:return
        try:self.runtime.ui_heartbeat()
        except Exception:self.stop()
        while True:
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='idle':self.busy=False
            elif kind=='stopped':self.stopping=False
            elif kind=='error':self.note.set(value)
            elif kind=='ok' and value:self.note.set(str(value))
            elif kind=='pair':self.root.after(0,lambda rows=value:self.show_pair(rows))
            elif kind=='monitor':self.monitoring=False
            elif kind=='access':self.access.set(value)
        state=self.runtime.manager.status()
        if state['phase']=='listening' and not self.runtime.server_ready:
            self.status.set('INITIALIZING · Checking capture and NVIDIA encoders; phone connection is not ready yet.')
        else:self.status.set(state['phase'].upper()+' · '+state['reason'])
        try:
            admin=read_shared_json(self.runtime.private/'admin-result.json')
            if admin.get('state')=='ready':
                heartbeat=read_shared_json(self.runtime.private/'capture-heartbeat.json')
                if heartbeat.get('capture_running') is True and 0<=time.time()-heartbeat.get('ts',0)<3:
                    self.access.set('Network observation READY · STOP must be clear locally before you turn streaming on.')
                else:self.access.set('Network observation expired or stopped. Prepare restricted network again; streaming stays OFF.')
            elif admin.get('state')=='observation_complete':self.access.set('Network observation finished. Streaming is OFF.')
            elif admin.get('state')=='failed':self.access.set('Network setup: '+admin.get('error','failed'))
        except (OSError,ValueError):pass
        if state['phase'] in ('listening','streaming') and not self.monitoring:
            self.monitoring=True
            def monitor():
                try:self.runtime.monitor()
                finally:self.events.put(('monitor',None))
            threading.Thread(target=monitor,daemon=True).start()
        try:
            atomic(self.runtime.private/'ui-status.json',{'ts':time.time(),'stream':state,'note':self.note.get(),'network':self.access.get(),'controls':{'busy':self.busy,'stopping':self.stopping,'monitoring':self.monitoring}})
        except OSError:
            self.note.set('Unable to save diagnostics; controls remain active. No automatic restart.')
        finally:self.root.after(250,self.tick)
    def close(self):
        self.closing=True;self.action_epoch+=1;self.runtime.manager.stop_now('Local setup window closed')
        threading.Thread(target=self.runtime.close,daemon=True).start()
        (self.runtime.private/'end-observation').touch();self.root.destroy()

def main():
    from musebridge.lifecycle import InstanceLock
    home=Path(__file__).resolve().parents[1]/'private'
    with InstanceLock(home):
        root=tk.Tk();App(root);root.mainloop()
if __name__=='__main__':main()
