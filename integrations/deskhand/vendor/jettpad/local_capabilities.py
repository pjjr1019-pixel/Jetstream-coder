"""Detect local model capabilities through Ollama; never infer them from names."""
import json,re
from urllib.request import Request,build_opener,ProxyHandler

def capabilities(model,opener=None):
    if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}',model) or '..' in model.split('/'):raise ValueError('Invalid model name')
    req=Request('http://127.0.0.1:11434/api/show',data=json.dumps({'model':model}).encode(),headers={'Content-Type':'application/json'})
    with (opener or build_opener(ProxyHandler({}))).open(req,timeout=5) as response:raw=response.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('Model metadata is too large')
    info=json.loads(raw)
    caps=info.get('capabilities') if isinstance(info,dict) else None
    if not isinstance(caps,list) or any(not isinstance(x,str) for x in caps):return {'tools':False,'vision':False,'known':False}
    return {'tools':'tools' in caps,'vision':'vision' in caps,'known':True}

