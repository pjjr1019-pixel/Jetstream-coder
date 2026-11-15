import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _electron as electron } from 'playwright-core';
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

test('unsaved editor buffers block candidate approval and checkpoint preparation', { timeout: 60000 }, async () => {
 const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-editor-approval-project-'));
 const other=await mkdtemp(path.join(os.tmpdir(),'jetstream-editor-approval-other-'));
 const data=await mkdtemp(path.join(os.tmpdir(),'jetstream-editor-approval-data-'));
 const file=path.join(dir,'sample.txt');
 await writeFile(file,'before');
 let app;
 try {
  app=await electron.launch({args:['.',`--user-data-dir=${data}`],env:{...process.env,JETSTREAM_DISPOSABLE_APPLY_TEST:'1'}});
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},dir);
  const win=await app.firstWindow();
  win.setDefaultTimeout(10000);
  win.on('dialog',dialog=>dialog.accept());
  if(await win.getByRole('button',{name:'Open changelog'}).count()) {
   await win.getByRole('button',{name:'Open changelog'}).click();
   await win.getByRole('button',{name:'Close changelog'}).click();
  }
  await win.getByRole('button',{name:'Open a project'}).click();
  await win.locator('.project-name').waitFor();
  await win.getByRole('button',{name:'Jobs',exact:true}).click();
  await win.getByPlaceholder('Describe a job for Muse').fill('Change sample text');
  await win.getByRole('button',{name:'Add job'}).click();
  await win.getByText(/Change sample text/).waitFor();
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
  const job=await win.evaluate(async()=>{
   await window.coder.prepareScratch(1);
   await window.coder.configureVerification('Write-Output build-ok','Write-Output test-ok');
   return window.coder.proposeLocal(1,'writer','sample.txt');
  });
  const candidate=await win.evaluate(()=>window.coder.latestCandidate(1));
  assert.equal(candidate.proposalId,job.id);
  await win.evaluate(id=>window.coder.reviewProposal(id,'reviewer'),job.id);
  const checks=await win.evaluate(id=>window.coder.verifyJob(1,id),candidate.id);
  assert.equal(checks.length,2);
  assert.ok(checks.every(result=>result.exit===0));
  await win.waitForFunction(async id=>(await window.coder.candidateEvidence(id)).reasons.every(reason=>!reason.includes('editor state is unknown')),candidate.id);
  const ready=await win.evaluate(id=>window.coder.candidateEvidence(id),candidate.id);
  assert.deepEqual(ready.reasons,[]);

  await win.locator('.file').filter({hasText:'sample.txt'}).click();
  await win.locator('.view-lines').click();
  await win.keyboard.press('Control+A');
  await win.keyboard.insertText('unsaved editor text');
  await win.waitForFunction(async id=>(await window.coder.candidateEvidence(id)).reasons.some(reason=>reason.includes('unsaved editor changes')),candidate.id);
  const dirtyEvidence=await win.evaluate(id=>window.coder.candidateEvidence(id),candidate.id);
  assert.ok(dirtyEvidence.reasons.some(reason=>reason.includes('sample.txt')));
  await assert.rejects(win.evaluate(e=>window.coder.approveCandidate(e.candidateId,e.sourceHash,e.candidateHash,e.review.id,e.check.id),dirtyEvidence),/incomplete/);
  await assert.rejects(win.evaluate(id=>window.coder.prepareCheckpoint(id),candidate.id),/incomplete/);

  await win.getByText('Job details and actions').last().click();
  await win.getByRole('button',{name:'Review candidate evidence'}).last().click();
  const dialog=win.getByRole('dialog',{name:'Candidate evidence'});
  await dialog.getByText(/Save or discard unsaved editor changes/).waitFor();
  assert.equal(await dialog.getByRole('button',{name:'Approve candidate'}).isDisabled(),true);
  await dialog.getByRole('button',{name:'Close candidate evidence'}).click();

  await win.getByRole('button',{name:'Reload',exact:true}).click();
  await win.waitForFunction(async id=>(await window.coder.candidateEvidence(id)).reasons.every(reason=>!reason.includes('unsaved editor changes')),candidate.id);
  const cleanEvidence=await win.evaluate(id=>window.coder.candidateEvidence(id),candidate.id);
  assert.deepEqual(cleanEvidence.reasons,[]);
  await win.evaluate(e=>window.coder.approveCandidate(e.candidateId,e.sourceHash,e.candidateHash,e.review.id,e.check.id),cleanEvidence);
  await app.evaluate(()=>{const fs=process.getBuiltinModule('node:fs/promises');const original=fs.open;fs.open=async(...args)=>{if(String(args[0]).endsWith('before.bin')&&!globalThis._checkpointHeld){globalThis._checkpointHeld=true;await new Promise(resolve=>globalThis._releaseCheckpoint=resolve)}return original(...args)}});
  await win.evaluate(id=>{window._checkpointAttempt=window.coder.prepareCheckpoint(id).then(()=>'',error=>String(error))},candidate.id);
  await app.evaluate(async()=>{const deadline=Date.now()+10000;while(!globalThis._checkpointHeld&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,10));if(!globalThis._checkpointHeld)throw new Error('Checkpoint preparation did not reach the barrier')});
  await win.locator('.view-lines').click();await win.keyboard.press('Control+A');await win.keyboard.insertText('changed during checkpoint');
  await win.waitForFunction(async id=>(await window.coder.candidateEvidence(id)).reasons.some(reason=>reason.includes('unsaved editor changes')),candidate.id);
  await app.evaluate(()=>globalThis._releaseCheckpoint());
  assert.match(await win.evaluate(()=>window._checkpointAttempt),/Evidence changed|editor state changed/);
  const failed=(await win.evaluate(()=>window.coder.jobHistory(1))).find(event=>event.kind==='checkpoint_failed');
  assert.ok(failed,'retained checkpoint must be recorded');
  assert.ok(JSON.parse(failed.detail).directory);
  await win.getByRole('button',{name:'Reload',exact:true}).click();
  await win.waitForFunction(async id=>(await window.coder.candidateEvidence(id)).reasons.every(reason=>!reason.includes('unsaved editor changes')),candidate.id);
  const checkpoint=await win.evaluate(id=>window.coder.prepareCheckpoint(id),candidate.id);
  assert.ok(checkpoint.id);
  assert.ok((await win.evaluate(()=>window.coder.jobHistory(1))).some(event=>event.kind==='candidate_approved'));
  assert.equal((await win.evaluate(()=>window.coder.jobGate(1))).applyAvailable,false);
  assert.equal(await readFile(file,'utf8'),'before');
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},other);
  const oldSession=await win.evaluate(()=>window.coder.openProjectSession());
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},dir);
  const newSession=await win.evaluate(()=>window.coder.openProjectSession());
  assert.notEqual(oldSession.session,newSession.session);
  await assert.rejects(win.evaluate(session=>window.coder.setUnsavedFiles(session,[]),oldSession.session),/outdated project session/);
  assert.ok((await win.evaluate(id=>window.coder.candidateEvidence(id),candidate.id)).reasons.some(reason=>reason.includes('editor state is unknown')));
  await win.evaluate(session=>window.coder.setUnsavedFiles(session,['sample.txt']),newSession.session);
  await assert.rejects(win.evaluate(session=>window.coder.setUnsavedFiles(session,[]),oldSession.session),/outdated project session/);
  assert.ok((await win.evaluate(id=>window.coder.candidateEvidence(id),candidate.id)).reasons.some(reason=>reason.includes('unsaved editor changes')));
 } finally {
  if(app) {
   const proc=app.process();
   await app.evaluate(({app})=>app.exit(0)).catch(()=>{});
   if(proc.exitCode===null)await Promise.race([new Promise(resolve=>proc.once('exit',resolve)),new Promise(resolve=>setTimeout(resolve,3000))]);
   if(proc.exitCode===null)proc.kill();
  }
  await rm(dir,{recursive:true,force:true});
  await rm(other,{recursive:true,force:true});
  await rm(data,{recursive:true,force:true});
 }
});
