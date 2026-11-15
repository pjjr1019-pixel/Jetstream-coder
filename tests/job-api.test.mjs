import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash, randomUUID} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const {JOB_API_ACTOR, JOB_API_VERSION, JetstreamJobApi, createJetstreamJobApi}=createRequire(import.meta.url)('../dist-electron/job-api.cjs');
const token='fixture-token-'+('x'.repeat(48));

async function fixture(t, overrides={}) {
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'jetstream-job-api-'));
  const ledger=new DatabaseSync(path.join(directory,'actions.sqlite'));
  let executions=0;
  const jobs=new Map([[1,{job:{id:1,request:'example',status:'scratch ready'},history:[{kind:'requested'}],candidate:{diff:'diff',review:{verdict:'approve'},checks:{build:'passed',test:'passed'},receipt:'receipt'}}]]);
  const operations={
    project:()=>({id:'project-hash',name:'fixture-project'}),
    jobs:(query)=>({jobs:[...jobs.values()].map(item=>item.job).filter(item=>!query||item.request.includes(query)),paused:false}),
    job:(id)=>jobs.get(id)??null,
    validateAction:()=>{},
    executeAction:async(action,actor)=>{executions++;return {action:action.action,actor,executions};},
    ...overrides,
  };
  const api=new JetstreamJobApi({ledger,operations,token,port:0});
  const status=await api.start();
  t.after(async()=>{await api.close();ledger.close();await fs.rm(directory,{recursive:true,force:true});});
  const base=`http://127.0.0.1:${status.port}/api/v1`;
  const call=(route,options={})=>fetch(base+route,{...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),'X-Jetstream-Project':'project-hash',...(options.headers||{}),Authorization:`Bearer ${token}`}});
  return {api,base,call,ledger,operations,get executions(){return executions;},directory,jobs};
}

function action(requestId, fields) {
  return {requestId,...fields};
}

test('actions require the expected project and cannot target a newly opened project',async t=>{
 const f=await fixture(t);
 const body=JSON.stringify(action(randomUUID(),{action:'cancel',jobId:1}));
 const missing=await fetch(f.base+'/actions',{method:'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'},body});
 assert.equal(missing.status,400);
 f.operations.project=()=>({id:'different-project',name:'new project'});
 assert.equal((await f.call('/actions',{method:'POST',body})).status,409);
 assert.equal(f.executions,0);
});

test('versioned read interface is authenticated and returns job pipeline evidence',async t=>{
  const f=await fixture(t);
  const unauthorized=await fetch(f.base);
  assert.equal(unauthorized.status,401);
  assert.equal(unauthorized.headers.get('x-jetstream-job-api-version'),String(JOB_API_VERSION));

  const status=await f.call('');
  assert.equal(status.status,200);
  assert.deepEqual(await status.json(),{apiVersion:1,project:{id:'project-hash',name:'fixture-project'}});
  const projects=await f.call('/projects');
  assert.deepEqual(await projects.json(),{projects:[{id:'project-hash',name:'fixture-project'}]});
  const jobs=await f.call('/jobs?query=example');
  assert.deepEqual((await jobs.json()).jobs,[{id:1,request:'example',status:'scratch ready'}]);
  const job=await f.call('/jobs/1');
  assert.deepEqual(await job.json(),f.jobs.get(1));
  assert.equal((await f.call('/api/v2/jobs')).status,404);
  assert.equal(f.api.status().host,'127.0.0.1');
});

test('every supported action is accepted and bound to the authenticated Deskhand actor',async t=>{
  const f=await fixture(t);
  const requests=[
    action(randomUUID(),{action:'submit',request:'add a test'}),
    action(randomUUID(),{action:'pause'}),
    action(randomUUID(),{action:'resume'}),
    action(randomUUID(),{action:'cancel',jobId:1}),
    action(randomUUID(),{action:'reject',candidateId:8}),
  ];
  for(const request of requests){
    const response=await f.call('/actions',{method:'POST',body:JSON.stringify(request)});
    assert.equal(response.status,200);
    const result=await response.json();
    assert.equal(result.state,'completed');
    assert.equal(result.actor,JOB_API_ACTOR);
    assert.equal(result.action,request.action);
  }
  assert.equal(f.executions,5);
});

