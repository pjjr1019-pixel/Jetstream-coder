import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {createRequire} from 'node:module';
import {DatabaseSync} from 'node:sqlite';
import {mkdtemp,rm} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import path from 'node:path';import os from 'node:os';
const run=promisify(execFile);
const {createJetstreamJobApi}=createRequire(import.meta.url)('../dist-electron/job-api.cjs');

test('Deskhand native client reaches the real API and retries an action without duplicating it',{timeout:60000},async t=>{
 try{await run('dotnet',['--version'])}catch{t.skip('.NET 8 SDK required for native adapter integration');return}
 const root=path.resolve('integrations/deskhand/windows/Jetstream.Adapter.Specs');
 await run('dotnet',['build',root,'-c','Release','--ignore-failed-sources','-v','quiet'],{timeout:30000,windowsHide:true});
 const dll=path.join(root,'bin/Release/net8.0/Jetstream.Adapter.Specs.dll');
 const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-native-adapter-'));const ledger=new DatabaseSync(path.join(dir,'actions.sqlite'));let executions=0;
 const projectId='a'.repeat(64);let active=projectId;
 const api=await createJetstreamJobApi(dir,ledger,{project:()=>({id:active,name:'fixture'}),jobs:()=>({jobs:[],paused:false}),job:()=>null,validateAction:()=>{},executeAction:()=>({count:++executions})});
 try{
  await api.start();const descriptor=path.join(dir,'deskhand-job-api.json');const call=async command=>JSON.parse((await run('dotnet',[dll,descriptor,JSON.stringify(command)],{windowsHide:true})).stdout);
  assert.equal((await call({op:'jetstream_status'})).project.id,projectId);
  const action={op:'jetstream_action',projectId,requestId:randomUUID(),action:'submit',request:'one task'};
  const first=await call(action);assert.equal(first.state,'completed');assert.deepEqual(await call(action),first);assert.equal(executions,1);
  active='b'.repeat(64);
  await assert.rejects(call({...action,requestId:randomUUID()}));assert.equal(executions,1);
  assert.equal((await call({op:'jetstream_outcome',requestId:action.requestId})).state,'completed');
 }finally{await api.close();ledger.close();await rm(dir,{recursive:true,force:true})}
});
