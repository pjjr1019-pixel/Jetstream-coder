from __future__ import annotations
import hashlib,json,os,queue,subprocess,sys,threading,time
from pathlib import Path
from .windows import SuspendedOwnedProcess,current_creation_time

class OwnedHost:
    def __init__(self,process):
        self.process=process;self.guard=None;self._closed=False;self._lock=threading.Lock()
    def attach_guard(self,guard):
        with self._lock:
            if self._closed:guard.kill();raise OSError('Stream stopped before badge registration')
            self.guard=guard
    def terminate_now(self):
        with self._lock:
            if self._closed:return
            self._closed=True;guard=self.guard
        self.process.terminate_now()
        if guard and guard.poll() is None:
            try:guard.kill()
            except OSError:pass
    def alive(self):
        return not self._closed and self.process.alive() and self.guard and self.guard.poll() is None

class WinLauncher:
    def __init__(self,root:Path,stop_event:str,log_path:Path,config_path:Path,*,python=None,on_failure=None,publisher_identity=None):
        python=python or getattr(sys,'_base_executable',sys.executable)
        self.root=root;self.stop_event=stop_event;self.log_path=log_path;self.config_path=config_path;self.python=python
        self.on_failure=on_failure
        self.publisher_identity=publisher_identity
        self.last_owned=None
    def __call__(self,proof,owned):
        executable=proof.policy.executable.resolve()
        if hashlib.sha256(executable.read_bytes()).hexdigest()!=proof.executable_sha256:
            raise ValueError('Sunshine binary changed since preflight')
        with self.log_path.open('ab',buffering=0) as log:
            process=SuspendedOwnedProcess([str(executable),str(self.config_path)],executable.parent,log)
        self.launch_suspended(process,owned,executable,expected_sha256=proof.executable_sha256)
    def launch_suspended(self,process,owned,executable,*,guard_test=False,expected_sha256=None):
        host=OwnedHost(process);self.last_owned=host
        try:
            if not guard_test:
                if not expected_sha256 or hashlib.sha256(executable.read_bytes()).hexdigest()!=expected_sha256:
                    raise ValueError('Sunshine binary changed on the actual launch path')
            if not owned(host):raise ValueError('Stream ownership was cancelled before resume')
            if not guard_test and (not self.publisher_identity or not self.publisher_identity.get('owner_created')):
                raise ValueError('Exact live STOP publisher identity is required before capture')
            with self.log_path.with_name('badge-errors.log').open('ab',buffering=0) as errors:
                guard=subprocess.Popen([self.python,'-m','host.badge'],cwd=str(self.root),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=errors,text=True,creationflags=0x08000000,close_fds=True)
            host.attach_guard(guard)
            guard.stdin.write(json.dumps({'stop_event':self.stop_event,'pid':process.process.pid,'created':process.created,
                'owner_pid':os.getpid(),'owner_created':current_creation_time(),'publisher':self.publisher_identity,
                'executable':str(executable),'guard_test':guard_test,'local_stop_marker':str(self.root/'private/local-stop.json')})+'\n');guard.stdin.flush()
            ready=queue.Queue()
            threading.Thread(target=lambda:ready.put(guard.stdout.readline()),daemon=True).start()
            def heartbeat():
                while not host._closed and guard.poll() is None:
                    try:guard.stdin.write('{"heartbeat":true}\n');guard.stdin.flush()
                    except (OSError,ValueError):break
                    time.sleep(.2)
            threading.Thread(target=heartbeat,daemon=True).start()
            try:line=ready.get(timeout=5)
            except queue.Empty:raise ValueError('Streaming indicator did not report ready within five seconds') from None
            if not line or not json.loads(line).get('visible'):raise ValueError('Visible streaming indicator did not become ready')
            if host._closed:raise ValueError('Streaming stopped while indicator was starting')
            process.resume()
            notify=self.on_failure
            def monitor():
                while not host._closed:
                    if not host.alive():
                        host.terminate_now()
                        if notify:notify('Streaming process or visible watchdog ended')
                        return
                    time.sleep(.05)
            threading.Thread(target=monitor,daemon=True).start()
        except Exception:
            host.terminate_now();raise
