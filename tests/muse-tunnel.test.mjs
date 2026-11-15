import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';import path from 'node:path';
const {MuseTunnel}=createRequire(import.meta.url)('../dist-electron/muse-tunnel.cjs');

test('Muse tunnel submits the Deskhand route and polls only its own reply',async()=>{
 const dir=await mkdtemp(path.join(os.tmpdir(),'jetstream-muse-'));const calls=[];const token='scoped-test-token-123456';
 const server=http.createServer(async(req,res)=>{let body='';for await(const chunk of req)body+=chunk;calls.push({url:req.url,auth:req.headers.authorization,body:body?JSON.parse(body):null});res.setHeader('Content-Type','application/json');if(req.url==='/chat/submit')res.end(JSON.stringify({turn_id:'own-id',status:'queued',is_new:true}));else if(req.url==='/chat/replies?turn_id=own-id')res.end(JSON.stringify({replies:[{turn_id:'own-id',reply:'Hello Jetstream',error:null}]}));else if(req.url==='/chat/ack')res.end(JSON.stringify({turn_id:'own-id',delivered:true}));else{res.statusCode=404;res.end('{}')}});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 try{const file=path.join(dir,'config.json');await writeFile(file,JSON.stringify({port:server.address().port,chat_token:token}));const tunnel=new MuseTunnel(file);await tunnel.submit('own-id','hello');assert.deepEqual(calls[0].body,{turn_id:'own-id',text:'hello',assistant:'muse',model:'muse',account:'deskhand',effort:''});assert.equal(calls[0].auth,`Bearer ${token}`);assert.deepEqual(await tunnel.reply('own-id'),{reply:'Hello Jetstream',error:null});await tunnel.ack('own-id');assert.equal(calls[1].url,'/chat/replies?turn_id=own-id');assert.equal(calls[2].body.turn_id,'own-id')}
 finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await rm(dir,{recursive:true,force:true})}
});
