import { _electron as electron } from 'playwright-core';
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises';
import assert from 'node:assert/strict';
import path from 'node:path';
import os from 'node:os';
const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-real-model-'));
const data=await mkdtemp(path.join(os.tmpdir(),'jetstream-real-data-'));
let app;
try {
  await writeFile(path.join(dir,'sample.js'),'export function add(a, b) { return a - b; }\n');
  app=await electron.launch({args:['.',`--user-data-dir=${data}`],env:{...process.env,JETSTREAM_DISPOSABLE_APPLY_TEST:'1'}});
  await app.evaluate(({dialog,app,BrowserWindow},folder)=>{globalThis._testOpenCount=0;globalThis._testLifecycle=[];app.on('window-all-closed',()=>globalThis._testLifecycle.push('window-all-closed'));for(const w of BrowserWindow.getAllWindows())w.on('closed',()=>globalThis._testLifecycle.push('window-closed'));dialog.showOpenDialog=async()=>{globalThis._testOpenCount++;globalThis._testLifecycle.push('open-dialog');return {canceled:false,filePaths:[folder]}}},dir);
  const win=await app.firstWindow();await app.evaluate(({BrowserWindow})=>{for(const w of BrowserWindow.getAllWindows())w.hide()});await win.evaluate(()=>{window._clickTrace=[];document.addEventListener('click',e=>{window._clickTrace.push({trusted:e.isTrusted,target:e.target?.closest('button')?.textContent||e.target?.tagName})},true)});await win.evaluate(()=>window.coder.openProject());
  await win.evaluate(()=>window.coder.addJob('Fix add so it returns the sum of its arguments.'));
  const scratch=await win.evaluate(()=>window.coder.prepareScratch(1));
  let proposal;try{proposal=await win.evaluate(()=>window.coder.proposeLocal(1,'qwen3.5:4b','sample.js'))}catch(error){console.error('Project lifecycle:',await app.evaluate(()=>({opens:globalThis._testOpenCount,events:globalThis._testLifecycle})).catch(()=>null),'clicks:',await win.evaluate(()=>window._clickTrace).catch(()=>null));throw error}
  assert.match(proposal.content,/a\s*\+\s*b/);
  const candidate=await win.evaluate(()=>window.coder.latestCandidate(1));assert.equal(candidate.proposalId,proposal.id);assert.match(await win.evaluate(id=>window.coder.candidateDiff(id),candidate.id),/a \+ b/);
  assert.equal(await readFile(path.join(scratch,'sample.js'),'utf8'),'export function add(a, b) { return a - b; }\n');
  assert.equal(await readFile(path.join(dir,'sample.js'),'utf8'),'export function add(a, b) { return a - b; }\n');
  await win.evaluate(()=>window.coder.openProject());
  assert.equal((await win.evaluate(()=>window.coder.latestProposal(1))).id,proposal.id);
  let review;try{review=await win.evaluate(id=>window.coder.reviewProposal(id,'qwen3.5:9b'),proposal.id)}catch(error){console.error('Project lifecycle:',await app.evaluate(()=>({opens:globalThis._testOpenCount,events:globalThis._testLifecycle})).catch(()=>null),'clicks:',await win.evaluate(()=>window._clickTrace).catch(()=>null));throw error}
  assert.ok(['approve','reject'].includes(review.verdict)); assert.ok(review.findings);
  if(review.verdict!=='approve')throw new Error('Real reviewer rejected the candidate: '+review.findings);
  await win.evaluate(()=>window.coder.configureVerification('Write-Output build-ok','Write-Output test-ok'));
  const checks=await win.evaluate(id=>window.coder.verifyJob(1,id),candidate.id);assert.ok(checks.every(x=>x.exit===0));
  const evidence=await win.evaluate(id=>window.coder.candidateEvidence(id),candidate.id);assert.deepEqual(evidence.reasons,[]);
  await win.evaluate(e=>window.coder.approveCandidate(e.candidateId,e.sourceHash,e.candidateHash,e.review.id,e.check.id),evidence);
  const checkpoint=await win.evaluate(id=>window.coder.prepareCheckpoint(id),candidate.id);
  const applied=await win.evaluate(id=>window.coder.disposableApply(id),candidate.id);assert.equal(applied.state,'checks_passed');assert.match(await readFile(path.join(dir,'sample.js'),'utf8'),/a\s*\+\s*b/);
  await win.evaluate(id=>window.coder.disposableRollback(id),checkpoint.id);assert.equal(await readFile(path.join(dir,'sample.js'),'utf8'),'export function add(a, b) { return a - b; }\n');
  const usage=await win.evaluate(()=>window.coder.localUsage());
  for(const model of ['qwen3.5:4b','qwen3.5:9b']){const row=usage.find(x=>x.model===model);assert.equal(row?.reported,1);assert.ok(row.promptTokens>0);assert.ok(row.completionTokens>0)}
  assert.equal((await win.evaluate(()=>window.coder.jobGate(1))).applyAvailable,false);
  console.log('Disposable real-model job passed with simulated approval, checks, test-only apply and rollback; production Apply remains blocked.');
} finally {
  if(app){const proc=app.process();await app.evaluate(({app})=>app.exit(0)).catch(()=>{});if(proc.exitCode===null)await new Promise(resolve=>proc.once('exit',resolve));}
  await rm(dir,{recursive:true,force:true});await rm(data,{recursive:true,force:true,maxRetries:3,retryDelay:200});
}
