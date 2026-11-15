import {useEffect, useState} from 'react';
import {DeskhandFeatures} from './DeskhandFeatures';

type RemoteState = {
 network: {state:string;address?:string;needsLogin:boolean};
 paused:boolean;
 pairing?:{id:string;name:string;code:string};
 phones:{id:string;name:string;control:boolean;connected:boolean}[];
 approvals:{Id:string;Operation:string;Target:string;Purpose:string}[];
 orientation:boolean;
 relayError?:string;
};

export function PhoneConnection() {
 const [state,setState]=useState<RemoteState|null>(null);
 const [qr,setQr]=useState<{png:string;expires:string}|null>(null);
 const [error,setError]=useState('');
 const [busy,setBusy]=useState(false);
 const [more,setMore]=useState(false);
 async function refresh(){setState(await window.coder.deskhandControl({operation:'status'}) as RemoteState)}
 async function act(command:Record<string,unknown>){
  setBusy(true);setError('');
  try{await window.coder.deskhandControl(command);await refresh()}
  catch(e){setError(e instanceof Error?e.message:String(e))}
  finally{setBusy(false)}
 }
 async function pair(configured=false){
  setBusy(true);setError('');
  try{setQr(await (configured?window.coder.deskhandControl({operation:'pair-configured'}):window.coder.pairPhone()) as {png:string;expires:string})}
  catch(e){setError(e instanceof Error?e.message:String(e))}
  finally{setBusy(false)}
 }
 useEffect(()=>{
  let active=true,inflight=false;
  async function poll(){if(inflight)return;inflight=true;try{const next=await window.coder.deskhandControl({operation:'status'}) as RemoteState;if(active){setState(next);setError('')}}catch(e){if(active)setError(e instanceof Error?e.message:String(e))}finally{inflight=false}}
  void poll();const timer=setInterval(poll,2500);return()=>{active=false;clearInterval(timer)};
 },[]);
 useEffect(()=>{if(state?.network.state==='Running'&&!state.paused&&!qr&&!busy)void pair()},[state?.network.state]);
 return <section aria-label="Phone connection"><h3>Phone connection</h3>
  {error&&<p role="alert">{error}</p>}
  <section className="settings-section"><h3>Tailscale</h3><p>{state?.network.state||'Connecting…'} {state?.network.address}</p>
   {state?.network.needsLogin&&<button disabled={busy} onClick={async()=>{setBusy(true);try{await window.coder.deskhandLogin();await refresh()}catch(e){setError(String(e))}finally{setBusy(false)}}}>Sign in to Tailscale</button>}
  </section>
  {state?.pairing?<section className="settings-section"><h3>Approve phone</h3><p>{state.pairing.name}</p><strong style={{fontSize:28}}>{state.pairing.code}</strong><p>Confirm this code matches your phone.</p>{[['view','View only'],['control','Allow control'],['reject','Reject']].map(([decision,label])=><button key={decision} disabled={busy} onClick={()=>void act({operation:'pair-decision',id:state.pairing!.id,decision})}>{label}</button>)}</section>:
  <section className="settings-section"><h3>Pair your phone</h3>{qr&&new Date(qr.expires).getTime()>Date.now()?<><p>Scan in the Deskhand phone app.</p><img alt="Phone pairing QR code" src={'data:image/png;base64,'+qr.png} style={{width:280,maxWidth:'100%'}}/><p>Expires {new Date(qr.expires).toLocaleTimeString()}</p></>:null}<button disabled={busy||state?.network.state!=='Running'||state.paused} onClick={()=>void pair()}>New QR code</button><button disabled={busy||!state||state.paused} onClick={()=>void pair(true)}>Pair using configured relay</button><button disabled={busy} onClick={async()=>{await act({operation:'cancel-pair'});setQr(null)}}>Cancel pairing</button></section>}
  <section className="settings-section"><h3>Remote access</h3><button disabled={busy||!state} onClick={()=>void act({operation:'pause',enabled:!state?.paused})}>{state?.paused?'Resume remote access':'Pause remote access'}</button><button disabled={busy} onClick={()=>void act({operation:'disconnect'})}>Disconnect phones</button><label><input type="checkbox" checked={state?.orientation||false} disabled={busy||!state} onChange={e=>void act({operation:'orientation',enabled:e.target.checked})}/> Follow phone orientation</label>{state?.relayError&&<p>{state.relayError}</p>}</section>
  <section className="settings-section"><h3>Paired phones</h3>{state?.phones.map(phone=><div key={phone.id}><p>{phone.name} · {phone.connected?'Connected':'Offline'} · {phone.control?'Control allowed':'View only'}</p><button disabled={busy} onClick={()=>void act({operation:'phone-control',id:phone.id,enabled:!phone.control})}>{phone.control?'Block control':'Allow control'}</button><button disabled={busy} onClick={()=>{if(confirm('Remove '+phone.name+'?'))void act({operation:'remove-phone',id:phone.id})}}>Remove phone</button></div>)}</section>
  {state?.approvals.map(approval=><section key={approval.Id} className="settings-section"><h3>{approval.Operation}</h3><p>{approval.Purpose}</p><pre>{approval.Target}</pre><button disabled={busy} onClick={()=>void act({operation:'approval',id:approval.Id,approved:true})}>Approve</button><button disabled={busy} onClick={()=>void act({operation:'approval',id:approval.Id,approved:false})}>Reject</button></section>)}
  <section className="settings-section"><button aria-expanded={more} onClick={()=>setMore(!more)}>{more?'Hide Deskhand settings':'More Deskhand settings'}</button><p>Assistant, models, runtime, diagnostics, Muse access and connection options.</p></section>
  {more&&<DeskhandFeatures/>}
 </section>;
}
