"""Truthful labels and non-destructive display cleanup for existing replies."""
import re

def present(row):
    sender=row.get('sender');text=row.get('text','');route=row.get('reply_route');model=row.get('reply_model');chosen=row.get('reply_codex_model');effort=row.get('reply_effort')
    who={'jett':'Jett','phil':'You','offline':'Offline Jett','system':'Jett Pad'}.get(sender,'Jett Pad')
    if sender=='offline' and (route=='codex' or text.startswith('Codex · ')):
        who='Codex';text=re.sub(r'^Codex · ChatGPT\s*\n\s*', '',text,count=1)
        if chosen:who+=' · '+chosen
        if effort:who+=' · '+effort
    elif sender=='offline' and (route=='selected_local' or text.startswith('Local model · ')):
        who='Local model'+(' · '+model if model else '')
        text=re.sub(r'^Local model · [^\n]+\n\s*','',text,count=1)
        text=re.sub(r'^Local model [^\n]+ could not answer \(HTTPError\)\. Your message was not sent to Muse\.$','The local model service returned an error. Check Ollama and retry.',text,count=1)
    elif sender=='offline':
        match=re.match(r'^Offline Jett - local stand-in \| ([^|\n]+) \| last sync ',text)
        if match:who+=' · '+match.group(1).strip()
        text=re.sub(r'^Offline Jett — local stand-in\s*\n\s*','',text,count=1)
    return who,text
