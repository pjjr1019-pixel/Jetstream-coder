"""App-owned job thread. Admitted jobs are independent of the bridge socket.

STOP is synchronous cancellation + kill of owned helper jobs, before DB/audit.
No replay of interrupted work, no grants, no PIN/day expiry extension.
"""
import base64,copy,hashlib,json,os,sqlite3,subprocess,sys,threading,time,uuid
from pathlib import Path
from contextlib import closing
from .protocol import validate_task,identity,required,TRANSPORT_END
from musebridge.execution import _WindowsJob,CREATE_SUSPENDED,scrub_environment
from musebridge.redaction import redact_text
from musebridge.session_watch import interactive_session
from musebridge.runtime_paths import documents as runtime_documents, pad_data, orb_data, helper_command, root as runtime_root

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(uuid.uuid4().hex[:12]+'.tmp')
    try:
        with temporary.open('w',encoding='utf-8') as out:json.dump(value,out,ensure_ascii=True,allow_nan=False);out.flush();os.fsync(out.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)

class Cancelled(ValueError):pass

def resolve_listed_targets(task,catalog):
    """Bind listed IDs to their current HWND/PID before a job is admitted."""
    task=copy.deepcopy(task)
    def bind(value):
        if isinstance(value,dict) and 'window_id' in value:
            info=catalog.resolve(value['window_id'])
            return {'hwnd':info['hwnd'],'pid':info['pid']}
        return value
    def visit(step):
        for key in ('target','window'):
            if key in step:step[key]=bind(step[key])
        verify=step.get('verify')
        if isinstance(verify,dict) and 'window' in verify:verify['window']=bind(verify['window'])
        for fallback in step.get('fallbacks',[]):visit(fallback)
    for step in task['steps']:visit(step)
    return task

def progress_lines(raw,seen):
    items=[];lines=raw.splitlines(keepends=True)
    for line in lines[seen:]:
        if not line.endswith(b'\n'):break  # retain partial records for next poll
        seen+=1
        try:item=json.loads(line)
        except (ValueError,UnicodeError):continue  # library banner must not hide later receipts
        if isinstance(item,dict):items.append(item)
    return seen,items

