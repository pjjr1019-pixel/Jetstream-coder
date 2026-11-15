"""Bounded attempts, real harness evidence and genuine file-based Jett mentorship."""
import hashlib
import json
from pathlib import Path
import uuid

from .coding_guard import Approval,Conflict,apply_locked,build_patch,fingerprint,run_harness
from .support import atomic_json
from .mentor_channel import notify,bridge_provenance


class Coding:
    def __init__(self,runner,*,notifier=notify,provenance=bridge_provenance,harness=run_harness):
        self.r=runner;self.approval=Approval(runner.data/'coding-approvals.json')
        self.notifier=notifier;self.provenance=provenance;self.harness=harness
        self.r.db.execute('create table if not exists coding_integrity (id text primary key,digest text)');self.r.db.commit()

    def state_path(self,identity):return self.r.root/'out'/(identity+'.coding-state.json')
    def save(self,state):
        path=self.state_path(state['task']['id']);atomic_json(path,state)
        value=hashlib.sha256(path.read_bytes()).hexdigest()
        self.r.db.execute('insert or replace into coding_integrity values (?,?)',(state['task']['id'],value));self.r.db.commit()

    def load(self,identity):
        path=self.state_path(identity);state=json.loads(path.read_text())
        expected=self.r.db.execute('select digest from coding_integrity where id=?',(identity,)).fetchone()
        if not expected or expected[0]!=hashlib.sha256(path.read_bytes()).hexdigest():
            raise Conflict('Coding checkpoint/ledger disagreement; preserve files and require local review, never replay')
        if state['attempts']>5 or state['round']>2:raise Conflict('Invalid attempt/mentor ceiling')
        return state

    def output(self,state,status,summary):
        task=state['task'];identity=task['id'];state['phase']='waiting' if status=='stuck' else 'closed'
        state['status']=status;state['summary']=summary;self.save(state)
        detail=self.r.root/'out'/(identity+'.coding-detail.json');atomic_json(detail,state)
        value={'id':identity,'status':status,'model_used':state['model'],'summary':summary,'detail_path':str(detail),
            'tests':state['tests'],'attempts':state['attempts'],'finished_ts':self.r.clock()}
        atomic_json(self.r.root/'out'/(identity+'.json'),value)
        self.r.record(identity,'',status,state['model'],state['attempts'])
        self.r.journal('crew.coding.'+status,summary,identity)
        self.r.status(status,task=identity,model=state['model'],reason=summary if status!='done' else None)
        self.r.state.update(summary,jett_state='blocked' if status=='stuck' else 'idle' if status=='done' else 'error',crew=[],progress=None)
        self.r.state.publish()
        if status!='stuck' and Path(state['claim']).exists():self.r.archive(Path(state['claim']),status)

    def start(self,path,task,model,excerpts):
        resolved=self.approval.resolve(task)
        state={'task':task,'claim':str(path),'model':model,'phase':'ready','attempts':0,'round':0,
            'tests':[],'history':[],'mentor_replies':[],'expected':fingerprint(resolved['target']),
            'approval_sha256':resolved['approval_sha256'],'excerpts':excerpts,'started_ts':self.r.clock()}
        self.save(state)
        try:self.run(state)
        except Exception as exc:self.output(state,'failed','Coding service error: '+str(exc))

    def resolved(self,state):
        resolved=self.approval.resolve(state['task'])
        if resolved['approval_sha256']!=state['approval_sha256']:raise Conflict('Local approval changed during task; execution stopped')
        if fingerprint(resolved['target'])!=state['expected']:raise Conflict('Concurrent human edit detected; target was not overwritten')
        return resolved

    def run(self,state):
        limit=3 if state['round']==0 else 3+state['round']
        if state['attempts']>=limit or state['attempts']>=5:raise Conflict('Attempt ceiling reached; no additional attempt permitted')
        while state['attempts']<limit:
            if self.r.stopping:
                self.output(state,'failed','Local operator stopped coding; no automatic replay');return
            try:resolved=self.resolved(state)
            except Exception as exc:self.output(state,'failed',str(exc));return
            state['attempts']+=1;number=state['attempts'];identity=state['task']['id'];state['phase']='generating';self.save(state)
            before=resolved['target'].read_text(encoding='utf-8')
            prefix=self.r.root/'out'/(identity+'.attempt-'+str(number))
            prefix.with_suffix(prefix.suffix+'.before.txt').write_text(before,encoding='utf-8')
            self.r.journal('crew.coding.attempt','Attempt '+str(number)+'; mentor round '+str(state['round']),identity)
            self.r.status('coding',task=identity,model=state['model'])
            self.r.state.update('Coding attempt',jett_state='coding',progress=None,
                crew=[{'task_id':identity,'model':state['model'],'kind':'coding','progress':None}]);self.r.state.publish()
            entry={'attempt':number,'round':state['round'],'error':None,'model_summary':None,'test_log':None}
            patch_ok=False
            try:
                prompt=json.dumps({'goal':state['task']['instructions'],'target':state['task']['target_files'][0],
                    'CURRENT_SOURCE':before,'retrieved_context':state['excerpts'][:8000],
                    'previous_failure':state['history'][-1] if state['history'] else None,
                    'mentor_advice':state['mentor_replies'][-1]['reply'] if state['mentor_replies'] else None})
                options={'num_ctx':state['task'].get('num_ctx',16384),'num_predict':state['task'].get('num_predict',4096),
                         'think':state['task'].get('think',False)}
                response=self.r.ollama.code(state['model'],prompt,options)
                atomic_json(prefix.with_suffix(prefix.suffix+'.model.json'),response)
                if response.get('model')!=state['model']:raise ValueError('Ollama returned an unexpected model')
                if response.get('done') is not True or response.get('done_reason')=='length':raise ValueError('Model output incomplete or budget exhausted')
                answer=json.loads(response.get('response',''))
                entry['model_summary']=str(answer.get('summary',''))[:1000]
                updated=build_patch(before,answer,state['task']['target_files'][0],resolved['rule']['allowed_imports'])
                self.resolved(state)
                if self.r.stopping:raise Conflict('Local operator stopped before patch commit')
                state['phase']='patching';self.save(state)
                state['expected']=apply_locked(resolved['target'],updated,state['expected']);self.save(state);patch_ok=True
                self.r.journal('crew.coding.patch','Guarded commit '+state['expected']['sha256'],identity)
            except Conflict as exc:
                entry['error']=str(exc);state['history'].append(entry);self.output(state,'failed',str(exc));return
            except Exception as exc:entry['error']=type(exc).__name__+': '+str(exc)
            # Even a rejected model proposal gets real baseline/current-code test evidence.
            try:
                self.resolved(state);state['phase']='testing';self.save(state)
                log=prefix.with_suffix(prefix.suffix+'.test.log')
                test,execution=self.harness(self.approval,state['task'],state['expected'],log)
                test['name']='attempt '+str(number)+': '+test['name'];state['tests'].append(test)
                entry['execution']=execution;entry['test_log']=str(log)
                self.r.journal('crew.coding.tests',json.dumps({'attempt':number,**execution,'pass':test['pass']}),identity)
                self.resolved(state)
                if not test['pass']:
                    entry['error']=(entry['error']+'\n' if entry['error'] else '')+test['output_tail']
                passed=patch_ok and test['pass']
            except Exception as exc:
                entry['error']=(entry['error']+'\n' if entry['error'] else '')+type(exc).__name__+': '+str(exc)
                state['history'].append(entry);self.output(state,'failed',entry['error']);return
            state['history'].append(entry);state['phase']='between_attempts';self.save(state)
            if passed:
                self.output(state,'done','Approved patch passed actual tests on attempt '+str(number)+
                    (' after genuine Jett mentor round '+str(state['round']) if state['round'] else ' without mentorship'));return
        if state['round']>=2:self.output(state,'failed','Five attempts exhausted after two Jett mentor rounds; full history retained')
        else:self.stuck(state)

    def stuck(self,state):
        identity=state['task']['id'];round_number=state['round']+1
        context={**state['task'],'context_files':[str(self.approval.resolve(state['task'])['target'])]}
        excerpts=self.r.retrieval.prepare(context)
        if self.r.retrieval.used_embedding:self.r.ollama.unload('embeddinggemma');self.r.last_model=None
        packet={'kind':'mentor_request','task_id':identity,'goal_one_line':state['task']['title'],
            'tried':['Attempt '+str(row['attempt'])+': '+str(row['model_summary'] or row['error']) for row in state['history']],
            'error_exact':state['history'][-1]['error'] or 'Patch rejected despite baseline test success',
            'relevant_code':[{'path':str(self.approval.resolve(state['task'])['target']),'excerpt':excerpts[:10000]}],
            'specific_question':'What exact source change fixes '+state['task']['title']+' while passing the approved harness and preserving existing behavior?'}
        packet_path=self.r.root/'out'/(identity+'.mentor-request-r'+str(round_number)+'.json')
        atomic_json(packet_path,packet)
        state['waiting_since']=self.r.clock();state['packet_path']=str(packet_path)
        state['reply_path']=str(self.r.root/'in'/(identity+'.mentor-reply-r'+str(round_number)+'.json'))
        self.output(state,'stuck','Waiting for genuine Jett mentor round '+str(round_number)+' after '+str(state['attempts'])+' failed attempts')
        self.ensure_notification(state)

    def ensure_notification(self,state):
        if state.get('notification_round')==state['round']+1:return
        try:receipt=self.notifier(state['task']['id'],state['round']+1,Path(state['packet_path']),Path(state['reply_path']))
        except Exception as exc:
            self.r.journal('crew.mentor.notification_error',str(exc),state['task']['id']);return
        state['notification']=receipt;state['notification_round']=state['round']+1;self.save(state)
        atomic_json(self.r.root/'out'/(state['task']['id']+'.mentor-notification-r'+str(state['round']+1)+'.json'),receipt)
        self.r.journal('crew.mentor.notified',json.dumps(receipt),state['task']['id'])

    def accept(self,path,reply):
        identity=reply.get('task_id')
        if not isinstance(identity,str):raise ValueError('Mentor reply needs task_id')
        from .runner import IDENTITY
        if not IDENTITY.fullmatch(identity):raise ValueError('Invalid mentor task id')
        state=self.load(identity)
        if state['phase']!='waiting':raise ValueError('Task is not waiting for a mentor')
        if reply.get('kind')!='mentor_reply' or type(reply.get('round')) is not int or reply['round']!=state['round']+1 or reply['round']>2:
            raise ValueError('Mentor round is stale, duplicate or beyond two-round limit')
        for key in ('diagnosis','fix_steps','pitfalls'):
            if key=='diagnosis':
                if not isinstance(reply.get(key),str) or not reply[key].strip():raise ValueError('Mentor diagnosis is required')
            elif not isinstance(reply.get(key),list) or any(not isinstance(v,str) for v in reply[key]):raise ValueError('Invalid mentor '+key)
        if reply.get('code_skeleton') is not None and not isinstance(reply['code_skeleton'],str):raise ValueError('Invalid mentor code_skeleton')
        if Path(path).resolve()!=Path(state['reply_path']).resolve():raise ValueError('Mentor reply path does not match the requested round')
        stamp=Path(path).stat();raw=Path(path).read_bytes()
        provenance=self.provenance(path,stamp.st_mtime,state['waiting_since'])
        if not provenance:raise ValueError('No matching authenticated bridge fs.write evidence for mentor reply')
        if json.loads(raw)!=reply or Path(path).stat().st_mtime_ns!=stamp.st_mtime_ns:raise Conflict('Mentor file changed while reading')
        self.resolved(state)
        allowed,reason=self.r.allowed(state['model'])
        if not allowed:
            self.r.status('blocked',task=identity,model=state['model'],reason=reason);return
        claimed=self.r.root/'.working'/Path(path).name;Path(path).rename(claimed)
        evidence=self.r.root/'out'/(identity+'.mentor-reply-r'+str(reply['round'])+'.json')
        atomic_json(evidence,reply)
        state['mentor_replies'].append({'reply':reply,'provenance':provenance,'sha256':hashlib.sha256(raw).hexdigest()})
        state['round']=reply['round'];state['phase']='ready';self.save(state)
        self.r.archive(claimed,'done');self.r.journal('crew.mentor.accepted',json.dumps(provenance),identity)
        self.run(state)

    def recover(self,path,task):
        """Return true for owned coding claims, including safe interrupted failure."""
        if task.get('kind') not in ('coding','code_fix'):return False
        if Path(path).resolve().parent!=(self.r.root/'.working').resolve():raise Conflict('Recovery claim is outside owned queue')
        try:
            canonical=self.r.result(task['id'])
            if canonical and canonical['status'] in ('done','failed'):
                self.r.archive(path,canonical['status']);return True
            state=self.load(task['id'])
            if state['phase']=='waiting' and canonical and canonical['status']=='stuck':
                if canonical['attempts']!=state['attempts'] or canonical['tests']!=state['tests']:raise Conflict('Canonical result and coding checkpoint disagree; unknown outcome')
                self.resolved(state);self.ensure_notification(state);return True
            self.output(state,'failed','Interrupted coding attempt; outcome unknown, no automatic replay')
        except Exception as exc:
            self.r.journal('crew.coding.recovery_error',str(exc),task.get('id'))
            damaged=self.state_path(task['id'])
            if damaged.exists():damaged.rename(damaged.with_name(damaged.stem+'.preserved-'+uuid.uuid4().hex+'.json'))
            canonical=self.r.result(task['id'])
            minimal={'task':task,'claim':str(path),'model':canonical['model_used'] if canonical else '',
                'attempts':canonical['attempts'] if canonical else 0,'round':0,'tests':canonical['tests'] if canonical else [],
                'history':[{'recovery_error':str(exc)}]}
            self.output(minimal,'failed','Coding recovery needs local review: '+str(exc))
        return True

    def retry_notifications(self):
        for path in (self.r.root/'out').glob('*.coding-state.json'):
            try:
                state=json.loads(path.read_text())
                checked=self.load(path.name.removesuffix('.coding-state.json'))
            except Exception as exc:
                self.r.journal('crew.coding.checkpoint_invalid',str(exc),path.name)
                # Never trust a claim path from unverified JSON. Find the real owned input instead.
                identity=path.name.removesuffix('.coding-state.json');found=False
                for claim in (self.r.root/'.working').glob('*.json'):
                    try:task=json.loads(claim.read_text())
                    except (ValueError,OSError):continue
                    if task.get('id')==identity:self.recover(claim,task);found=True;break
                if not found:path.rename(path.with_name(path.stem+'.preserved-'+uuid.uuid4().hex+'.json'))
                continue
            if state.get('phase')=='waiting':
                state=checked
                try:self.resolved(state)
                except Exception as exc:self.output(state,'failed',str(exc));continue
                self.ensure_notification(state)
