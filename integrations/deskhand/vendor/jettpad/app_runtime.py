"""One local application owns Pad, bridge, private browser, state services and orb."""
from pathlib import Path
import json,os,subprocess,sys,time

def lock_held(path):
    path=Path(path)
    if not path.exists():return False
    try:
        with path.open('r+b') as stream:
            if os.name=='nt':
                import msvcrt
                try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                except OSError:return True
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except OSError:return True
        return False
    except OSError:return True  # Never start a competing service on an uncertain lock.

class ApplicationServices:
    def __init__(self,root,data,launch=subprocess.Popen,local=None):
        self.root=Path(root);self.data=Path(data)
        self.local=Path(local or os.environ['LOCALAPPDATA']);self.launch=launch
        sys.path.insert(0,str(self.root/'orb'))
        from orb.local_control import OrbMailbox,atomic
        self.atomic=atomic;self.orb=OrbMailbox(self.local/'JettOrb');self.orb_process=None
        self.closed=False;self.crew_process=None
    def log(self,event):
        self.data.mkdir(parents=True,exist_ok=True)
        with (self.data/'app-lifecycle.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(dict(ts=time.time(),event=event,pid=os.getpid()))+'\n')
    def start(self):
        self.atomic(self.data/'application.json',dict(pid=os.getpid(),started=time.time(),status='running'))
        from companion_service import CompanionService
        self.companion=CompanionService()
        from away_guardian import start_guardian
        start_guardian(self.root,explicit_start=True)
        self.start_orb()
        try:
            from harness_knowledge import refresh
            refresh(self.root,Path.home()/'Documents')
        except Exception:self.log('harness.reference_refresh_failed')
        self.ensure_crew()
        self.log('application.services_started')
    def ensure_crew(self):
        if not self.closed:
            component=getattr(self,'companion',None)
            if component and not component.thread.is_alive():
                from companion_service import CompanionService
                self.companion=CompanionService()
            browser=getattr(self,'browser',None)
            if browser and browser.thread and not browser.thread.is_alive() and not browser.stopping.is_set():browser.start()
            if getattr(self,'last_bridge_check',0)+10<time.monotonic():
                self.last_bridge_check=time.monotonic()
                try:
                    from away_guardian import start_guardian
                    start_guardian(self.root)
                except Exception:self.log('application.guardian_recovery_failed')
                try:
                    from musebridge.lifecycle import local_request
                    from bridge_control import BridgeControl
                    request=lambda action=None,payload=None:local_request(self.local/'MuseBridge',action,payload,timeout=2)
                    control=BridgeControl(request=request)
                    try:control.ensure_running()
                    finally:control.close()
                except Exception:self.log('application.bridge_recovery_failed')
        if self.closed or lock_held(self.local/'JettCrew/service.lock'):return
        if self.crew_process is None or self.crew_process.poll() is not None:
            self.crew_process=self.launch([str(self.root/'backend/.venv/Scripts/pythonw.exe'),'-m','jettcrew'],cwd=self.root,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    def start_orb(self):
        if self.closed:return
        if self.orb_process is not None and self.orb_process.poll() is None:return
        self.orb_process=self.launch([str(self.root/'backend/.orb-venv/Scripts/pythonw.exe'),
            str(self.root/'orb/guardian.py'),'--quiet','--owner-pid',str(os.getpid())],cwd=self.root/'orb',
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    def configure_orb(self,key,value):self.orb.request('configure',key=key,value=value)
    def preview(self):self.start_orb();self.orb.request('preview')
    def close(self):
        if self.closed:return
        self.closed=True
        if getattr(self,"companion",None):self.companion.close()
        self.orb.request('quit')
        self.atomic(self.local/'JettCrew/stop-request.json',dict(ts=time.time()))
        self.atomic(self.data/'application.json',dict(pid=os.getpid(),status='stopped',ts=time.time()))
        self.log('application.quit_requested')
