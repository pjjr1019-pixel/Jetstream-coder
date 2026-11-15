import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _electron as electron } from 'playwright-core';
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

test('job cancellation aborts only its local proposal request', { timeout: 60000 }, async () => {
 const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-cancel-project-'));
 const otherDir=await mkdtemp(path.join(os.tmpdir(),'jetstream-cancel-other-'));
 const data=await mkdtemp(path.join(os.tmpdir(),'jetstream-cancel-data-'));
 const file=path.join(dir,'sample.txt');
 await writeFile(file,'original');
 let app;
 try {
  app=await electron.launch({args:['.',`--user-data-dir=${data}`],env:{...process.env,JETSTREAM_DISPOSABLE_APPLY_TEST:'1'}});
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},dir);
  const win=await app.firstWindow();
  win.setDefaultTimeout(10000);
  await win.evaluate(()=>window.coder.openProject());
  await win.waitForFunction(()=>window.coder.listFiles().then(files=>files.includes('sample.txt')));
  await win.evaluate(async()=>{
   await window.coder.addJob('unrelated active chat');
   await window.coder.addJob('cancel proposal');
   await window.coder.addJob('cancel review');
   await window.coder.prepareScratch(1);
   await window.coder.prepareScratch(2);
   await window.coder.prepareScratch(3);
  });
  await app.evaluate(()=>{
   const realSetTimeout=globalThis.setTimeout;
   globalThis.setTimeout=(callback,delay,...args)=>{if(delay===120000||delay===180000)globalThis._requestTimeout=()=>callback(...args);return realSetTimeout(callback,delay,...args)};
   globalThis._manualAborted=false;
   globalThis._proposalAborted=false;
   globalThis._reviewAborted=false;
   globalThis._holdProposal=true;
   globalThis.fetch=async(url,options={})=>{
    if(String(url).endsWith('/api/tags'))return new Response(JSON.stringify({models:[{name:'fixture-model',digest:'fixture-digest'},{name:'fixture-reviewer',digest:'reviewer-digest'},{name:'manual',digest:'manual-digest'}]}),{status:200});
    if(String(url).endsWith('/api/chat')){
     const model=JSON.parse(options.body).model;
     if(model==='fixture-model'&&!globalThis._holdProposal)return new Response(JSON.stringify({model,done:true,done_reason:'stop',message:{content:JSON.stringify({explanation:'Fixture proposal',content:'changed'})}}),{status:200});
     const proposal=model==='fixture-model',review=model==='fixture-reviewer';
     if(proposal)globalThis._proposalStarted=true;else if(review)globalThis._reviewStarted=true;else globalThis._manualStarted=true;
     return await new Promise((resolve,reject)=>{
      const abort=()=>{if(proposal)globalThis._proposalAborted=true;else if(review)globalThis._reviewAborted=true;else globalThis._manualAborted=true;reject(new DOMException('Aborted','AbortError'))};
      if(options.signal.aborted)abort();else options.signal.addEventListener('abort',abort,{once:true});
     });
    }
    throw new Error(`Unexpected URL: ${url}`);
   };
  });
  await win.evaluate(()=>{window._manualResult=window.coder.askLocal('manual','hello').then(()=>'',error=>String(error))});
  await app.evaluate(async()=>{const deadline=Date.now()+10000;while(!globalThis._manualStarted&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,10));if(!globalThis._manualStarted)throw new Error('Manual request did not start')});
  await win.evaluate(()=>window.coder.cancelJob(1));
  assert.equal(await app.evaluate(()=>globalThis._manualAborted),false);
  await win.evaluate(()=>window.coder.cancelLocal());
  assert.equal(await app.evaluate(()=>globalThis._manualAborted),true);
  assert.match(await win.evaluate(()=>window._manualResult),/cancelled/i);
  await win.evaluate(()=>{window._proposalResult=window.coder.proposeLocal(2,'fixture-model','sample.txt').then(()=>'',error=>String(error))});
  await app.evaluate(async()=>{const deadline=Date.now()+10000;while(!globalThis._proposalStarted&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,10));if(!globalThis._proposalStarted)throw new Error('Proposal request did not start')});
  await win.evaluate(()=>window.coder.cancelJob(2));
  assert.equal(await app.evaluate(()=>globalThis._proposalAborted),true);
  assert.match(await win.evaluate(()=>window._proposalResult),/cancelled/i);
  const history=await win.evaluate(()=>window.coder.jobHistory(2));
  assert.ok(history.some(event=>event.kind==='cancelled'));
  assert.ok(history.some(event=>event.kind==='local_proposal_failed'&&event.detail.includes('Local request cancelled')));
  await app.evaluate(()=>{globalThis._holdProposal=false;globalThis._proposalAborted=false});
  const proposal=await win.evaluate(()=>window.coder.proposeLocal(3,'fixture-model','sample.txt'));
  await win.evaluate(id=>{window._reviewResult=window.coder.reviewProposal(id,'fixture-reviewer').then(()=>'',error=>String(error))},proposal.id);
  await app.evaluate(async()=>{const deadline=Date.now()+10000;while(!globalThis._reviewStarted&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,10));if(!globalThis._reviewStarted)throw new Error('Review request did not start')});
  await win.evaluate(()=>window.coder.cancelJob(3));
  assert.equal(await app.evaluate(()=>globalThis._reviewAborted),true);
  assert.match(await win.evaluate(()=>window._reviewResult),/cancelled/i);
  assert.ok((await win.evaluate(()=>window.coder.jobHistory(3))).some(event=>event.kind==='proposal_review_failed'&&event.detail==='Local request cancelled'));

  // Fire the actual request timeout callback without waiting for a model timeout.
  await app.evaluate(()=>{globalThis._manualStarted=false});
  await win.evaluate(()=>{window._manualResult=window.coder.askLocal('manual','timeout').then(()=>'',error=>String(error))});
  await app.evaluate(async()=>{while(!globalThis._manualStarted)await new Promise(resolve=>setTimeout(resolve,10));globalThis._requestTimeout()});
  assert.match(await win.evaluate(()=>window._manualResult),/timed out/i);

  await win.evaluate(async()=>{await window.coder.addJob('proposal timeout');await window.coder.prepareScratch(4)});
  await app.evaluate(()=>{globalThis._holdProposal=true;globalThis._proposalStarted=false});
  await win.evaluate(()=>{window._proposalResult=window.coder.proposeLocal(4,'fixture-model','sample.txt').then(()=>'',error=>String(error))});
  await app.evaluate(async()=>{while(!globalThis._proposalStarted)await new Promise(resolve=>setTimeout(resolve,10));globalThis._requestTimeout()});
  assert.match(await win.evaluate(()=>window._proposalResult),/timed out/i);
  assert.ok((await win.evaluate(()=>window.coder.jobHistory(4))).some(event=>event.kind==='local_proposal_failed'&&event.detail==='Local request timed out'));

  await win.evaluate(async()=>{await window.coder.addJob('review timeout');await window.coder.prepareScratch(5)});
  await app.evaluate(()=>{globalThis._holdProposal=false;globalThis._reviewStarted=false});
  const reviewProposal=await win.evaluate(()=>window.coder.proposeLocal(5,'fixture-model','sample.txt'));
  await win.evaluate(id=>{window._reviewResult=window.coder.reviewProposal(id,'fixture-reviewer').then(()=>'',error=>String(error))},reviewProposal.id);
  await app.evaluate(async()=>{while(!globalThis._reviewStarted)await new Promise(resolve=>setTimeout(resolve,10));globalThis._requestTimeout()});
  assert.match(await win.evaluate(()=>window._reviewResult),/timed out/i);
  assert.ok((await win.evaluate(()=>window.coder.jobHistory(5))).some(event=>event.kind==='proposal_review_failed'&&event.detail==='Local request timed out'));

  await win.evaluate(async()=>{await window.coder.addJob('old project request');await window.coder.prepareScratch(6)});
  await app.evaluate(()=>{globalThis._holdProposal=true;globalThis._proposalStarted=false;globalThis._proposalAborted=false});
  await win.evaluate(()=>{window._proposalResult=window.coder.proposeLocal(6,'fixture-model','sample.txt').then(()=>'',error=>String(error))});
  await app.evaluate(async()=>{while(!globalThis._proposalStarted)await new Promise(resolve=>setTimeout(resolve,10))});
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},otherDir);
  await win.evaluate(()=>window.coder.openProject());
  await win.evaluate(async()=>{for(let i=0;i<6;i++)await window.coder.addJob('new project job')});
  await win.evaluate(()=>window.coder.cancelJob(6));
  assert.equal(await app.evaluate(()=>globalThis._proposalAborted),false,'a matching job ID in another project must not cancel the old request');
  await win.evaluate(()=>window.coder.cancelLocal());
  assert.match(await win.evaluate(()=>window._proposalResult),/cancelled/i);
  assert.equal(await readFile(file,'utf8'),'original');
 } finally {
  if(app) {
   const proc=app.process();
   await app.evaluate(({app})=>app.exit(0)).catch(()=>{});
   if(proc.exitCode===null) {
    await Promise.race([new Promise(resolve=>proc.once('exit',resolve)),new Promise(resolve=>setTimeout(resolve,3000))]);
    if(proc.exitCode===null)proc.kill();
   }
  }
  await rm(dir,{recursive:true,force:true});
  await rm(otherDir,{recursive:true,force:true});
  await rm(data,{recursive:true,force:true});
 }
});