test('duplicate submissions and reconnects replay one durable result; reused ids conflict',async t=>{
  const f=await fixture(t);
  const request=action(randomUUID(),{action:'submit',request:'create one job'});
  const first=await f.call('/actions',{method:'POST',body:JSON.stringify(request)});
  const result=await first.json();
  assert.equal(f.executions,1);
  const retry=await f.call('/actions',{method:'POST',body:JSON.stringify(request)});
  assert.deepEqual(await retry.json(),result);
  assert.equal(f.executions,1);
  const conflict=await f.call('/actions',{method:'POST',body:JSON.stringify({...request,request:'different job'})});
  assert.equal(conflict.status,409);
  const queried=await f.call(`/actions/${request.requestId}`);
  assert.equal((await queried.json()).state,'completed');

  await f.api.close();
  const reconnected=new JetstreamJobApi({ledger:f.ledger,operations:f.operations,token,port:0});
  t.after(()=>reconnected.close());
  const status=await reconnected.start();
  const replay=await fetch(`http://127.0.0.1:${status.port}/api/v1/actions`,{method:'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json','X-Jetstream-Project':'project-hash'},body:JSON.stringify(request)});
  assert.deepEqual(await replay.json(),result);
  assert.equal(f.executions,1);
  await reconnected.close();
});

test('unsupported and invalid actions are rejected without dispatch',async t=>{
  const f=await fixture(t,{validateAction:(item)=>{if(item.action==='cancel')throw new Error('Job is being verified')}});
  const invalid=await f.call('/actions',{method:'POST',body:JSON.stringify(action(randomUUID(),{action:'cancel',jobId:1}))});
  assert.equal(invalid.status,422);
  const unsupported=await f.call('/actions',{method:'POST',body:JSON.stringify(action(randomUUID(),{action:'apply',candidateId:8}))});
  assert.equal(unsupported.status,400);
  const badType=await f.call('/actions',{method:'POST',headers:{'Content-Type':'text/plain'},body:'{}'});
  assert.equal(badType.status,415);
  assert.equal(f.executions,0);
  assert.equal((f.ledger.prepare('SELECT COUNT(*) AS count FROM action_requests').get()).count,0);
});

test('uncertain execution is queryable and never re-executed on retry',async t=>{
  let attempts=0;
  const f=await fixture(t,{executeAction:()=>{attempts++;throw new Error('simulated lost acknowledgement')}});
  const request=action(randomUUID(),{action:'pause'});
  const first=await f.call('/actions',{method:'POST',body:JSON.stringify(request)});
  assert.equal(first.status,202);
  assert.equal((await first.json()).state,'uncertain');
  const retry=await f.call('/actions',{method:'POST',body:JSON.stringify(request)});
  assert.equal(retry.status,202);
  assert.equal((await retry.json()).state,'uncertain');
  const status=await f.call(`/actions/${request.requestId}`);
  assert.equal((await status.json()).state,'uncertain');
  assert.equal(attempts,1);
});

test('restart converts an unacknowledged in-progress request to uncertain',async t=>{
  const f=await fixture(t);
  const request=action('1f29360e-4000-4dc0-8d3f-7aa340a5994d',{action:'resume'});
  const requestHash=createHash('sha256').update(JSON.stringify(['project-hash',JSON.stringify({action:'resume'})])).digest('hex');
  f.ledger.prepare("INSERT INTO action_requests(actor,request_id,action,request_hash,state,created,updated) VALUES(?,?,?,?,'running',?,?)").run(
    JOB_API_ACTOR,request.requestId,'resume',requestHash,new Date().toISOString(),new Date().toISOString(),
  );
  await f.api.close();
  const restarted=new JetstreamJobApi({ledger:f.ledger,operations:f.operations,token,port:0});
  t.after(()=>restarted.close());
  const status=await restarted.start();
  const retry=await fetch(`http://127.0.0.1:${status.port}/api/v1/actions`,{method:'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json','X-Jetstream-Project':'project-hash'},body:JSON.stringify(request)});
  assert.equal(retry.status,202);
  assert.equal((await retry.json()).state,'uncertain');
  assert.equal(f.executions,0);
  await restarted.close();
});

test('credential descriptor stays outside renderer status and API binding cannot leave loopback',async t=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'jetstream-job-api-credentials-'));
  const ledger=new DatabaseSync(path.join(directory,'actions.sqlite'));
  const api=await createJetstreamJobApi(directory,ledger,{
    project:()=>null,jobs:()=>({jobs:[],paused:false}),job:()=>null,validateAction:()=>{},executeAction:()=>null,
  });
  t.after(async()=>{await api.close();ledger.close();await fs.rm(directory,{recursive:true,force:true});});
  const status=await api.start();
  const descriptor=JSON.parse(await fs.readFile(path.join(directory,'deskhand-job-api.json'),'utf8'));
  assert.equal(descriptor.apiVersion,1);
  assert.equal(descriptor.actor,JOB_API_ACTOR);
  assert.equal(descriptor.endpoint,`http://127.0.0.1:${status.port}/api/v1`);
  assert.equal(descriptor.token.length>=32,true);
  assert.equal('token' in status,false);
  assert.equal(status.host,'127.0.0.1');
});
