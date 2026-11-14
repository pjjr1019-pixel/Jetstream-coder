import { _electron as electron } from 'playwright-core';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import assert from 'node:assert/strict';
import path from 'node:path';
import os from 'node:os';
const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-crash-project-'));
const data=await mkdtemp(path.join(os.tmpdir(),'jetstream-crash-data-'));
let app;
async function launch(){const a=await electron.launch({args:['.',`--user-data-dir=${data}`]});await a.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]})},dir);return a}
try {
 await writeFile(path.join(dir,'slow.txt'),'fixture');app=await launch();let win=await app.firstWindow();await win.evaluate(()=>window.coder.openProject());await win.evaluate(()=>window.coder.addJob('Crash recovery fixture'));
 await app.evaluate(()=>{const fs=process.getBuiltinModule('node:fs/promises');const original=fs.readFile;fs.readFile=async(...args)=>{if(String(args[0]).endsWith('slow.txt'))await new Promise(()=>{});return original(...args)}});
 await win.evaluate(()=>{window.coder.prepareScratch(1).catch(()=>{})});await win.waitForFunction(async()=>((await window.coder.jobs()).jobs[0].status==='preparing scratch'),null,{timeout:10000});
 const proc=app.process();await app.evaluate(()=>process.exit(99)).catch(()=>{});if(proc.exitCode===null)await new Promise(resolve=>proc.once('exit',resolve));app=undefined;
 app=await launch();win=await app.firstWindow();await win.evaluate(()=>window.coder.openProject());const jobs=await win.evaluate(()=>window.coder.jobs());assert.match(jobs.jobs[0].status,/scratch preparation interrupted/);assert.ok((await win.evaluate(()=>window.coder.jobHistory(1))).some(e=>e.kind==='scratch_interrupted'));console.log('Actual forced process exit recovered: interrupted scratch reported.');
} finally {
 if(app){const proc=app.process();await app.evaluate(({app})=>app.exit(0)).catch(()=>{});if(proc.exitCode===null)await new Promise(resolve=>proc.once('exit',resolve));}
 for(const target of [dir,data]){const rel=path.relative(os.tmpdir(),target);if(!rel||rel.startsWith('..')||path.isAbsolute(rel))throw new Error('Unsafe temporary cleanup path');await rm(target,{recursive:true,force:true,maxRetries:5,retryDelay:200})}
}
