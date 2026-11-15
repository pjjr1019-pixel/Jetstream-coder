"""Bounded text excerpts ranked by cached embeddinggemma vectors."""
import hashlib
import json
import math
from pathlib import Path
import re


def approved_file(value,roots):
    path=Path(value).resolve(strict=True)
    if not any(path.is_relative_to(Path(root).resolve()) for root in roots):
        raise ValueError('Context path outside locally approved read roots')
    if not path.is_file() or path.suffix.lower() not in {'.txt','.md','.py','.js','.ts','.json','.jsonl','.csv','.log','.html','.css','.toml','.rst'}:
        raise ValueError('Context must name a supported text file')
    if re.search(r'(password|credential|secret|token|\.env)',path.name,re.I):
        raise ValueError('Credential-like context filename is not allowed')
    if path.stat().st_size>4*1024*1024:raise ValueError('Context file exceeds 4 MiB; supply a bounded extract')
    return path


def chunks(label,text):
    # Stable overlapping text windows; labels preserve evidence provenance.
    return [(label+' chars '+str(start)+'-'+str(min(start+1800,len(text))),text[start:start+1800])
            for start in range(0,len(text),1600)]


class Retrieval:
    def __init__(self,db,ollama,roots):self.db,self.ollama,self.roots=db,ollama,roots;self.used_embedding=False
    def vector(self,texts):
        result=[None]*len(texts);missing=[]
        for i,text in enumerate(texts):
            digest=hashlib.sha256(('embeddinggemma:2048:'+text).encode()).hexdigest()
            row=self.db.execute('select vector from embeddings where digest=?',(digest,)).fetchone()
            if row:result[i]=json.loads(row[0])
            else:missing.append((i,digest,text))
        for start in range(0,len(missing),12):
            group=missing[start:start+12];self.used_embedding=True
            vectors=self.ollama.embed([row[2] for row in group])
            for (i,digest,_),vector in zip(group,vectors):
                result[i]=vector
                self.db.execute('insert or replace into embeddings values (?,?)',(digest,json.dumps(vector)))
            self.db.commit()
        return result

    def prepare(self,task,web_docs=()):
        documents=[]
        for name in task['context_files']:
            path=approved_file(name,self.roots)
            raw=path.read_bytes()
            if len(raw)>4*1024*1024:raise ValueError('Context grew beyond 4 MiB')
            if b'\x00' in raw:raise ValueError('Binary context is not supported')
            documents.append((str(path),raw.decode('utf-8-sig',errors='replace')))
        documents.extend(web_docs)
        parts=[part for label,text in documents for part in chunks(label,text)]
        if len(parts)>256:raise ValueError('Context exceeds 256 excerpts; narrow the task context')
        if not parts:return ''
        vectors=self.vector([text for _,text in parts]+[(task['title']+'\n'+task['instructions'])[:4000]])
        query=vectors[-1]
        def score(vector):
            if len(vector)!=len(query):raise ValueError('Embedding dimension changed; invalidate cache locally')
            return sum(a*b for a,b in zip(vector,query))/(math.sqrt(sum(a*a for a in vector))*math.sqrt(sum(a*a for a in query)) or 1)
        from context_budget import select_excerpts
        selected=select_excerpts(parts,[score(v) for v in vectors[:-1]],max_items=3,max_chars=5400)
        return '\n\n'.join('SOURCE '+label+'\n'+text for label,text in selected)
