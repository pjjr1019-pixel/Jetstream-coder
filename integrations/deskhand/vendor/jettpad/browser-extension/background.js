'use strict';
let native = null, pumping = false, retry = 5000, nativeError = '', nativeReady = false, nextOpen = 0;
const calls = new Map();
const HOST = 'com.jettpad.browser';
const allowed = url => { try { const u=new URL(url); return u.origin === 'https://muse.ai'; } catch { return false; } };
function connect() {
  if (native) return;
  const opened = Date.now();
  nativeReady=false;
  try { native=chrome.runtime.connectNative(HOST); } catch { nativeError='Edge could not open the local helper';schedule(); return; }
  native.onMessage.addListener(message => {
    const entry=calls.get(message.rpc); if (!entry) return;
    nativeReady=true;nativeError='';
    calls.delete(message.rpc); clearTimeout(entry.timer);
    message.ok ? entry.resolve(message.result) : entry.reject(Error('Local relay rejected request'));
  });
  native.onDisconnect.addListener(() => {
    nativeError=(chrome.runtime.lastError?.message||'Local helper disconnected').slice(0,240);
    native=null;nativeReady=false;
    for (const entry of calls.values()) {clearTimeout(entry.timer);entry.reject(Error('Local relay disconnected'));}
    calls.clear(); if (Date.now()-opened>30000) retry=5000; schedule();
  });
}
function schedule() { setTimeout(connect,retry); retry=Math.min(30000,retry*2); }
function rpc(op, data={}) {
  if (!native) return Promise.reject(Error('Local helper is not connected'));
  const id=crypto.randomUUID();
  return new Promise((resolve,reject) => {
    const timer=setTimeout(()=>{calls.delete(id);reject(Error('Local relay timed out'));},5000);
    calls.set(id,{resolve,reject,timer}); native.postMessage({rpc:id,op,...data});
  });
}
async function page(target,message) {
  const tab=await chrome.tabs.get(target.tabId);
  if (!allowed(tab.url) || tab.url !== target.url) throw Error('Paired chat changed');
  return chrome.tabs.sendMessage(target.tabId,message,{frameId:0});
}
async function ensureMuse(target,probe) {
  if (Date.now()<nextOpen) return;
  nextOpen=Date.now()+2000;
  const url=target?.url || 'https://muse.ai/';
  if (!allowed(url)) return;
  // A changed conversation within the same page is never silently adopted.
  if(target?.documentSession && probe?.documentSession===target.documentSession
      && probe.code==='wrong_chat') return;
  let tab;
  try {if(target) {const found=await chrome.tabs.get(target.tabId);if(found.url===url)tab=found;}} catch {}
  if(!tab) {
    const matches=(await chrome.tabs.query({url:'https://muse.ai/*'})).filter(t=>t.url===url);
    if(matches.length>1)return; // Do not guess between two Muse chats.
    tab=matches[0];
  }
  if(!tab) {
    // Muse defers initialization in an inactive tab. Keep its tab active in
    // a separate unfocused window so the Pad retains the keyboard focus.
    const created=await chrome.windows.create({url,focused:false,type:'normal'});
    await chrome.windows.update(created.id,{focused:false});
    tab=created.tabs?.[0];
    if(tab)await chrome.storage.local.set({target:{tabId:tab.id,url,documentSession:null,autoWindow:created.id}});
    return; // The page must load and recognize Jett before it can be paired.
  }
  try {
    const result=await chrome.tabs.sendMessage(tab.id,{op:'auto_pair'},{frameId:0});
    if(result.ok) {
      await chrome.storage.local.set({target:{tabId:tab.id,url,documentSession:result.documentSession}});
    }
  } catch {} // Loading, signed out, a draft or a busy chat remains untouched.
}
async function pump() {
  if (!native || pumping) return;
  pumping=true;
  try {
    const {target}=await chrome.storage.local.get('target');
    let probe;
    try { probe=target ? await page(target,{op:'probe'}) : {code:'needs_pair'}; }
    catch { probe={code:'wrong_chat'}; }
    const valid=!!(target?.documentSession && probe.documentSession === target.documentSession);
    const code=valid ? probe.code : 'needs_pair';
    const status=await rpc('status',{code});
    const job=status.active;
    // Muse can leave its busy UI stale while its window is minimized. Keep
    // the paired page running in a normal, unfocused window for queued work.
    // Never touch a changed chat or a human draft, or open anything when idle.
    if (!job && status.queued_count>0 && valid && ['ready','busy'].includes(code)) {
      const tab=await chrome.tabs.get(target.tabId);
      const window=await chrome.windows.get(tab.windowId);
      if(window.state==='minimized') {
        await chrome.windows.update(window.id,{state:'normal',focused:false});
        return; // Reobserve page state after it resumes; do not bypass busy.
      }
    }
    if (job) {
      if (job.state === 'uncertain') {await rpc('status',{code:'uncertain'});return;}
      if (job.state === 'sent') {
        if (Date.now()/1000-job.updated > 600) {
          await rpc('event',{id:job.id,attempt:job.attempt,event:'uncertain'});
          await rpc('status',{code:'uncertain'});
        }
        return; // Reply arrives through a new PC file; never scrape browser answers.
      }
      const snapshot=valid ? await page(target,{op:'observe',id:job.id,attempt:job.attempt}) : {state:'missing'};
      if (snapshot.state === 'missing' || snapshot.state === 'uncertain') {
        await rpc('event',{id:job.id,attempt:job.attempt,event:'uncertain'});
        await rpc('status',{code:'uncertain'}); return;
      }
      if (snapshot.state === 'sent') await rpc('event',{id:job.id,attempt:job.attempt,event:'sent'});
      return;
    }
    if (!valid) {if(status.queued_count>0)await ensureMuse(target,probe);return;}
    if (code !== 'ready') return;
    const {lastJob}=await chrome.storage.local.get('lastJob');
    if (lastJob) {
      await page(target,{op:'release',id:lastJob});
      await chrome.storage.local.remove('lastJob');
    }
    if (!status.queued_count) return; // Idle chats need no SQLite claim transaction.
    const {job:next}=await rpc('claim',{destination:target.documentSession});
    if (!next) return;
    await chrome.storage.local.set({lastJob:next.id});
    // The durable local claim precedes the DOM action. An unknown outcome is never retried.
    let outcome;
    try { outcome=await page(target,{op:'send',job:next}); }
    catch { outcome={state:'uncertain'}; }
    await rpc('event',{id:next.id,attempt:next.attempt,
      event:outcome.state === 'blocked' ? 'blocked' : outcome.state === 'sent' ? 'sent' : 'uncertain'});
  } catch {
    // No message bodies, page content or credentials are logged.
  } finally {pumping=false;}
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if (sender.id !== chrome.runtime.id) return false;
  if(sender.tab) {
    if(message.op==='page_ready' && sender.frameId===0 && allowed(sender.url||sender.tab.url)) {
      connect();reply({ok:true});
    }
    return false; // Page scripts can wake the helper, never use popup controls.
  }
  (async()=>{
    if (message.op === 'status') {
      const {target}=await chrome.storage.local.get('target');
      let paired=false;
      if(target) {
        try {const probe=await page(target,{op:'probe'}); paired=probe.documentSession===target.documentSession
          && !['needs_pair','wrong_chat'].includes(probe.code);} catch {}
      }
      connect();return {connected:nativeReady,paired,error:nativeError};
    }
    if (message.op === 'pair') {
      const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
      if (!tab || !allowed(tab.url)) throw Error('Open your Muse chat first');
      const result=await chrome.tabs.sendMessage(tab.id,{op:'pair'},{frameId:0});
      if (!result.ok) throw Error('The Muse chat layout is not recognized or it is busy');
      await chrome.storage.local.set({target:{tabId:tab.id,url:tab.url,documentSession:result.documentSession}});
      connect(); return {ok:true};
    }
    if (message.op === 'ack_uncertain') {
      const {target}=await chrome.storage.local.get('target');
      const status=await rpc('status',{code:'uncertain'});
      if (target && status.active) {
        try {await page(target,{op:'release',id:status.active.id,reviewed:true});} catch {}
      }
      await rpc('ack_uncertain');return {ok:true};
    }
    throw Error('Unknown operation');
  })().then(result=>reply({ok:true,...result})).catch(error=>reply({ok:false,error:error.message}));
  return true;
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();setInterval(pump,100);