class Coordinator:
    def __init__(self,bridge,*,documents=None,native=None,session_check=interactive_session):
        self.bridge=bridge;self.root=bridge.data_dir/'executor';self.root.mkdir(exist_ok=True)
        self.output=Path(documents or runtime_documents())/'jett-executor';self.records={}
        self.lock=threading.RLock();self.wake=threading.Event();self.stopping=threading.Event()
        self.active=None;self.thread=None;self._job=None;self._epoch=0;self._admissions_stopped=False
        self._process_lock=threading.RLock()
        self._stop_receipt=None
        self._reconnect_approved_epoch=None
        self.storage_fault=None
        self.worker_fault=None
        self.reserved=False
        from jettcrew.state_service import StateService
        self.journal=StateService(self.output.parent).journal
        self.session_check=session_check;self.native=native or self.native_call;self.capture_active=False;self.input_active=False
        # Never automatically repeat work after a runtime/process interruption.
        for path in self.root.glob('*.json'):
            try:
                row=json.loads(path.read_text())
                if row.get('status') in ('queued','running'):
                    row['id']=identity(row['id']);row['result_path']=str(self.output/'out'/(row['id']+'.json'))
                    row.update(status='uncertain',reason='Runtime interrupted; review before any resubmission',finished_ts=time.time())
                    atomic(path,row);self.records[row['id']]=row;self.write_result(row)
            except (OSError,ValueError,KeyError):continue

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():return
            self.stopping.clear();self.thread=threading.Thread(target=self.loop,name='Jett local executor',daemon=True);self.thread.start()

    def interrupt(self,reason):
        if reason in TRANSPORT_END:return
        # Called at the very start of DesktopAccess.revoke, ahead of PIN I/O.
        self._epoch+=1;self._admissions_stopped=True;self._stop_receipt=(time.monotonic_ns(),reason)
        with self._process_lock:
            job=self._job
            if job:
                try:job.terminate()
                except OSError:pass
        self.wake.set()

    def resume_admissions(self, *, expected_epoch=None):
        # Called only after an already-authorized local clear/PIN/day unlock.
        epoch=self._epoch
        if expected_epoch is not None and expected_epoch!=epoch:return False
        try:self.bridge.desktop._ensure_authority('desktop_control')
        except ValueError:return False
        if self.worker_fault:return False
        self._admissions_stopped=False
        if self._epoch!=epoch:
            self._admissions_stopped=True;return False
        return True

    def defer_local_recovery(self):
        """Remember an explicit local clear until its first live connection."""
        from musebridge.host_stop import ensure_host_allows
        ensure_host_allows()
        if self.bridge.desktop.stop_latched or not self.bridge.config['features']['desktop_control']:
            return False
        self._reconnect_approved_epoch=self._epoch
        return True

    def resume_authenticated_session(self):
        """Recover transport interruption or a prior explicit local clear only."""
        epoch=self._epoch
        receipt=self._stop_receipt
        if (self._admissions_stopped and self._reconnect_approved_epoch!=epoch
                and (receipt is None or receipt[1]!='https_session_ended')):
            return False
        resumed=self.resume_admissions(expected_epoch=epoch)
        if resumed:self._reconnect_approved_epoch=None
        return resumed

    def submit(self,request_id,payload,*,notify=False,https_generation=None):
        task,fingerprint=validate_task(payload)
        job_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'jett-executor:'+request_id))
        with self.lock:
            existing=self.get(job_id,private=True)
            if existing:
                if existing['fingerprint']!=fingerprint:raise ValueError('Request id reused for a different task')
                return self.public(existing)
            b=self.bridge;epoch=self._epoch
            if https_generation is not None and (b.https_session is None or not b.https_session.generation_is_live(https_generation)):
                raise ValueError('HTTPS session expired')
            if self._admissions_stopped:raise ValueError('Executor STOP requires normal authorized recovery')
            if not b.config.get('enabled') or b.config.get('tier',0)==0:raise ValueError('Bridge permissions are off')
            b.permissions.check_session('desktop');b.desktop._ensure_authority('desktop_control')
            if not b.connected or not self.session_check():raise ValueError('Authenticated interactive session required for job admission')
            needs=required(task)
            for feature in needs:
                if not b.config['features'].get(feature):raise ValueError('Required local feature is disabled: '+feature)
            task=resolve_listed_targets(task,b.desktop.catalog)
            if sum(r['status'] in ('queued','running') for r in self.records.values())>=16:raise ValueError('Executor queue is full')
            for step in [s for row in task['steps'] for s in [row,*row.get('fallbacks',[])]]:
                if step['op']=='run':
                    if b.config.get('execution_mode','host')!='host':raise ValueError('Host execution is disabled')
                    b.permissions.check_session('exec')
                    if not step.get('cwd'):raise ValueError('run requires an approved cwd')
                    step['cwd']=str(b.permissions.authorize_cwd(step['cwd']))
            now=time.time();deadline=now+task['timeout_secs']
            if b.permissions.session_expires_at is not None:deadline=min(deadline,b.permissions.session_expires_at)
            remote=b.remote_control
            remote_active=remote.check()
            if remote_active:deadline=min(deadline,remote.expires)
            revision=remote.pins.revision()
            day=remote.day_status()
            if remote_active and day['day_authorized']:deadline=min(deadline,day['day_authorization_expires_at'])
            # Capture the monotonic lease at ADMISSION, not when queued work
            # starts. Clock rollback or queue delay must never buy extra time.
            lease_deadline=time.monotonic()+max(0,deadline-now)
            if remote_active:lease_deadline=min(lease_deadline,time.monotonic()+max(0,remote.deadline-remote.monotonic()))
            if self._epoch!=epoch or self._admissions_stopped:raise Cancelled('Admission revoked')
            if https_generation is not None and (b.https_session is None or not b.https_session.generation_is_live(https_generation)):
                raise Cancelled('HTTPS session expired')
            row=dict(id=job_id,request_id=request_id,fingerprint=fingerprint,task=task,status='queued',created_ts=now,
                deadline=deadline,pin_revision=revision,epoch=epoch,required=sorted(needs),steps=[],cancel=False,
                result_path=str(self.output/'out'/(job_id+'.json')),_lease_deadline=lease_deadline,
                _https_generation=https_generation,
                _notify_socket=b.client if notify else None)
            with b.desktop._lock:
                if b.desktop._active_control_operations:raise ValueError('Another desktop operation is running; retry admission later')
                self.reserved=True
                try:atomic(self.root/(job_id+'.json'),{k:v for k,v in row.items() if not k.startswith('_')});self.records[job_id]=row
                except Exception:self.reserved=bool(self.active or any(r['status']=='queued' for r in self.records.values()));raise
            self.log(row,'job.accepted');self.start();self.wake.set();return self.public(row)

    def get(self,job_id,private=False):
        job_id=identity(job_id)
        row=self.records.get(job_id)
        if row is None:
            path=self.root/(job_id+'.json')
            if path.exists():
                try:row=json.loads(path.read_text())
                except (OSError,ValueError):row=None
                if not isinstance(row,dict):row=None
        return row if private or row is None else self.public(row)

    def public(self,row):
        return {key:row.get(key) for key in ('id','status','created_ts','started_ts','finished_ts','reason','result_path','steps')}

    def status(self):
        with self.lock:
            return dict(ready=bool(self.thread and self.thread.is_alive()),running=self.active is not None,
                job_id=self.active,queued=sum(r['status']=='queued' for r in self.records.values()),capture_active=self.capture_active,
                recording=False,storage_fault=self.storage_fault,worker_fault=self.worker_fault,authorization='immutable admitted job; original expiry; STOP/lock/revoke cancel')

    def cancel(self,job_id):
        row=self.get(job_id,private=True)
        if row is None:raise ValueError('Unknown executor job')
        row['cancel']=True
        with self._process_lock:
            if self.active==row['id'] and self._job:self._job.terminate()
        self.wake.set();return self.public(row)

    def guard(self,row):
        if self.stopping.is_set() or row.get('cancel') or row['epoch']!=self._epoch:raise Cancelled('Job cancelled by STOP/revoke')
        if time.time()>=row['deadline'] or time.monotonic()>=row['_monotonic_deadline']:raise Cancelled('Original job authorization/budget expired')
        if not self.session_check():raise Cancelled('Windows session unavailable or locked')
        b=self.bridge
        generation=row.get('_https_generation')
        if generation is not None and (b.https_session is None or not b.https_session.generation_is_live(generation)):
            raise Cancelled('HTTPS session expired')
        if not b.config.get('enabled') or b.config.get('tier',0)==0:raise Cancelled('Bridge permissions revoked')
        for feature in row['required']:
            if not b.config['features'].get(feature):raise Cancelled('Local feature revoked: '+feature)
        b.permissions.check_session('desktop')
        if b.config.get('stop_latched') and b.config.get('stop_reason') not in TRANSPORT_END|{'remote_day_authorization_session','remote_unlock_requires_fresh_pin'}:
            raise Cancelled('Local STOP is latched')
        # Short, read-only PIN-revision query. Never export verifier material.
        if time.monotonic()-row.get('_last_pin_check',0)>.2:
            with closing(sqlite3.connect(b.remote_control.pins.path.as_uri()+'?mode=ro',uri=True,timeout=.01)) as db:
                found=db.execute('SELECT revision FROM pin WHERE id=1').fetchone()
            if (found[0] if found else None)!=row['pin_revision']:raise Cancelled('Local PIN changed')
            row['_last_pin_check']=time.monotonic()

    def log(self,row,event,**extra):
        entry=dict(job_id=row['id'],event=event,ts=time.time(),monotonic_ns=time.monotonic_ns());entry.update(extra)
        self.output.mkdir(parents=True,exist_ok=True)
        with (self.output/'events.jsonl').open('a',encoding='utf-8') as out:out.write(json.dumps(entry)+'\n')
        try:self.bridge.audit.record('executor.'+event,row['id'],True,json.dumps(extra)[:1500])
        except Exception:pass
        try:self.journal('executor.'+event,json.dumps(extra)[:1500],row['id'])
        except OSError:pass
        loop=self.bridge._loop
        if loop is not None and not loop.is_closed():loop.call_soon_threadsafe(self.bridge.state_changed)

    def write_result(self,row):
        atomic(row['result_path'],self.public(row))

    def loop(self):
        while not self.stopping.is_set():
            with self.lock:
                row=next((r for r in self.records.values() if r['status']=='queued'),None)
                if row is None:self.reserved=False
            if row is None:self.wake.wait(.2);self.wake.clear();continue
            try:self.execute(row)
            except Exception as error:
                # An unavailable result disk must stop admission, never kill the
                # worker silently or claim a result was delivered.
                self.worker_fault=type(error).__name__+': '+redact_text(str(error))[:500]
                if isinstance(error,OSError):self.storage_fault=self.worker_fault
                row.update(status='failed',reason=('Result/journal storage failed: ' if self.storage_fault else 'Executor worker failed: ')+self.worker_fault)
                self.interrupt('executor_storage_fault')

    def execute(self,row):
        self.active=row['id'];row['_monotonic_deadline']=min(row['_lease_deadline'],time.monotonic()+max(0,row['deadline']-time.time()))
        row.update(status='running',started_ts=time.time());variables={}
        try:
            self.log(row,'job.started')
            for index,original in enumerate(row['task']['steps']):
                self.guard(row);steps=[original,*original.get('fallbacks',[])];done=False;last=None
                for step in steps:
                    for attempt in range(step.get('retries',0)+1):
                        self.guard(row);sid=str(index+1);self.log(row,'step.started',step_id=sid,attempt=attempt+1,op=step['op'])
                        if step['op']=='wait':
                            end=time.monotonic()+step['seconds']
                            while time.monotonic()<end:self.guard(row);self.stopping.wait(min(.025,max(0,end-time.monotonic())))
                            result={'ok':True,'waited_seconds':step['seconds']}
                        else:
                            prepared=copy.deepcopy(step)
                            if isinstance(prepared.get('target'),str):
                                if prepared['target'] not in variables:raise ValueError('Unknown window alias')
                                prepared['target']=variables[prepared['target']]
                            self.capture_active=False
                            self.input_active=step['op'] in ('open','close','activate','taskbar','click','type','key')
                            try:result=self.native(row,prepared,index)
                            finally:self.capture_active=False;self.input_active=False
                        self.guard(row)
                        if result.get('ok'):
                            if step.get('save_as') and result.get('window'):variables[step['save_as']]=result['window']
                            row['steps'].append(dict(step=index+1,op=step['op'],attempts=attempt+1,result=result))
                            self.log(row,'step.done',step_id=sid,attempt=attempt+1);done=True;break
                        last=result;self.log(row,'step.failed',step_id=sid,attempt=attempt+1,error=result.get('error','Step failed'))
                        if result.get('issued') and not step.get('retry_safe'):
                            row['status']='uncertain';raise ValueError('Action outcome unverified; not replayed: '+str(result.get('error','unknown')))
                        if attempt<step.get('retries',0):self.log(row,'step.retry',step_id=sid,attempt=attempt+1)
                    if done:break
                if not done:raise ValueError((last or {}).get('error','Step failed'))
                atomic(self.root/(row['id']+'.json'),{k:v for k,v in row.items() if not k.startswith('_')})
            row.update(status='done',reason='All requested postconditions verified')
        except Cancelled as error:row.update(status='cancelled',reason=str(error))
        except Exception as error:
            row.update(status='uncertain' if row['status']=='uncertain' else 'failed',reason=redact_text(str(error))[:500])
        finally:
            self.capture_active=False;self.input_active=False;self.active=None;row['finished_ts']=time.time()
            if row['epoch']!=self._epoch and self._stop_receipt:
                self.log(row,'stop.received',monotonic_ns=self._stop_receipt[0],reason=self._stop_receipt[1])
                self.log(row,'stop.quiescent')
            self.log(row,'job.'+row['status'])
            clean={k:v for k,v in row.items() if not k.startswith('_')};atomic(self.root/(row['id']+'.json'),clean);self.write_result(clean)
            self.notify_completed(row)

    def notify_completed(self,row):
        socket=row.get('_notify_socket');loop=self.bridge._loop
        if socket is None or loop is None or loop.is_closed():return
        async def publish():
            if socket is not self.bridge.client:return
            try:await self.bridge._send(socket,'actions.completed',self.public(row),row['request_id'])
            except Exception:pass  # canonical receipt remains queryable; never replay
        loop.call_soon_threadsafe(lambda:loop.create_task(publish()))

    def native_call(self,row,step,index):
        b=self.bridge;op=step['op']
        if op=='run':
            retry=b.runner.limiter.accept('exec',b.config['rate_limits']['exec_count'],b.config['rate_limits']['exec_window_secs'])
            if retry:raise ValueError('Execution rate limit exceeded; retry after '+str(retry)+' seconds')
        if op in ('open','close','activate','taskbar','click','type','key'):b.desktop._rate_limit()
        if op=='screenshot':
            retry=b.desktop.screenshot_limiter.accept('screenshot',b.config['rate_limits']['screenshots_per_minute'],60)
            if retry:raise ValueError('Screenshot rate limit exceeded')
        cfg=copy.deepcopy(self.bridge.config);cfg['stop_latched']=False
        cfg.pop('token',None)
        operators=set(self.bridge.desktop.operator_pids)
        try:
            app=json.loads((pad_data()/'application.json').read_text());operators.add(app['pid'])
        except (OSError,ValueError,KeyError):pass
        request=dict(step=step,config=cfg,config_path=str(self.bridge.config_path),operators=list(operators),orb_renderer=str(orb_data()/'renderer.json'))
        package=Path(__file__).resolve().parent.parent
        python=Path(sys.executable)
        if python.name.casefold()=='pythonw.exe':python=python.with_name('python.exe')
        command=(helper_command('executor.native') if runtime_root() is not None else
                 [str(python),'-B','-c',"import sys,runpy;sys.path.insert(0,sys.argv[1]);runpy.run_module('executor.native',run_name='__main__')",str(package)])
        command[0]=str(python)
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env=scrub_environment(),creationflags=CREATE_SUSPENDED|subprocess.CREATE_NO_WINDOW)
        job=None;initial=True;seen=0;start=time.monotonic();timeout=min(step.get('timeout_secs',15),row['deadline']-time.time())
        def progress(raw):
            nonlocal seen
            seen,items=progress_lines(raw,seen)
            for item in items:
                if item.get('event')=='action.issued':
                    self.log(row,'action.issued',**{k:v for k,v in item.items() if k in ('monotonic_ns','op','target_hwnd','process_name','title','target_protected')})
                elif item.get('event')=='capture.started':self.capture_active=True
                elif item.get('event')=='capture.ended':self.capture_active=False
                elif item.get('event')=='capture.source':self.log(row,'capture.source',**{k:v for k,v in item.items() if k in ('target_hwnd','process_name','title','target_protected')})
        try:
            job=_WindowsJob(process)
            with self._process_lock:self._job=job;self.guard(row);job.resume(process)
            data=json.dumps(request).encode()
            while True:
                self.guard(row)
                if time.monotonic()-start>=timeout:
                    row['status']='uncertain';raise ValueError('Step deadline exceeded; outcome may be uncertain')
                try:
                    output,errors=process.communicate(data if initial else None,timeout=.025);break
                except subprocess.TimeoutExpired as error:
                    initial=False
                    if error.output:progress(error.output)
            progress(output)
            self.guard(row)
            if process.returncode or len(output)>1024*1024:
                row['status']='uncertain';raise ValueError('Native step failed: '+redact_text(errors.decode('utf-8',errors='replace'))[-1000:])
            result=json.loads(output.splitlines()[-1])
            if 'data_b64' in result:
                self.guard(row)
                destination=self.bridge.permissions.authorize(str(self.output/'out'/(row['id']+'-'+str(index+1)+'.jpg')),'write')
                destination.parent.mkdir(parents=True,exist_ok=True)
                temporary=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp');raw=base64.b64decode(result.pop('data_b64'),validate=True)
                try:
                    temporary.write_bytes(raw);self.guard(row)
                    if destination.exists():raise ValueError('Capture destination already exists; not overwriting')
                    os.rename(temporary,destination)
                finally:temporary.unlink(missing_ok=True)
                result['path']=str(destination);result['sha256']=hashlib.sha256(raw).hexdigest()
                self.log(row,'capture.completed',target_protected=False,protected_sources_captured=False,protected_regions_count=len(result.get('protected_regions',[])),scope=result['capture_scope'])
            return result
        finally:
            with self._process_lock:
                self._job=None
                if job:job.terminate()
                elif process.poll() is None:process.kill()
            try:process.communicate(timeout=1)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            with self._process_lock:
                if job:job.close()

    def close(self):
        self.stopping.set();self.interrupt('executor_shutdown');self.wake.set()
        if self.thread:
            self.thread.join(2)
            if self.thread.is_alive():
                raise RuntimeError('Executor shutdown was not confirmed')
