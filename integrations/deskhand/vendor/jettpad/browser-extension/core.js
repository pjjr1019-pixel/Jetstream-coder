/* Prompt formatting and conservative send guards; no browser reply scraping. */
(function(root) {
  'use strict';
  function wireText(job) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(job.reply_id)
        || typeof job.text !== 'string' || !job.text.trim() || job.text.length > 24000) throw Error('Invalid relay message');
    if (root.__deskhandChatUrl) {
      return `DeskHand message:\n${job.text}\n\n[DeskHand reply delivery]\nDeliver your answer through the existing authenticated DeskHand Prototype HTTPS connector using POST /deskhand-prototype/pad-replies with JSON {"reply_id":"${job.reply_id}","text":"your answer"} and the active X-Deskhand-Session header. If there is no active session, open a new DeskHand session first. Keep credentials and session values private. Do not use another bridge, write files, or launch Notepad for reply delivery. If delivery is unconfirmed, report it here and do not automatically resend.`;
    }
    const documents = root.__jettReplyDocuments || 'C:/Users/Phlip/Documents';
    const base = documents + '/jett-pad-inbox/' + job.reply_id;
    return `open notepad with the reply to this
${job.text}

[Jett Pad delivery]

Do requested PC actions first. For ordinary conversation or a ping, go straight to the answer: no startup checks, history reads, background status checks, or log maintenance unless this prompt requests them. Be concise unless detail is requested. Deliver your answer immediately: fs.write UTF-8 text to ${base}.txt. If pad_reply is returned, end this turn immediately; delivery is done. Do not append a second reply log, run a housekeeping command, or wait for other work after delivery. Otherwise exec.run command=notepad.exe "${base}.txt", cwd="${documents}". This shows the answer in Jett Pad instead of opening Notepad. No shell wrappers. Do not read old PC history. No bridge chat_reply. If delivery fails, explain here.`;
  }
  function canSend(state) {
    if (!state.sameChat) return 'wrong_chat';
    if (!state.layout) return 'layout';
    if (state.draft || state.humanActive) return 'draft';
    if (state.busy) return 'busy';
    return 'ready';
  }
  function availability(signals) {
    // Callers supply visible service UI only, never chat messages or draft text.
    const exhausted = /(?:\b(?:you(?:['’]ve| have)?\s+)?(?:have\s+)?(?:reached|hit|exceeded)\b.{0,90}\b(?:usage|message|weekly|daily|token|credit|rate)\b.{0,35}\blimit\b|\byou(?:['’]ve| have)?\s+(?:reached|hit|exceeded)\s+(?:your|the)\s+limit\b|\b(?:usage|message|weekly|daily|token|credit|rate)\b.{0,35}\blimit\b.{0,45}\b(?:reached|exceeded|try again)\b|\b(?:out of|no (?:remaining|more))\s+(?:\w+\s+){0,3}(?:credits|messages|tokens)\b)/i;
    return signals.some(value => typeof value === 'string' && value.length <= 2000 && exhausted.test(value)) ? 'quota' : null;
  }
  const api = {wireText, canSend, availability};
  root.JettRelay = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
