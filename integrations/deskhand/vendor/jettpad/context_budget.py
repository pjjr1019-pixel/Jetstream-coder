"""Conservative local prompt sizing and duplicate excerpt reduction."""
import re


def select_excerpts(parts, scores, *, max_items=3, max_chars=6000):
    selected=[];used=0;fingerprints=[]
    for index in sorted(range(len(parts)),key=lambda i:scores[i],reverse=True):
        label,text=parts[index]
        words=set(re.findall(r'\w+',text.lower()))
        if any(words and len(words&old)/max(1,len(words|old))>.8 for old in fingerprints):
            continue
        if used+len(text)>max_chars:continue  # preserve complete evidence excerpts
        selected.append((label,text));fingerprints.append(words);used+=len(text)
        if len(selected)>=max_items:break
    return selected


def byte_tokens(text):
    # Byte count deliberately overestimates typical token use. This is not a
    # measured model tokenizer count and must not be reported as one.
    return len(text.encode('utf-8'))+64


def add_references(system,current,references,*,max_context=16384,output_tokens=1024,history_reserve=0):
    """Fit whole optional evidence blocks without cutting mandatory policies."""
    room=max_context-byte_tokens(system)-byte_tokens(current)-output_tokens-256-history_reserve
    terms=set(re.findall(r'\w+',current.lower()))-{'the','a','is','to','i','you','and','of','what','me'}
    ranked=[]
    for order,(label,text) in enumerate(references):
        words=set(re.findall(r'\w+',text.lower()))
        ranked.append((len(terms&words),-order,label,text))
    for _,_,label,text in sorted(ranked,reverse=True):
        block='\n\n'+label+'\n'+text
        cost=len(block.encode('utf-8'))
        if cost<=room:
            system+=block;room-=cost
    return system


def fit_messages(system, current, history, *, max_context=16384, output_tokens=1024):
    mandatory=byte_tokens(system)+byte_tokens(current)+output_tokens+256
    if mandatory>max_context:
        raise ValueError('Required local context exceeds the safe budget; narrow the request or reference package')
    remaining=min(4000,max_context-mandatory)
    chosen=[]
    for row in reversed(history):
        if row.get('sender') not in ('phil','offline'):continue
        content=row['text']
        cost=byte_tokens(content)
        if cost>remaining:break  # never replace recent history with older fragments
        chosen.append({'role':'user' if row['sender']=='phil' else 'assistant','content':content})
        remaining-=cost
    messages=[{'role':'system','content':system},*reversed(chosen),{'role':'user','content':current}]
    required=sum(byte_tokens(m['content']) for m in messages)+output_tokens+256
    context=next(size for size in (4096,8192,16384) if size>=required and size<=max_context)
    return messages,context
