"""Single-consumer file queue. All outputs are local preparation, never Pad replies."""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

from .ollama import Ollama
from .retrieval import Retrieval
from .support import atomic_json, machine_facts

MODELS={'qwen3.5:4b','qwen3.5:9b','qwen3.5:27b'}
IDENTITY=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$')


def validate(task):
    if not isinstance(task,dict):raise ValueError('Task must be a JSON object')
    if not isinstance(task.get('id'),str) or not IDENTITY.fullmatch(task['id']):raise ValueError('Invalid task id')
    if task.get('kind') not in ('research','coding','triage','code_fix'):raise ValueError('Unsupported task kind; mentorship begins in Phase 3')
    if task['kind'] in ('coding','code_fix'):
        if not isinstance(task.get('project_root'),str) or not task['project_root']:raise ValueError('Coding requires a locally approved project_root')
        if not isinstance(task.get('target_files'),list) or len(task['target_files'])!=1 or not isinstance(task['target_files'][0],str):raise ValueError('Coding requires one bounded target file')
        if not isinstance(task.get('test_command'),list) or not task['test_command'] or any(not isinstance(s,str) for s in task['test_command']):raise ValueError('Coding requires an immutable test_command argument array')
    if task.get('model_hint') not in MODELS|{'auto'}:raise ValueError('Unsupported model_hint')
    for key in ('title','instructions'):
        if not isinstance(task.get(key),str) or not task[key].strip():raise ValueError('Missing '+key)
    if len(task['title'])>500 or len(task['instructions'])>16000:raise ValueError('Task instructions too large; split the task')
    if type(task.get('created_ts')) not in (int,float) or not 0<=task['created_ts']<1e12:raise ValueError('Invalid created_ts')
    files=task.get('context_files')
    if not isinstance(files,list) or len(files)>16 or any(not isinstance(p,str) for p in files):raise ValueError('context_files must be up to 16 explicit text paths')
    urls=task.get('source_urls',[])
    if not isinstance(urls,list) or len(urls)>4 or any(not isinstance(u,str) for u in urls):raise ValueError('source_urls must be up to four public URLs')
    # Validate supported generation controls; never pass arbitrary model options through.
    for key,low,high in (('num_ctx',8192,32768),('num_predict',128,8192)):
        if key in task and (type(task[key]) is not int or not low<=task[key]<=high):raise ValueError('Invalid '+key)
    if 'think' in task and type(task['think']) is not bool:raise ValueError('think must be boolean')
    if task.get('think') and task.get('num_predict',0)<4096:raise ValueError('Explicit thinking needs num_predict >=4096')
    if task['kind']=='triage' and task.get('think'):raise ValueError('Triage always requires think=false')
    if task.get('model_hint')=='qwen3.5:27b' and not re.search(r'\b(review|synthesis|synthesize)\b',task['title']+' '+task['instructions'],re.I):
        raise ValueError('27b is reserved for scheduled batch review/synthesis, never interactive tasks')
    return task


def route(task):
    if task['model_hint']!='auto':return task['model_hint']
    if task['kind']=='triage':return 'qwen3.5:4b'
    # Size-based short transforms must explicitly describe a transform, not research.
    if task['kind']=='research' and not task['context_files'] and not task.get('source_urls') and len(task['instructions'])<500 and re.search(r'\b(classify|classification|rewrite|reformat|transform)\b',task['title']+' '+task['instructions'],re.I):
        return 'qwen3.5:4b'
    return 'qwen3.5:9b'


def settings(task,model):
    quick=task['kind']=='triage' or model=='qwen3.5:4b'
    return {'num_ctx':task.get('num_ctx',8192),'num_predict':task.get('num_predict',1024 if quick else 2048),
            'think':False if quick else task.get('think',False)}


