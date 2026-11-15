import {test} from 'node:test';
import assert from 'node:assert/strict';
import {_electron as electron} from 'playwright-core';
import {mkdtemp,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

test('phone shortcut opens integrated settings and routes pairing and remote controls', {timeout:60000}, async()=>{
 const data=await mkdtemp(path.join(os.tmpdir(),'jetstream-phone-settings-'));
 let app;
 try{
  app=await electron.launch({args:['.',`--user-data-dir=${data}`],env:{...process.env,JETSTREAM_DISABLE_LAPTOP_AGENT:'1'}});
  await app.evaluate(({ipcMain})=>{
   const state={network:{state:'NeedsLogin',needsLogin:true},paused:false,phones:[],approvals:[],orientation:false};
   globalThis.phoneCommands=[];
   for(const channel of ['deskhand:control','deskhand:login','deskhand:pair-phone'])ipcMain.removeHandler(channel);
   ipcMain.handle('deskhand:control',(_event,command)=>{
    globalThis.phoneCommands.push(command);
    if(command.operation==='pause')state.paused=command.enabled;
    if(command.operation==='pair-decision'){
     state.phones=[{id:'phone-1',name:'Test phone',control:command.decision==='control',connected:true}];
     state.pairing=null;
    }
    if(command.operation==='phone-control')state.phones[0].control=command.enabled;
    return command.operation==='status'?state:true;
   });
   ipcMain.handle('deskhand:login',()=>{state.network={state:'Running',needsLogin:false,address:'100.64.0.1'};return true});
   ipcMain.handle('deskhand:pair-phone',()=>{
    state.pairing={id:'pair-1',name:'Test phone',code:'123456'};
    return {png:'fixture',expires:new Date(Date.now()+60000).toISOString()};
   });
  });
  const win=await app.firstWindow();win.setDefaultTimeout(15000);
  await win.getByRole('button',{name:'Pair a phone',exact:true}).click();
  await win.getByRole('dialog',{name:'Settings',exact:true}).waitFor();
  await win.getByRole('heading',{name:'Phone connection',exact:true}).waitFor();
  await win.getByRole('button',{name:'Sign in to Tailscale',exact:true}).click();
  await win.getByRole('heading',{name:'Approve phone',exact:true}).waitFor();
  await win.getByText('123456',{exact:true}).waitFor();
  await win.getByRole('button',{name:'Allow control',exact:true}).click();
  await win.getByText('Test phone · Connected · Control allowed',{exact:true}).waitFor();
  await win.getByRole('button',{name:'Block control',exact:true}).click();
  await win.getByText('Test phone · Connected · View only',{exact:true}).waitFor();
  await win.getByRole('button',{name:'Pause remote access',exact:true}).click();
  await win.getByRole('button',{name:'Resume remote access',exact:true}).click();
  await win.getByRole('button',{name:'Workspace',exact:true}).click();
  await win.getByRole('heading',{name:'Local models',exact:true}).waitFor();
  await win.getByRole('button',{name:'Phone connection',exact:true}).click();
  await win.getByRole('heading',{name:'Phone connection',exact:true}).waitFor();
  const commands=await app.evaluate(()=>globalThis.phoneCommands);
  assert.ok(commands.some(c=>c.operation==='pair-decision'&&c.id==='pair-1'&&c.decision==='control'));
  assert.ok(commands.some(c=>c.operation==='phone-control'&&c.id==='phone-1'&&c.enabled===false));
  assert.deepEqual(commands.filter(c=>c.operation==='pause').map(c=>c.enabled),[true,false]);
 }finally{
  if(app)await app.close().catch(()=>{});
  await rm(data,{recursive:true,force:true});
 }
});
