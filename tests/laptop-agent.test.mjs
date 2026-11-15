import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdtemp,copyFile,readFile,writeFile,rm} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import net from 'node:net';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
const {TaskAgent}=createRequire(import.meta.url)('../dist-electron/laptop-agent.cjs');
const port=()=>new Promise((resolve,reject)=>{const server=net.createServer();server.once('error',reject);server.listen(0,'127.0.0.1',()=>{const value=server.address().port;server.close(()=>resolve(value))})});
const wait=async(fn)=>{const until=Date.now()+15000;while(Date.now()<until){if(await fn())return;await new Promise(resolve=>setTimeout(resolve,50))}throw new Error('Timed out waiting for Task Agent')};

test('agent starts on localhost with remote control off and stops with its owner', {timeout:30000},async t=>{
 try{execFileSync('python',['-c','import websockets.asyncio.server'],{stdio:'ignore'})}catch{t.skip('Python websockets is unavailable');return}
 const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-laptop-agent-'));let agent;
 try{
  await copyFile('TaskAgent/laptop_agent_v2.py',path.join(dir,'laptop_agent_v2.py'));
  const httpPort=await port();let wsPort=await port();while(wsPort===httpPort)wsPort=await port();
  const config={host:'127.0.0.1',port:httpPort,bridge_port:wsPort,bridge_path:'/v1/bridge',timeout_secs:120,allowed_roots:[],remote_control_enabled:false,token:'test-token-that-is-at-least-32-characters'};
  await writeFile(path.join(dir,'agent_config.json'),JSON.stringify(config));
  agent=new TaskAgent(dir);await agent.start();await wait(()=>agent.status().status!=='Starting');assert.equal(agent.status().name,'Task Agent');
  assert.equal(agent.status().status,'Running');assert.match(agent.status().detail,/remote control is off/i);
  const response=await fetch(`http://127.0.0.1:${httpPort}/health`);assert.equal(response.ok,true);assert.equal((await response.json()).remote_control_enabled,false);
  const denied=await fetch(`http://127.0.0.1:${httpPort}/exec`,{method:'POST',headers:{Authorization:`Bearer ${config.token}`,'Content-Type':'application/json'},body:JSON.stringify({command:'whoami'})});assert.equal(denied.status,403);
  const ws=new WebSocket(`ws://127.0.0.1:${wsPort}/v1/bridge`);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});const auth=new Promise((resolve,reject)=>{ws.onmessage=e=>resolve(JSON.parse(e.data));ws.onerror=reject});ws.send(JSON.stringify({type:'auth',token:config.token}));assert.equal((await auth).type,'auth_ok');ws.close();
  agent.stop();await wait(async()=>{try{await fetch(`http://127.0.0.1:${httpPort}/health`,{signal:AbortSignal.timeout(200)});return false}catch{return true}});
  assert.equal(agent.status().status,'Stopped');assert.equal(JSON.parse(await readFile(path.join(dir,'agent_config.json'),'utf8')).remote_control_enabled,false);
 }finally{agent?.stop();await rm(dir,{recursive:true,force:true})}
});

test('invalid or unsafe address is not started and existing settings are preserved',async()=>{
 const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-laptop-agent-invalid-'));
 try{await writeFile(path.join(dir,'laptop_agent_v2.py'),'');const config={host:'0.0.0.0',port:8899,bridge_port:8765,remote_control_enabled:true,token:'x'.repeat(40)};const file=path.join(dir,'agent_config.json');await writeFile(file,JSON.stringify(config));const agent=new TaskAgent(dir);await agent.start();assert.equal(agent.status().status,'Unavailable');assert.deepEqual(JSON.parse(await readFile(file,'utf8')),config)}finally{await rm(dir,{recursive:true,force:true})}
});

test('shared Deskhand agent uses scoped chat authentication and is never stopped by Jetstream',async()=>{
 const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-shared-agent-'));const token='scoped-fixture-token-123456';let probeCount=0;
 const server=http.createServer((req,res)=>{res.setHeader('Content-Type','application/json');if(req.url==='/health')res.end(JSON.stringify({ok:true,server:'laptop-agent-v2',remote_control_enabled:true}));else if(req.url?.startsWith('/chat/replies?turn_id=')&&req.headers.authorization===`Bearer ${token}`){probeCount++;res.end(JSON.stringify({replies:[]}))}else{res.statusCode=401;res.end('{}')}});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 try{
  const file=path.join(dir,'deskhand-config.json');await writeFile(file,JSON.stringify({port:server.address().port,chat_token:token,token:'unused-full-token'}));
  const agent=new TaskAgent(path.join(dir,'no-standalone-agent'),()=>{},file);await agent.start();assert.equal(agent.status().name,'Task Agent');assert.equal(agent.status().status,'Shared');assert.match(agent.status().detail,/scoped chat access verified/);assert.equal(probeCount,1);
  agent.stop();assert.equal(agent.status().status,'Shared');assert.equal((await fetch(`http://127.0.0.1:${server.address().port}/health`)).ok,true);
  await writeFile(file,JSON.stringify({port:server.address().port,chat_token:'wrong-fixture-token'}));await agent.start();assert.equal(agent.status().status,'Unavailable');assert.doesNotMatch(agent.status().detail,/wrong-fixture-token/);
 }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await rm(dir,{recursive:true,force:true})}
});
