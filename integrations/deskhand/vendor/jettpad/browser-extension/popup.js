'use strict';
const status=document.querySelector('#status');
const notice=document.querySelector('#notice');
async function action(op) {
  const output=op==='status'?status:notice;
  try {const r=await chrome.runtime.sendMessage({op});output.textContent=r.ok ?
    (op==='status' ? (r.connected ? (r.paired ? 'Local helper connected; chat paired.' : 'Local helper connected; select your Muse chat.') : 'Local helper is not connected. '+(r.error||'Connecting…')) : 'Chat connected. Existing drafts and history are unchanged.') : r.error;
  } catch {output.textContent='The extension is not ready. Reload it and open Muse again.';}
}
document.querySelector('#pair').onclick=()=>action('pair');
document.querySelector('#review').onclick=()=>action('ack_uncertain');
action('status');
setInterval(()=>action('status'),1000);
