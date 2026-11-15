import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import os from 'node:os';
import path from 'node:path';

const {prepareCheckpoint}=createRequire(import.meta.url)('../dist-electron/checkpoint.cjs');
const {inspectRecoveryFixture,recoverCheckpointFixture}=createRequire(import.meta.url)('../dist-electron/checkpoint-fixture.cjs');
const {readJournal}=createRequire(import.meta.url)('../dist-electron/checkpoint-journal.cjs');

test('actual process termination during post-apply checks requires inspection and exact-byte recovery',{skip:process.platform!=='win32',timeout:25000},async()=>{
 const root=await mkdtemp(path.join(os.tmpdir(),'jetstream-check-crash-'));let child;
 try{
  const project=path.join(root,'project'),store=path.join(root,'store');await mkdir(project);await mkdir(store);
  const target=path.join(project,'file.txt');await writeFile(target,'before');
  const checkpoint=await prepareCheckpoint(project,'file.txt',createHash('sha256').update('before').digest('hex'),Buffer.from('after'),store);
  const modulePath=path.resolve('dist-electron/checkpoint-transaction.cjs');
  const code=`const {runCheckpointTransaction}=require(process.env.JETSTREAM_MODULE);runCheckpointTransaction(process.env.JETSTREAM_CHECKPOINT,{authorize:async()=>true,checks:async()=>{console.log('CHECKS_STARTED');await new Promise(()=>{})}}).catch(e=>{console.error(e);process.exit(1)});setInterval(()=>{},1000)`;
  child=spawn(process.execPath,['-e',code],{env:{...process.env,JETSTREAM_MODULE:modulePath,JETSTREAM_CHECKPOINT:checkpoint.directory},windowsHide:true,stdio:['ignore','pipe','pipe']});
  let output='',errors='';child.stderr.on('data',data=>{errors+=String(data)});
  await new Promise((resolve,reject)=>{
   child.stdout.on('data',data=>{output+=String(data);if(output.includes('CHECKS_STARTED'))resolve()});
   child.once('error',reject);child.once('exit',code=>reject(new Error(`Worker exited ${code} before checks started: ${errors}`)));
  });
  const exited=new Promise(resolve=>child.once('exit',resolve));child.kill();await exited;
  assert.equal(await readFile(target,'utf8'),'after');
  assert.deepEqual(await readJournal(checkpoint.directory),['prepared','applying','applied','checking']);
  assert.deepEqual(await inspectRecoveryFixture(checkpoint.directory),{state:'checking',fileStatus:'after',needsInspection:true});
  await writeFile(target,'later edit');await assert.rejects(recoverCheckpointFixture(checkpoint.directory),/conflict/);assert.equal(await readFile(target,'utf8'),'later edit');
  await writeFile(target,'after');assert.equal(await recoverCheckpointFixture(checkpoint.directory),'reverted');assert.equal(await readFile(target,'utf8'),'before');
 }finally{
  if(child&&child.exitCode===null&&!child.killed)child.kill();
  const relative=path.relative(os.tmpdir(),root);if(!relative||relative.startsWith('..')||path.isAbsolute(relative))throw new Error('Unsafe cleanup path');
  await rm(root,{recursive:true,force:true,maxRetries:3,retryDelay:200});
 }
});
