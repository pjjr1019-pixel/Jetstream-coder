import {test} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import { _electron as electron } from 'playwright-core';
import {mkdtemp, writeFile, readFile, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

test('Deskhand API fixture reads real Jetstream job evidence and records idempotent actions', {timeout:90000}, async()=>{
 const project=await mkdtemp(path.join(os.tmpdir(),'jetstream-job-api-project-'));
 const data=await mkdtemp(path.join(os.tmpdir(),'jetstream-job-api-data-'));
 await writeFile(path.join(project,'sample.txt'),'before');
 let app;
 try{
  app=await electron.launch({args:['.',`--user-data-dir=${data}`],env:{...process.env,JETSTREAM_DISABLE_LAPTOP_AGENT:'1'}});
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},project);
  const win=await app.firstWindow();win.setDefaultTimeout(15000);
  if(await win.getByRole('button',{name:'Open changelog'}).count()){await win.getByRole('button',{name:'Open changelog'}).click();await win.getByRole('button',{name:'Close changelog'}).click();}
  await win.getByRole('button',{name:'Open a project'}).click();
  await win.locator('.project-name').waitFor();

  let descriptor;
  for(let attempt=0;attempt<50;attempt++){
   try{descriptor=JSON.parse(await readFile(path.join(data,'deskhand-job-api.json'),'utf8'));break}catch{await new Promise(resolve=>setTimeout(resolve,100));}
  }
  assert.ok(descriptor?.endpoint);
  assert.equal(descriptor.apiVersion,1);
  assert.equal(descriptor.actor,'deskhand-host');
  const currentProject=await fetch(descriptor.endpoint,{headers:{Authorization:`Bearer ${descriptor.token}`}}).then(r=>r.json());
  const api=(route,options={})=>fetch(descriptor.endpoint+route,{...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),Authorization:`Bearer ${descriptor.token}`,'X-Jetstream-Project':currentProject.project.id,...(options.headers||{})}});
  const unauthorized=await fetch(descriptor.endpoint);
  assert.equal(unauthorized.status,401);
  const rendererProviders=await win.evaluate(()=>window.coder.providers());
  const apiProvider=rendererProviders.find(provider=>provider.name==='Jetstream Job API');
  assert.equal(apiProvider.status,'Available');
  assert.equal(JSON.stringify(rendererProviders).includes(descriptor.token),false);

  const submit={requestId:randomUUID(),action:'submit',request:'Improve sample text'};
  const created=await api('/actions',{method:'POST',body:JSON.stringify(submit)});
  assert.equal(created.status,200);
  const accepted=await created.json();
  const jobId=accepted.result.jobId;
  assert.equal(typeof jobId,'number');
  const duplicate=await api('/actions',{method:'POST',body:JSON.stringify(submit)});
  assert.deepEqual(await duplicate.json(),accepted);

  await win.evaluate(id=>window.coder.prepareScratch(id),jobId);
  await win.evaluate(()=>window.coder.configureVerification('Write-Output BUILD_PRIVATE_MARKER','Write-Output TEST_PRIVATE_MARKER'));
  await app.evaluate(()=>{
   globalThis.fetch=async(url,options={})=>{
    if(String(url).endsWith('/api/tags'))return new Response(JSON.stringify({models:[{name:'writer',digest:'writer-digest'},{name:'reviewer',digest:'reviewer-digest'}]}),{status:200});
    if(String(url).endsWith('/api/chat')){
     const request=JSON.parse(options.body),content=request.model==='writer'
      ?JSON.stringify({explanation:'Fixture proposal',content:'after'})
      :JSON.stringify({verdict:'approve',findings:'The diff matches the request.'});
     return new Response(JSON.stringify({model:request.model,done:true,done_reason:'stop',prompt_eval_count:3,eval_count:2,message:{content}}),{status:200});
    }
    throw new Error(`Unexpected URL: ${url}`);
   };
  });
  const proposal=await win.evaluate(id=>window.coder.proposeLocal(id,'writer','sample.txt'),jobId);
  const candidate=await win.evaluate(id=>window.coder.latestCandidate(id),jobId);
  assert.equal(candidate.proposalId,proposal.id);
  await win.evaluate(id=>window.coder.reviewProposal(id,'reviewer'),proposal.id);
  await win.evaluate((input)=>window.coder.verifyJob(input.jobId,input.candidateId),{jobId,candidateId:candidate.id});

  const detailResponse=await api(`/jobs/${jobId}`);
  assert.equal(detailResponse.status,200);
  const detail=await detailResponse.json();
  assert.match(detail.candidate.diff,/before/);
  assert.match(detail.candidate.diff,/after/);
  assert.equal(detail.candidate.review.verdict,'approve');
  assert.equal(detail.candidate.checksPassed,true);
  assert.equal('path' in detail.candidate,false);
  assert.equal(JSON.stringify(detail).includes('BUILD_PRIVATE_MARKER'),false);
  assert.equal(JSON.stringify(detail).includes('TEST_PRIVATE_MARKER'),false);
  assert.ok(detail.candidate.checks.results.every(result=>!('command' in result)&&!('output' in result)));
  assert.match(detail.receipt,/Project Apply: unavailable/);
  assert.equal(detail.applyAvailable,false);

  const reject={requestId:randomUUID(),action:'reject',candidateId:candidate.id};
  const rejected=await api('/actions',{method:'POST',body:JSON.stringify(reject)});
  assert.equal((await rejected.json()).state,'completed');
  const history=await api(`/jobs/${jobId}`);
  const afterReject=await history.json();
  assert.ok(afterReject.history.some(event=>event.kind==='deskhand_action'&&JSON.parse(event.detail).actor==='deskhand-host'&&JSON.parse(event.detail).action==='reject'&&JSON.parse(event.detail).outcome==='completed'));
  assert.ok(afterReject.candidate.blockedReasons.some(reason=>reason.includes('rejected')));

  const pause={requestId:randomUUID(),action:'pause'};
  assert.equal((await api('/actions',{method:'POST',body:JSON.stringify(pause)})).status,200);
  assert.equal((await (await api('/jobs')).json()).paused,true);
  const resume={requestId:randomUUID(),action:'resume'};
  assert.equal((await api('/actions',{method:'POST',body:JSON.stringify(resume)})).status,200);
  assert.equal((await (await api('/jobs')).json()).paused,false);

  const cancelJob=await api('/actions',{method:'POST',body:JSON.stringify({requestId:randomUUID(),action:'submit',request:'Cancel this job'})});
  const secondId=(await cancelJob.json()).result.jobId;
  const cancel={requestId:randomUUID(),action:'cancel',jobId:secondId};
  assert.equal((await api('/actions',{method:'POST',body:JSON.stringify(cancel)})).status,200);
  assert.equal((await (await api(`/jobs/${secondId}`)).json()).job.status,'cancelled');
  const unsupported=await api('/actions',{method:'POST',body:JSON.stringify({requestId:randomUUID(),action:'apply',candidateId:candidate.id})});
  assert.equal(unsupported.status,400);
  assert.equal(await readFile(path.join(project,'sample.txt'),'utf8'),'before');
 }finally{
  if(app)await app.close().catch(()=>{});
  await rm(project,{recursive:true,force:true});
  await rm(data,{recursive:true,force:true});
 }
});
