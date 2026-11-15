/* Operates only in the explicitly paired chat. Answers return as PC files. */
(function(root) {
  function create() {
  'use strict';
  const documentSession = crypto.randomUUID();
  let paired = null, active = null, lastHuman = 0;
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = node => !!(node && node.getClientRects().length);
  const unique = nodes => {const all=[...nodes].filter(visible);return all.length===1?all[0]:null;};
  function layout() {
    const log=unique(document.querySelectorAll('[role="log"][aria-label="Chat messages"]'));
    const outside=selector=>[...document.querySelectorAll(selector)].filter(node=>!log?.contains(node));
    const composer=unique(outside('textarea[placeholder="Message"],textarea[aria-label="Message"],input[aria-label="Message"],[contenteditable="true"][role="textbox"][aria-label="Message"]'));
    const send=unique(outside('button[aria-label="Send"],button[title="Send"]'));
    const stop=unique(outside('button[aria-label="Stop"],button[aria-label="Stop generating"],button[title="Stop"]'));
    return {log,composer,send,busy:!!stop};
  }
  const text=node=>node?.isContentEditable?node.innerText:node?.value||'';
  function probe() {
    if(root.__deskhandChatUrl && location.href !== root.__deskhandChatUrl)
      return {documentSession,code:'wrong_chat'};
    const view=layout();
    const signals=[...document.querySelectorAll('[role="alert"],[role="status"],[role="dialog"],h1,h2,h3')]
      .filter(n=>visible(n)&&!view.log?.contains(n)&&!n.contains(view.log)&&!n.contains(view.composer))
      .map(n=>(n.innerText||'').trim());
    if(view.composer) {
      const parent=view.composer.closest('form')||view.composer.parentElement;
      if(parent&&!parent.contains(view.log))signals.push([...parent.querySelectorAll('p,span,div')]
        .filter(n=>visible(n)&&!n.contains(view.composer)&&!view.composer.contains(n)&&!n.querySelector('textarea,input,[contenteditable="true"]'))
        .map(n=>(n.innerText||'').trim()).filter(s=>s.length<=1000).join(' ').slice(0,2000));
    }
    if(JettRelay.availability(signals)==='quota')return {documentSession,code:'quota'};
    if(view.composer&&(view.composer.disabled||view.composer.readOnly))return {documentSession,code:'unavailable'};
    const sameChat=!!(paired && paired.url===location.href && paired.log===view.log
      && (paired.replyKey ? view.log.innerText.includes(paired.replyKey)
        : paired.anchor.isConnected && (!paired.tail || view.log.innerText.includes(paired.tail))));
    return {documentSession,code:!paired?'needs_pair':JettRelay.canSend({sameChat,
      layout:!!(view.log&&view.composer),draft:!!text(view.composer).trim(),
      humanActive:Date.now()-lastHuman<3000,busy:view.busy})};
  }
  for (const event of ['beforeinput','keydown','pointerdown']) document.addEventListener(event,e=>{
    if(e.isTrusted)lastHuman=Date.now();
  },true);
  function fill(node,value) {
    if (node.isContentEditable) {
      node.focus();
      const range=document.createRange();range.selectNodeContents(node);
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
      if(!document.execCommand('insertText',false,value))throw Error('Composer refused text');
    } else {
      const proto=node instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto,'value').set.call(node,value);
      node.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:value}));
    }
  }
  async function send(job) {
    if(active||probe().code!=='ready')return {state:'blocked'};
    const value=JettRelay.wireText(job), original=layout();
    active={id:job.id,attempt:job.attempt,state:'sending'};
    // After touching the composer, every unknown outcome stays uncertain; never resend.
    try {
      fill(original.composer,value);
      // Rich-text browsers normalize line breaks while inserting. Compare with
      // that synchronous result so subsequent user/framework edits still block.
      const filled=text(original.composer);
      const lines=s=>s.replace(/\r\n?/g,'\n').replace(/\n+/g,'\n');
      if(lines(filled)!==lines(value))throw Error('Composer refused complete text');
      await pause(100);
      const view=layout();
      if(!['ready','draft'].includes(probe().code)||view.composer!==original.composer||text(view.composer)!==filled
          ||!view.send||view.send.disabled||view.busy||Date.now()-lastHuman<3000)throw Error('Composer changed');
      view.send.click();
      for(let i=0;i<25;i++) {
        await pause(100);
        const now=layout();
        const ownMessage=paired.url===location.href && paired.log===now.log && now.log.innerText.includes(job.reply_id);
        if(probe().code==='wrong_chat'&&!ownMessage)break;
        if(now.composer && !text(now.composer).trim() && ownMessage) {
          // Muse can recycle old message elements. Keep the unique, confirmed
          // reply key as our chat anchor instead of an obsolete DOM child.
          paired.replyKey=job.reply_id;
          active.state='sent';return {state:'sent'};
        }
      }
    } catch { /* Preserve all drafts after an interrupted send. */ }
    active.state='uncertain';return {state:'uncertain'};
  }
  return async function handle(message) {
      if(message.op==='pair'||message.op==='auto_pair') {
        if(root.__deskhandChatUrl && location.href !== root.__deskhandChatUrl)
          return {ok:false,code:'wrong_chat'};
        const view=layout();
        if(message.op==='auto_pair') {
          if(!root.__deskhandChatUrl && !/^Chat\s*[—-]\s*Jett(?:\s|$)/i.test(document.title||''))return {ok:false,code:'wrong_chat'};
          if(!view.log?.lastElementChild||!view.composer)return {ok:false,code:'layout'};
          if(text(view.composer).trim()||Date.now()-lastHuman<3000)return {ok:false,code:'draft'};
          if(view.busy)return {ok:false,code:'busy'};
        }
        if(!view.log?.lastElementChild||!view.composer||view.busy||active)return {ok:false};
        paired={url:location.href,log:view.log,anchor:view.log.lastElementChild,tail:(view.log.innerText||'').slice(-200)};
        return {ok:true,documentSession};
      }
      if(message.op==='probe')return probe();
      if(message.op==='send')return send(message.job);
      if(message.op==='observe')return {state:active?.id===message.id&&active?.attempt===message.attempt?active.state:'missing'};
      if(message.op==='release') {if(active?.id===message.id)active=null;return {ok:true};}
      return {ok:false};
  };
  }
  root.JettPage={create};
})(globalThis);
