import fs from 'node:fs/promises';

/** Deskhand's scoped chat mailbox; never handles the full-control token. */
export class MuseTunnel {
 constructor(private configPath:string){}
 private async request(endpoint:string,body?:unknown){
  const config=JSON.parse(await fs.readFile(this.configPath,'utf8')) as {port?:unknown;chat_token?:unknown};
  if(!Number.isInteger(config.port)||Number(config.port)<1||Number(config.port)>65535||typeof config.chat_token!=='string'||config.chat_token.length<16)throw new Error('Deskhand scoped chat access is unavailable');
  let response:Response;try{response=await fetch(`http://127.0.0.1:${config.port}${endpoint}`,{method:body===undefined?'GET':'POST',headers:{Authorization:`Bearer ${config.chat_token}`,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(5000),redirect:'error'})}catch{throw new Error('Deskhand chat transport is unavailable; a submitted message may still be queued')}
  if(!response.ok)throw new Error(`Deskhand chat transport returned HTTP ${response.status}`);
  return await response.json() as Record<string,unknown>;
 }
 async submit(id:string,text:string){
  const result=await this.request('/chat/submit',{turn_id:id,text,assistant:'muse',model:'muse',account:'deskhand',effort:''});
  if(result.turn_id!==id||result.is_new!==true||result.status!=='queued')throw new Error('Deskhand did not confirm a new queued message');
 }
 async reply(id:string){
  const result=await this.request('/chat/replies?turn_id='+encodeURIComponent(id));
  if(!Array.isArray(result.replies)||result.replies.length>1)throw new Error('Deskhand returned an invalid reply');
  if(!result.replies.length)return null;
  const r=result.replies[0] as {turn_id?:unknown;reply?:unknown;error?:unknown};
  if(r.turn_id!==id||(typeof r.reply!=='string'&&typeof r.error!=='string'))throw new Error('Deskhand returned an invalid reply');
  return {reply:typeof r.reply==='string'?r.reply:null,error:typeof r.error==='string'?r.error:null};
 }
 async ack(id:string){const result=await this.request('/chat/ack',{turn_id:id});if(result.turn_id!==id||result.delivered!==true)throw new Error('Deskhand did not confirm the reply receipt')}
}
