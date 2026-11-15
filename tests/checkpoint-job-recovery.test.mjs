import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {_electron as electron} from 'playwright-core';
import os from 'node:os';
import path from 'node:path';

const {prepareCheckpoint}=createRequire(import.meta.url)('../dist-electron/checkpoint.cjs');

test('application restart records an interrupted checkpoint and recovery in job history',{skip:process.platform!=='win32',timeout:30000},async(t)=>{
 const root=await mkdtemp(path.join(os.tmpdir(),'jetstream-job-recovery-'));const project=path.join(root,'project'),data=path.join(root,'data'),store=path.join(root,'checkpoints');await mkdir(project);await mkdir(data);await mkdir(store);
 const target=path.join(project,'file.txt');await writeFile(target,'before');const checkpoint=await prepareCheckpoint(project,'file.txt',createHash('sha256').update('before').digest('hex'),Buffer.from('after'),store);
 let app;
 try{
  const launch=()=>electron.launch({args:['.',`--user-data-dir=${data}`],env:{...process.env,JETSTREAM_DISPOSABLE_APPLY_TEST:'1'}});
  app=await launch();await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},project);let win=await app.firstWindow();await win.evaluate(()=>window.coder.openProject());
  await app.evaluate(({app},args)=>{
   const {DatabaseSync}=process.getBuiltinModule('node:sqlite');const {createHash}=process.getBuiltinModule('node:crypto');const path=process.getBuiltinModule('node:path');
   const project=path.resolve(args.project),database=path.join(app.getPath('userData'),'projects',createHash('sha256').update(project).digest('hex')+'.sqlite'),db=new DatabaseSync(database),now=new Date().toISOString();
   db.prepare('INSERT INTO jobs(id,request,status,created) VALUES(?,?,?,?)').run(9,'recovery fixture','candidate approved',now);
   db.prepare('INSERT INTO local_candidates(id,job_id,proposal_id,root_path,source_hash,candidate_hash,created) VALUES(?,?,?,?,?,?,?)').run(3,9,1,project,'before-hash','after-hash',now);
   db.prepare('INSERT INTO candidate_checkpoints(id,candidate_id,approval_id,directory,created) VALUES(?,?,?,?,?)').run(77,3,1,args.directory,now);db.close();
  },{project,directory:checkpoint.directory});
  await writeFile(path.join(checkpoint.directory,'state-000001.json'),JSON.stringify({sequence:1,state:'applying'}));await writeFile(target,'later edit');await app.evaluate(({app})=>setTimeout(()=>app.exit(0),20));await app.close();app=await launch();
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},project);win=await app.firstWindow();await win.evaluate(()=>window.coder.openProject());
  const history=await win.evaluate(()=>window.coder.jobHistory(9));assert.ok(history.some(event=>event.kind==='checkpoint_recovery_required'));
  const status=()=>app.evaluate(({app},folder)=>{const {DatabaseSync}=process.getBuiltinModule('node:sqlite');const {createHash}=process.getBuiltinModule('node:crypto');const path=process.getBuiltinModule('node:path');const db=new DatabaseSync(path.join(app.getPath('userData'),'projects',createHash('sha256').update(path.resolve(folder)).digest('hex')+'.sqlite'));try{return db.prepare('SELECT status FROM jobs WHERE id=9').get().status}finally{db.close()}},project);
  assert.equal(await status(),'checkpoint recovery required; apply blocked');
  await app.evaluate(({app},folder)=>{const {DatabaseSync}=process.getBuiltinModule('node:sqlite');const {createHash}=process.getBuiltinModule('node:crypto');const path=process.getBuiltinModule('node:path');const db=new DatabaseSync(path.join(app.getPath('userData'),'projects',createHash('sha256').update(path.resolve(folder)).digest('hex')+'.sqlite'));db.prepare('UPDATE jobs SET status=? WHERE id=9').run('status changed');db.close()},project);
  await win.evaluate(()=>window.coder.openProject());assert.equal((await win.evaluate(()=>window.coder.jobHistory(9))).filter(event=>event.kind==='checkpoint_recovery_required').length,1);assert.equal(await status(),'checkpoint recovery required; apply blocked');
  const target2=path.join(project,'second.txt');await writeFile(target2,'before2');const checkpoint2=await prepareCheckpoint(project,'second.txt',createHash('sha256').update('before2').digest('hex'),Buffer.from('after2'),store);await writeFile(path.join(checkpoint2.directory,'state-000001.json'),JSON.stringify({sequence:1,state:'applying'}));await writeFile(target2,'after2');await app.evaluate(({app},{folder,directory})=>{const {DatabaseSync}=process.getBuiltinModule('node:sqlite');const {createHash}=process.getBuiltinModule('node:crypto');const path=process.getBuiltinModule('node:path');const db=new DatabaseSync(path.join(app.getPath('userData'),'projects',createHash('sha256').update(path.resolve(folder)).digest('hex')+'.sqlite'));db.prepare('INSERT INTO candidate_checkpoints(id,candidate_id,approval_id,directory,created) VALUES(?,?,?,?,?)').run(78,3,1,directory,new Date().toISOString());db.close()},{folder:project,directory:checkpoint2.directory});
  await assert.rejects(win.evaluate(id=>window.coder.disposableRecovery(id),77),/conflict/);assert.equal(await readFile(target,'utf8'),'later edit');
  await writeFile(target,'after');
  assert.equal(await win.evaluate(id=>window.coder.disposableRecovery(id),77),'reverted');assert.equal(await readFile(target,'utf8'),'before');
  const recovered=await win.evaluate(()=>window.coder.jobHistory(9));assert.ok(recovered.some(event=>event.kind==='checkpoint_recovered'));
  assert.equal(await status(),'checkpoint recovery required; apply blocked');assert.equal(await readFile(target2,'utf8'),'after2');
  assert.equal(await win.evaluate(id=>window.coder.disposableRecovery(id),78),'reverted');assert.equal(await readFile(target2,'utf8'),'before2');assert.equal(await status(),'checkpoint recovered; apply blocked');
  await app.evaluate(({app},folder)=>{const {DatabaseSync}=process.getBuiltinModule('node:sqlite');const {createHash}=process.getBuiltinModule('node:crypto');const path=process.getBuiltinModule('node:path');const db=new DatabaseSync(path.join(app.getPath('userData'),'projects',createHash('sha256').update(path.resolve(folder)).digest('hex')+'.sqlite'));db.prepare('UPDATE jobs SET status=? WHERE id=9').run('checkpoint recovery required; apply blocked');db.close()},project);
  await win.evaluate(()=>window.coder.openProject());assert.equal(await status(),'checkpoint recovered; apply blocked');assert.equal((await win.evaluate(()=>window.coder.jobHistory(9))).filter(event=>event.kind==='checkpoint_reconciled').length,1);
  await writeFile(target2,'second episode edit');await win.evaluate(()=>window.coder.openProject());assert.equal(await status(),'checkpoint recovery required; apply blocked');await writeFile(target2,'before2');await win.evaluate(()=>window.coder.openProject());assert.equal(await status(),'checkpoint recovered; apply blocked');assert.equal((await win.evaluate(()=>window.coder.jobHistory(9))).filter(event=>event.kind==='checkpoint_reconciled').length,2);await win.evaluate(()=>window.coder.openProject());assert.equal((await win.evaluate(()=>window.coder.jobHistory(9))).filter(event=>event.kind==='checkpoint_reconciled').length,2);
  await writeFile(path.join(checkpoint2.directory,'state-000001.json'),JSON.stringify({sequence:1,state:'invalid'}));await win.evaluate(()=>window.coder.openProject());assert.equal(await status(),'checkpoint inspection failed; apply blocked');assert.ok((await win.evaluate(()=>window.coder.jobHistory(9))).some(event=>event.kind==='checkpoint_inspection_failed'));
  const missingCheckpoint=path.join(store,'missing-checkpoint');await app.evaluate(({app},args)=>{const {DatabaseSync}=process.getBuiltinModule('node:sqlite');const {createHash}=process.getBuiltinModule('node:crypto');const path=process.getBuiltinModule('node:path');const db=new DatabaseSync(path.join(app.getPath('userData'),'projects',createHash('sha256').update(path.resolve(args.project)).digest('hex')+'.sqlite'));db.prepare('UPDATE candidate_checkpoints SET directory=? WHERE id=78').run(args.directory);db.close()}, {project,directory:missingCheckpoint});
  await win.evaluate(()=>window.coder.openProject());assert.equal(await status(),'checkpoint inspection failed; apply blocked');assert.ok((await win.evaluate(()=>window.coder.jobHistory(9))).some(event=>{try{return event.kind==='checkpoint_inspection_failed'&&JSON.parse(event.detail).directory===missingCheckpoint}catch{return false}}));
  await t.test('each recovered episode has a distinct reconciliation event',async()=>{const events=await win.evaluate(()=>window.coder.jobHistory(9));const episodes=events.filter(event=>event.kind==='checkpoint_reconciled').map(event=>JSON.parse(event.detail).episode).sort();assert.deepEqual(episodes,[1,2])});
 }finally{
  if(app){await app.evaluate(({app})=>setTimeout(()=>app.exit(0),20));await app.close()}const relative=path.relative(os.tmpdir(),root);if(!relative||relative.startsWith('..')||path.isAbsolute(relative))throw new Error('Unsafe cleanup path');await rm(root,{recursive:true,force:true,maxRetries:3,retryDelay:200});
 }
});