class Runner:
    def __init__(self,documents,data,state,*,ollama=None,facts=machine_facts,clock=time.time):
        self.documents=Path(documents);self.root=self.documents/'jett-crew';self.data=Path(data)
        from model_priority import ModelPriority
        self.model_priority=ModelPriority()
        self.state=state;self.ollama=ollama or Ollama(scheduler=self.model_priority);self.facts=facts;self.clock=clock
        self.data.mkdir(parents=True,exist_ok=True)
        for name in ('in','out','done','failed','.working'):(self.root/name).mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.data/'ledger.sqlite3')
        self.db.execute('create table if not exists tasks (id text primary key,digest text,state text,model text,attempts integer,updated real)')
        self.db.execute('create table if not exists embeddings (digest text primary key,vector text)');self.db.commit()
        self.retrieval=Retrieval(self.db,self.ollama,[self.documents])
        self.seen={};self.last_model=None;self.model_since=0;self.last_block=None
        self.started=self.clock();self.stopping=False
        from .coding import Coding
        self.coding=Coding(self)

    def journal(self,event,reason,ref=None):self.state.journal(event,reason,ref)

    def status(self,code,*,task=None,model=None,reason=None):
        atomic_json(self.data/'runtime.json',{'pid':os.getpid(),'started_ts':self.started,'ts':self.clock(),
            'state':code,'task_id':task,'model':model,'reason':reason,'queue_root':str(self.root),'contract':'1.1'})

    def record(self,identity,digest,status,model='',attempts=0):
        self.db.execute('insert or replace into tasks values (?,?,?,?,?,?)',(identity,digest,status,model,attempts,self.clock()));self.db.commit()

    def result(self,identity):
        path=self.root/'out'/(identity+'.json')
        if not path.exists():return None
        value=json.loads(path.read_text(encoding='utf-8'))
        if (value.get('id')!=identity or value.get('status') not in ('done','failed','stuck') or
            not isinstance(value.get('summary'),str) or not isinstance(value.get('model_used'),str) or
            not isinstance(value.get('tests'),list) or type(value.get('attempts')) is not int or
            type(value.get('finished_ts')) not in (int,float) or
            (value.get('detail_path') is not None and not isinstance(value.get('detail_path'),str))):
            raise ValueError('Canonical result is malformed; preserve it for local review')
        return value

    def archive(self,path,status):
        folder='done' if status=='done' else 'failed'
        target=self.root/folder/path.name
        if target.exists():target=target.with_name(target.stem+'-'+uuid.uuid4().hex+target.suffix)
        os.replace(path,target)

    def finish(self,path,task,model,*,summary,status='done',detail=None,attempts=1,started=None,metrics=None):
        identity=task['id'];existing=self.result(identity)
        if existing:
            self.journal('crew.reconcile','Canonical result already exists; do not overwrite or replay',identity)
            self.record(identity,'',existing['status'],existing['model_used'],existing['attempts']);self.archive(path,existing['status']);return
        detail_path=self.root/'out'/(identity+'.detail.txt')
        wall=max(0,self.clock()-(started if started is not None else self.clock()))
        metadata={'task_id':identity,'model':model,'wall_seconds':round(wall,3),'local_crew':True,
            'jett_authored':False,'created_ts':task.get('created_ts'),'finished_ts':self.clock(),
            'generation':metrics or {}}
        content='LOCAL CREW PREPARATION FOR JETT — NOT A JETT MESSAGE\n'+json.dumps(metadata)+'\n\n'+(detail or summary)
        temporary=detail_path.with_suffix('.txt.tmp')
        with temporary.open('w',encoding='utf-8') as stream:stream.write(content);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,detail_path)
        value={'id':identity,'status':status,'model_used':model,'summary':summary,'detail_path':str(detail_path),
            'tests':[],'attempts':attempts,'finished_ts':self.clock()}
        # Publish canonical files BEFORE bookkeeping and archive; recovery respects them.
        atomic_json(self.root/'out'/(identity+'.json'),value)
        self.record(identity,'',status,model,attempts);self.archive(path,status)
        self.journal('crew.'+status,summary,identity)
        self.status(status,task=identity,model=model,reason=summary if status!='done' else None)
        if status!='done':self.state.update('Crew task failed: '+summary,jett_state='error',crew=[],progress=None)
        else:self.state.update('Crew task completed',jett_state='idle',crew=[],progress=None)
        self.state.publish()

    def failed(self,path,task,model,reason,*,attempts=0):
        self.finish(path,task,model,summary='Failed: '+reason,status='failed',attempts=attempts)

    def recover(self):
        for path in sorted((self.root/'.working').glob('*.json')):
            try:
                task=json.loads(path.read_text(encoding='utf-8'));identity=task['id']
                if not isinstance(identity,str) or not IDENTITY.fullmatch(identity):raise ValueError('Invalid claimed id')
                if self.coding.recover(path,task):continue
                result=self.result(identity)
                if result:
                    self.journal('crew.reconcile','Result file wins over interrupted ledger/claim',identity)
                    self.record(identity,'',result['status'],result['model_used'],result['attempts']);self.archive(path,result['status'])
                else:
                    row=self.db.execute('select model,attempts from tasks where id=?',(identity,)).fetchone()
                    self.failed(path,task,row[0] if row else '',
                        'Interrupted while claimed; outcome unknown. Not automatically replayed; submit a new task id.',attempts=row[1] if row else 0)
            except Exception as exc:
                self.journal('crew.recovery_error',str(exc),path.name)
                # Preserve malformed/conflicting canonical files and all input evidence.
                self.archive(path,'failed');self.state.update('Crew recovery needs local review',jett_state='error')

    def interactive_pending(self):
        path=self.documents.parent/'AppData/Local/JettPad/browser-relay.sqlite3'
        if not path.exists():return False
        try:
            with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=.5) as db:
                return bool(db.execute("select 1 from jobs where state in ('queued','sending','sent','uncertain') limit 1").fetchone())
        except sqlite3.Error:return True

    def allowed(self,model):
        if shutil.disk_usage(self.root).free<2*1024**3:return False,'Disk pressure: less than 2 GiB free'
        facts=self.facts();free=facts['memory_free']
        if free is None:return False,'Memory sensor unavailable'
        if free<3*1024**3:return False,'Memory pressure: less than 3 GiB free'
        if model=='qwen3.5:27b':
            if free<20*1024**3:return False,'27b batch needs 20 GiB free RAM'
            if facts['ac'] is not True:return False,'27b requires verified AC power'
            if facts['idle_seconds'] is None or facts['idle_seconds']<300:return False,'27b waits for five idle minutes'
            if facts['fullscreen'] is not False:return False,'27b waits for no fullscreen activity'
            if self.interactive_pending():return False,'27b waits for interactive browser work'
        return True,None

    def candidates(self):
        groups=OrderedDict();now=self.clock()
        for path in sorted((self.root/'in').glob('*.json'),key=lambda p:(p.stat().st_mtime,p.name))[:128]:
            stamp=(path.stat().st_size,path.stat().st_mtime_ns)
            previous=self.seen.get(path)
            self.seen[path]=(stamp,previous[1] if previous and previous[0]==stamp else now)
            if now-self.seen[path][1]<1:continue
            try:
                if stamp[0]>128*1024:raise ValueError('Task file exceeds 128 KiB')
                raw=json.loads(path.read_text(encoding='utf-8-sig'))
                if isinstance(raw,dict) and raw.get('kind')=='mentor_reply':
                    self.coding.accept(path,raw);continue
                task=validate(raw);model=route(task)
                groups.setdefault(model,[]).append((path,task))
            except (ValueError,KeyError) as exc:
                self.reject(path,str(exc))
        return groups

    def reject(self,path,reason):
        with path.open('rb') as stream:raw_bytes=stream.read(128*1024+1)
        identity='invalid-'+hashlib.sha256(raw_bytes).hexdigest()[:24]
        try:
            raw=json.loads(raw_bytes.decode('utf-8-sig'))
            if isinstance(raw,dict) and isinstance(raw.get('id'),str) and IDENTITY.fullmatch(raw['id']):identity=raw['id']
        except (ValueError,UnicodeError):pass
        claimed=self.root/'.working'/(path.stem+'-'+uuid.uuid4().hex+'.json')
        os.replace(path,claimed);self.journal('crew.rejected',reason,identity)
        self.failed(claimed,{'id':identity},'',reason)

    def claim(self,path,task,model):
        claimed=self.root/'.working'/(path.stem+'-'+uuid.uuid4().hex+'.json')
        try:os.replace(path,claimed)
        except FileNotFoundError:return None
        # Re-read after the atomic claim, never execute a stale validation snapshot.
        with claimed.open('rb') as stream:raw=stream.read(128*1024+1)
        if len(raw)>128*1024:raise ValueError('Claimed task exceeds size limit')
        actual=validate(json.loads(raw.decode('utf-8-sig')))
        if actual!=task:raise ValueError('Task changed while being claimed; recovery will fail it safely')
        identity=task['id'];result=self.result(identity)
        if result:
            self.journal('crew.duplicate','Canonical result exists; input archived without recomputation',identity)
            self.record(identity,'',result['status'],result['model_used'],result['attempts']);self.archive(claimed,result['status']);return None
        prior=self.db.execute('select state from tasks where id=?',(identity,)).fetchone()
        if prior:
            self.journal('crew.reconcile','Ledger has task but canonical result missing; no automatic replay',identity)
            self.failed(claimed,task,model,'Canonical result missing for known task id; submit a new id.');return None
        digest=hashlib.sha256(raw).hexdigest();self.record(identity,digest,'claimed',model)
        self.journal('crew.claimed','Atomic claim; model='+model,identity)
        return claimed

    def cycle(self):
        if self.model_priority.interactive_waiting():
            self.status('blocked',reason='Interactive local reply has priority; crew waits at model-call boundaries')
            return 0  # Leave unclaimed tasks pending; never fail them for yielding.
        self.coding.retry_notifications()
        groups=self.candidates()
        if not groups:return 0
        # Give queued 4b/9b work precedence over all scheduled 27b work.
        order=sorted(groups,key=lambda m:(m=='qwen3.5:27b',m!=self.last_model))
        ready=[]
        for model in order:
            allowed,reason=self.allowed(model)
            if model=='qwen3.5:27b' and any(m!='qwen3.5:27b' for m in groups):allowed,reason=False,'27b waits for queued interactive-model batches'
            if model!=self.last_model and self.last_model and self.clock()-self.model_since<60:
                allowed,reason=False,'Model batching dwell: retaining previous model for at least 60 seconds'
            if not allowed:
                if reason!=self.last_block:self.journal('crew.blocked',reason);self.last_block=reason
                self.status('blocked',model=model,reason=reason)
                continue
            ready.extend((p,t,model) for p,t in groups[model][:16])
        if not ready:
            self.state.update('Crew scheduling blocked: '+str(self.last_block),jett_state='blocked',crew=[],progress=None)
            self.state.publish();return 0
        self.last_block=None;prepared=[]
        # Retrieve every selected task before any generation batch, not between tasks.
        self.retrieval.used_embedding=False
        for path,task,model in ready:
            if self.stopping:break
            claimed=self.claim(path,task,model)
            if not claimed:continue
            started=self.clock()
            self.status('retrieving',task=task['id'],model='embeddinggemma')
            self.journal('crew.retrieval','Preparing bounded excerpts',task['id'])
            try:
                from .public_web import collect
                web=collect(task.get('source_urls',[]),self.journal,task['id'])
                excerpts=self.retrieval.prepare(task,web)
                prepared.append((claimed,task,model,excerpts,started))
            except Exception as exc:self.failed(claimed,task,model,str(exc))
        if self.retrieval.used_embedding:self.ollama.unload('embeddinggemma')
        for model in OrderedDict.fromkeys(row[2] for row in prepared):
            self.journal('crew.batch','Generation batch '+model)
            self.last_model=model;self.model_since=self.clock()
            for path,task,selected,excerpts,started in prepared:
                if selected!=model or self.stopping:continue
                # Recheck resource/interactive gates before each heavy task.
                allowed,reason=self.allowed(model)
                if not allowed:
                    self.failed(path,task,model,'Resources changed after claim: '+reason);continue
                if task['kind'] in ('coding','code_fix'):
                    try:self.coding.start(path,task,model,excerpts)
                    except Exception as exc:self.failed(path,task,model,str(exc))
                    continue
                self.status('generating',task=task['id'],model=model)
                self.state.update('Crew generating',jett_state='researching' if task['kind'] in ('research','triage') else 'coding',
                    crew=[{'task_id':task['id'],'model':model,'kind':'coding' if task['kind']=='code_fix' else task['kind'],'progress':None}],progress=None)
                self.state.publish()
                self.record(task['id'],'','generating',model,1)
                self.journal('crew.generate','Ollama generation; settings='+json.dumps(settings(task,model)),task['id'])
                try:
                    prompt=json.dumps({'title':task['title'],'instructions':task['instructions'],
                        'phase':'Preparation only. No commands or source edits.','evidence_excerpts':excerpts},ensure_ascii=True)
                    response=self.ollama.generate(model,prompt,settings(task,model))
                    atomic_json(self.root/'out'/(task['id']+'.attempt-1.json'),response)
                    if response.get('done') is not True or response.get('done_reason')=='length':raise ValueError('Generation incomplete or output budget exhausted')
                    answer=json.loads(response.get('response',''))
                    if any(not isinstance(answer.get(k),str) or not answer[k].strip() for k in ('summary','detail')):raise ValueError('Empty/malformed model summary or detail')
                    words=answer['summary'].split();summary=' '.join(words[:199])[:2400]
                    detail=answer['detail']+'\n\nFull model summary:\n'+answer['summary']+'\n\nRetrieved evidence:\n'+excerpts
                    metrics={key:response.get(key) for key in ('total_duration','load_duration','prompt_eval_count','eval_count','eval_duration')}
                    self.finish(path,task,model,summary=summary,detail=detail,started=started,metrics=metrics)
                except Exception as exc:
                    self.failed(path,task,model,str(exc),attempts=1)
            if model=='qwen3.5:27b':self.ollama.unload(model);self.last_model=None
        return len(prepared)

    def close(self):self.db.close()
