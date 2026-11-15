import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import net from 'node:net';
import {spawn, type ChildProcess} from 'node:child_process';

type State={name:string;status:string;detail:string};
export class TaskAgent {
 private child?:ChildProcess;
 private state:State={name:'Task Agent',status:'Stopped',detail:'The bridge is not running.'};
 private starting=false;
 private stopped=false;
 private shared=false;
 constructor(private directory:string,private changed:()=>void=()=>{},private sharedConfig?:string){}
 private set(status:string,detail:string){this.state={name:'Task Agent',status,detail};this.changed()}
 status(){return {...this.state}}
 async start(){
  if(this.child||this.starting)return;
  this.starting=true;this.stopped=false;this.set('Starting','Preparing the local bridge.');
  try{
   if(this.sharedConfig){
    let configText:string|undefined;try{configText=await fs.readFile(this.sharedConfig,'utf8')}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error}
    if(configText!==undefined){
     this.shared=true;const cfg=JSON.parse(configText) as {chat_token?:unknown;port?:unknown};const token=cfg.chat_token,port=cfg.port;
     if(typeof token!=='string'||token.length<16||!Number.isInteger(port)||Number(port)<1||Number(port)>65535)throw new Error('Shared Deskhand configuration lacks a usable scoped chat credential or port.');
     const base=`http://127.0.0.1:${port}`;const health=await fetch(base+'/health',{signal:AbortSignal.timeout(3000)});if(!health.ok)throw new Error('Shared Deskhand agent is unavailable.');const info=await health.json() as {ok?:unknown;server?:unknown;remote_control_enabled?:unknown};if(info.ok!==true||info.server!=='laptop-agent-v2')throw new Error('Shared Deskhand agent identity was not verified.');
     const probe=await fetch(base+'/chat/replies?turn_id='+crypto.randomUUID(),{headers:{Authorization:`Bearer ${token}`},signal:AbortSignal.timeout(3000)});if(!probe.ok)throw new Error('Shared Deskhand chat credential was not accepted.');const reply=await probe.json() as {replies?:unknown};if(!Array.isArray(reply.replies))throw new Error('Shared Deskhand chat interface was not verified.');
     if(!this.stopped)this.set('Shared',`Deskhand agent reachable and scoped chat access verified; remote control is ${info.remote_control_enabled===true?'enabled':'off'}. Muse account and coordinator connection are not verified.`);return;
    }
   }
   this.shared=false;
   const script=path.join(this.directory,'laptop_agent_v2.py'),config=path.join(this.directory,'agent_config.json');
   await fs.access(script);
   const initial={allowed_roots:[],bridge_path:'/v1/bridge',bridge_port:8765,host:'127.0.0.1',log_file:'agent.log',port:8899,remote_control_enabled:false,timeout_secs:120,token:crypto.randomBytes(32).toString('base64url')};
   try{await fs.writeFile(config,JSON.stringify(initial,null,2)+'\n',{flag:'wx',mode:0o600})}catch(error){if((error as NodeJS.ErrnoException).code!=='EEXIST')throw error}
   const parsed=JSON.parse(await fs.readFile(config,'utf8')) as Record<string,unknown>;
   const host=parsed.host,token=parsed.token,port=parsed.port,bridgePort=parsed.bridge_port;
   const permitted=typeof host==='string'&&net.isIP(host)===4&&(host==='127.0.0.1'||(()=>{const parts=host.split('.').map(Number);return parts[0]===100&&parts[1]>=64&&parts[1]<=127})());
   if(!permitted||typeof token!=='string'||token.length<32||!Number.isInteger(port)||!Number.isInteger(bridgePort)||Number(port)<1||Number(port)>65535||Number(bridgePort)<1||Number(bridgePort)>65535||port===bridgePort||typeof parsed.remote_control_enabled!=='boolean')throw new Error('Invalid configuration. Use the Task Agent launcher to configure a strong token, localhost or Tailscale address, and distinct ports.');
   if(this.stopped)return;
   const child=spawn('python',['-u',script,'--config',config],{cwd:this.directory,windowsHide:true,stdio:['ignore','pipe','pipe']});this.child=child;
   let http=false,bridge=false,buffer='';const startup=setTimeout(()=>{if(this.child===child&&(!http||!bridge)){this.child=undefined;child.kill();this.set('Unavailable','Task Agent did not start both services. Check dependencies, configuration, and port availability.')}},10000);const update=(text:string)=>{buffer=(buffer+text).slice(-10000);http ||= buffer.includes('HTTP listening on ');bridge ||= buffer.includes('BRIDGE listening on ');if(this.child===child&&http&&bridge){clearTimeout(startup);this.set('Running',parsed.remote_control_enabled?'Bridge running with remote control enabled. Muse connection is not verified.':'Bridge running; remote control is off. Muse connection is not verified.')}};
   child.stdout?.on('data',chunk=>update(String(chunk)));child.stderr?.on('data',chunk=>update(String(chunk)));
   child.once('error',()=>{clearTimeout(startup);if(this.child===child){this.child=undefined;this.set('Unavailable','Could not start Python. Install Python and the websockets package, then retry.')}});
   child.once('exit',(code)=>{clearTimeout(startup);if(this.child===child){this.child=undefined;this.set('Unavailable',`Task Agent stopped (exit ${code??'unknown'}). Check its configuration, dependencies, and port availability.`)}});
  }catch(error){this.set('Unavailable',this.shared?'Shared Deskhand agent or scoped chat access is unavailable. Start it through Deskhand and retry.':error instanceof Error&&error.message.startsWith('Invalid configuration')?error.message:'Task Agent files or configuration could not be opened.');}
  finally{this.starting=false}
 }
 stop(){this.stopped=true;const child=this.child;this.child=undefined;if(child)child.kill();if(this.shared)this.set('Shared','Deskhand manages this shared agent; Jetstream will not stop it.');else this.set('Stopped','The bridge is stopped.');}
}
