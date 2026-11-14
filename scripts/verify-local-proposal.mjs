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
  app=await electron.launch({args:['.',`--user-data-dir=${data}`]});
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},dir);
  const win=await app.firstWindow();await win.evaluate(()=>window.coder.openProject());
  await win.evaluate(()=>window.coder.addJob('Fix add so it returns the sum of its arguments.'));
  const scratch=await win.evaluate(()=>window.coder.prepareScratch(1));
  const proposal=await win.evaluate(()=>window.coder.proposeLocal(1,'qwen3.5:4b','sample.js'));
  assert.match(proposal.content,/a\s*\+\s*b/);
  assert.equal(await readFile(path.join(scratch,'sample.js'),'utf8'),'export function add(a, b) { return a - b; }\n');
  assert.equal(await readFile(path.join(dir,'sample.js'),'utf8'),'export function add(a, b) { return a - b; }\n');
  await win.evaluate(()=>window.coder.openProject());
  assert.equal((await win.evaluate(()=>window.coder.latestProposal(1))).id,proposal.id);
  const review=await win.evaluate(id=>window.coder.reviewProposal(id,'qwen3.5:9b'),proposal.id);
  assert.ok(['approve','reject'].includes(review.verdict)); assert.ok(review.findings);
  assert.equal((await win.evaluate(()=>window.coder.jobGate(1))).applyAvailable,false);
  console.log('Real local proposal and distinct-model review passed; proposal survived reopening, scratch and project were unchanged, and Apply remains blocked.');
} finally {
  if(app){const proc=app.process();await app.evaluate(({app})=>app.exit(0)).catch(()=>{});if(proc.exitCode===null)await new Promise(resolve=>proc.once('exit',resolve));}
  await rm(dir,{recursive:true,force:true});await rm(data,{recursive:true,force:true,maxRetries:3,retryDelay:200});
}
