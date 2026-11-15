// Authorized real-phone acceptance only; approval is simulated in a disposable project.
import {_electron as electron} from 'playwright-core';
import {mkdtemp,writeFile,readFile,mkdir,rm,access} from 'node:fs/promises';
import os from 'node:os';import path from 'node:path';import assert from 'node:assert/strict';
if(!process.argv.includes('--live-phone'))throw new Error('Requires --live-phone');
const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-phone-job-'));
const request='Phone prototype test: fix sample.js add to return a + b.';
await mkdir('.cache',{recursive:true});await rm('.cache/phone-job-finish',{force:true});
await writeFile(path.join(dir,'sample.js'),'export function add(a, b) { return a - b; }\n');
await writeFile(path.join(dir,'package.json'),JSON.stringify({type:'module'}));
await writeFile(path.join(dir,'sample.test.js'),"import test from 'node:test';import assert from 'node:assert/strict';import {add} from './sample.js';test('sum',()=>assert.equal(add(2,3),5));\n");
let app;
try{
 app=await electron.launch({args:['.'],env:{...process.env,JETSTREAM_DISABLE_LAPTOP_AGENT:'1',JETSTREAM_DISPOSABLE_APPLY_TEST:'1'}});
 await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},dir);
 const win=await app.firstWindow();await win.evaluate(async()=>{const opened=await window.coder.openProjectSession();await window.coder.setUnsavedFiles(opened.session,[])});
 await win.evaluate(()=>window.coder.configureVerification('node --check sample.js','node --test sample.test.js'));
 await writeFile('.cache/phone-job-state.json',JSON.stringify({project:dir,request,ready:true}));console.log('WAITING_FOR_PHONE_JOB '+dir);
 let job;for(let attempt=0;attempt<150;attempt++){job=(await win.evaluate(()=>window.coder.jobs())).jobs.find(row=>row.request===request);if(job)break;await new Promise(r=>setTimeout(r,2000))}
 assert.ok(job,'No matching phone-originated job arrived');console.log('PHONE_JOB_RECEIVED '+job.id);
 await win.evaluate(id=>window.coder.prepareScratch(id),job.id);
 const baseline=await win.evaluate(id=>window.coder.verifyJob(id),job.id);assert.ok(baseline.some(check=>check.exit!==0));
 const proposal=await win.evaluate(id=>window.coder.proposeLocal(id,'qwen3.5:4b','sample.js'),job.id);
 const candidate=await win.evaluate(id=>window.coder.latestCandidate(id),job.id);
 const review=await win.evaluate(id=>window.coder.reviewProposal(id,'qwen3.5:9b'),proposal.id);assert.equal(review.verdict,'approve');
 const checks=await win.evaluate(input=>window.coder.verifyJob(input.job,input.candidate),{job:job.id,candidate:candidate.id});assert.ok(checks.every(check=>check.exit===0));
 const evidence=await win.evaluate(id=>window.coder.candidateEvidence(id),candidate.id);assert.deepEqual(evidence.reasons,[]);assert.notEqual(evidence.authorDigest,evidence.review.reviewer_digest);
 await win.evaluate(e=>window.coder.approveCandidate(e.candidateId,e.sourceHash,e.candidateHash,e.review.id,e.check.id),evidence);
 const checkpoint=await win.evaluate(id=>window.coder.prepareCheckpoint(id),candidate.id);
 const applied=await win.evaluate(id=>window.coder.disposableApply(id),candidate.id);assert.equal(applied.state,'checks_passed');
 await win.evaluate(id=>window.coder.disposableRollback(id),checkpoint.id);assert.match(await readFile(path.join(dir,'sample.js'),'utf8'),/a - b/);
 assert.equal((await win.evaluate(id=>window.coder.jobGate(id),job.id)).applyAvailable,false);
 const report={date:new Date().toISOString(),phoneSubmitted:true,jobId:job.id,authorDigest:evidence.authorDigest,reviewer:review.model,reviewerDigest:evidence.review.reviewer_digest,review:review.verdict,checks:checks.map(check=>({kind:check.kind,exit:check.exit})),simulatedApproval:true,disposableApply:applied.state,rollbackRestored:true,productionApply:false,museCoordinator:false};
 await mkdir('docs/acceptance',{recursive:true});await writeFile('docs/acceptance/phone-job-2026-11-15.json',JSON.stringify(report,null,2));
 await writeFile('.cache/phone-job-state.json',JSON.stringify({project:dir,request,ready:true,complete:true,jobId:job.id}));console.log('PHONE_JOB_PASSED '+JSON.stringify(report));
 for(let attempt=0;attempt<180;attempt++){try{await access('.cache/phone-job-finish');break}catch{}await new Promise(r=>setTimeout(r,2000))}
}finally{if(app)await app.evaluate(({app})=>app.exit(0)).catch(()=>{});await rm(dir,{recursive:true,force:true,maxRetries:4,retryDelay:200});}
