/* Extension wrapper; the same guarded page adapter also runs in headless Edge. */
(() => {
  const handle=JettPage.create();
  const wake=()=>{try{chrome.runtime.sendMessage({op:'page_ready'}).catch(()=>{});}catch{}};
  wake();
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')wake();});
  chrome.runtime.onMessage.addListener((message,sender,reply)=>{
    if(sender.id!==chrome.runtime.id)return false;
    handle(message).then(reply).catch(()=>reply({state:'uncertain'}));
    return true;
  });
})();
