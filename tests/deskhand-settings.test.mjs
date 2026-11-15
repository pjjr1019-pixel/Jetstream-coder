import {test} from 'node:test';
import assert from 'node:assert/strict';
import {_electron as electron} from 'playwright-core';
import {mkdtemp,rm} from 'node:fs/promises';import os from 'node:os';import path from 'node:path';

test('embedded Deskhand settings preserve connection, runtime, routing, history and explicit access controls',{timeout:60000},async()=>{
 const data=await mkdtemp(path.join(os.tmpdir(),'jetstream-deskhand-settings-'));let app;
 try{
  app=await electron.launch({args:['.',`--user-data-dir=${data}`],env:{...process.env,JETSTREAM_DISABLE_LAPTOP_AGENT:'1'}});
  await app.evaluate(({ipcMain})=>{
   globalThis.deskhandCommands=[];
   let failSubmit=true;
   const settings={deviceName:'Fixture PC',relayUrl:'ws://100.64.0.1:47801',relayPort:47801,builtInRelay:true,startWithWindows:false};
   const runtime={connection:'Ready',relay:'Relay connected',worker:'Worker unavailable',activity:'Idle',age:'updated 1s ago',files:['sample.txt'],eventsHealth:'Feed connected',events:['Task finished'],boardHealth:'1 post',board:[{id:'post',author:'Muse',text:'Completed fixture',detail:'Full post detail',time:'2026-11-14'}],authority:'Off',diagnostics:[{key:'service',name:'Service',status:'OK',lastOk:0,version:'fixture',error:'',level:'INFO'}],turns:['turn-1']};
   ipcMain.removeHandler('deskhand:control');ipcMain.removeHandler('deskhand:pair-phone');
   ipcMain.handle('deskhand:pair-phone',()=>({png:'fixture',expires:new Date(Date.now()+60000).toISOString()}));
   ipcMain.handle('deskhand:control',(_event,request)=>{
    globalThis.deskhandCommands.push(request);
    if(request.operation==='status')return {network:{state:'Running',needsLogin:false},paused:false,phones:[],approvals:[],orientation:true};
    if(request.operation==='features')return {settings,runtime};
    if(request.operation==='settings'){Object.assign(settings,request);return true}
    if(request.operation==='startup')return request.enabled;
    const command=request.command;
    if(command?.op==='catalog')return {routes:[{name:'Fixture Muse',assistant:'muse',model:'fixture',account:'fixture',effort:'low',efforts:['low','medium']}]};
    if(command?.op==='router_settings')return {always_smart:false};
    if(command?.op==='history')return {turns:[{id:'turn-1',text:'Original prompt',assistant:'muse',model:'fixture',status:'completed'}],jettReplies:[{prompt_id:'turn-1',reply_id:'reply-1',text:'Original connector reply',status:'completed'}],assistantActions:[],jettActions:[]};
    if(command?.op==='route_suggest')return {route:{assistant:'muse',model:'fixture',account:'fixture',effort:'medium'},reason:'Fixture route'};
    if(command?.op==='access_status'||command?.op==='access_set')return {state:command.enabled?'On':'Off',supported:true};
    if(command?.op==='submit'&&failSubmit){failSubmit=false;throw new Error('Simulated lost acknowledgement')}
    if(command?.op==='muse_status')return {cloud_ready:false,delivery_ready:false,bridge:{bind:'127.0.0.1',port:47800,features:{desktop_control:false,screenshots:false,exec:false},desktop_apps:[],scopes:{}}};
    if(command?.op==='timing')return {timing:{total_secs:1}};
    return {status:'requested'};
   });
  });
  const win=await app.firstWindow();win.setDefaultTimeout(15000);
  await win.getByRole('button',{name:'Pair a phone',exact:true}).click();
  await win.getByRole('button',{name:'More Deskhand settings',exact:true}).click();
  await win.getByLabel('Deskhand PC name',{exact:true}).fill('Renamed PC');
  await win.getByRole('button',{name:'Save connection settings',exact:true}).click();
  await win.getByText('Connection settings saved.',{exact:false}).waitFor();
  await win.getByRole('button',{name:'Runtime',exact:true}).click();
  await win.getByText('Task finished',{exact:true}).waitFor();
  await win.getByText('Muse · Completed fixture',{exact:true}).click();
  await win.getByText('Full post detail',{exact:true}).waitFor();
  await win.getByLabel('Deskhand turn timing',{exact:true}).selectOption('turn-1');
  await win.getByRole('button',{name:'Show turn timing',exact:true}).click();
  await win.getByText('"total_secs": 1',{exact:false}).waitFor();
  await win.getByRole('button',{name:'Assistant',exact:true}).click();
  await win.getByText('Original connector reply',{exact:true}).waitFor();
  await win.getByLabel('Deskhand message',{exact:true}).fill('Test message');
  await win.getByRole('button',{name:'Send to Deskhand assistant',exact:true}).click();
  await win.getByRole('alert').filter({hasText:'Simulated lost acknowledgement'}).waitFor();
  assert.equal(await win.getByRole('button',{name:'Send to Deskhand assistant',exact:true}).isDisabled(),true);
  await win.getByRole('button',{name:'Refresh chat history',exact:true}).click();
  let commands=await app.evaluate(()=>globalThis.deskhandCommands);
  assert.equal(commands.filter(c=>c.command?.op==='submit').length,1);
  assert.equal(commands.filter(c=>c.command?.op==='access_set').length,0,'catalog/history must not grant computer access');
  assert.equal(commands.find(c=>c.command?.op==='submit').command.agent,false);
  await win.getByRole('button',{name:'Enable assistant computer access',exact:true}).click();
  await win.getByRole('button',{name:'Muse access',exact:true}).click();
  await win.getByLabel('Muse bridge address',{exact:true}).waitFor();
  await win.getByLabel('Allow desktop control',{exact:true}).check();
  await win.getByLabel('ENABLE DESKTOP CONTROL',{exact:true}).fill('ENABLE DESKTOP CONTROL');
  await win.getByRole('button',{name:'Save Muse permissions',exact:true}).click();
  await win.getByText('Muse permissions saved.',{exact:true}).waitFor();
  commands=await app.evaluate(()=>globalThis.deskhandCommands);
  assert.equal(commands.find(c=>c.operation==='settings').deviceName,'Renamed PC');
  const permissionChange=commands.find(c=>c.command?.op==='muse_action'&&c.command.payload?.features);
  assert.equal(permissionChange.command.payload.features.desktop_control,true);
  assert.equal(permissionChange.command.payload.confirmations.desktop_control,'ENABLE DESKTOP CONTROL');
 }finally{if(app)await app.close().catch(()=>{});await rm(data,{recursive:true,force:true})}
});
