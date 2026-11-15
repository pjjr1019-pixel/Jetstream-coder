"""Host facts, not model assertions: dated notes, status and snapshot freshness."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
from urllib.request import build_opener,ProxyHandler

def rows(path):
    result=[]
    if not path.exists():return result
    for line in path.read_text(encoding='utf-8').splitlines()[-200:]:
        try:result.append(json.loads(line))
        except ValueError:continue
    return result

def changes(package):
    notes=rows(Path(package)/'OFFLINE_NOTES.md')
    return 'Original offline notes only; these are unverified observations, not a cloud memory merge:\n'+('\n'.join(str(r.get('as_of','unknown'))+' | '+str(r.get('text',''))[:500] for r in notes[-12:]) or 'No local notes recorded.')

def stale_day(package, fields):
    path=Path(package)/'JETT_DAY_SNAPSHOT.json'
    if not path.exists():return True
    value=json.loads(path.read_text(encoding='utf-8'))
    for key in fields:
        row=value.get('fields',{}).get(key,{})
        if row.get('value') is None:return True
        try:
            stamp=row['as_of'];ts=float(stamp) if isinstance(stamp,(int,float)) else datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
            if not 0<=time.time()-ts<=86400:return True
        except (KeyError,ValueError,TypeError):return True
    return False

def status(package,data):
    package,data=Path(package),Path(data)
    with closing(sqlite3.connect('file:'+str(data/'browser-relay.sqlite3')+'?mode=ro',uri=True)) as db:
        floor=dict(db.execute('SELECT * FROM relay_control'))
        claims={row[0] for row in db.execute('SELECT id FROM local_claims WHERE epoch=?',(int(floor.get('floor_epoch',0)),))}
    pending=rows(package/'PENDING_FOR_JETT.md')
    receipts=[r for r in rows(package/'OFFLINE_ACTIONS.jsonl') if r.get('request_id') in claims]
    try:
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:11434/api/ps',timeout=1) as response:
            loaded=[r['name'] for r in json.load(response).get('models',[])]
        ollama={'reachable':True,'loaded_models':loaded}
    except Exception:ollama={'reachable':False,'loaded_models':None}
    crew_path=data.parent/'JettCrew/runtime.json'
    try:
        crew=json.loads(crew_path.read_text(encoding='utf-8'));crew={'pid':crew.get('pid'),'runtime_record_age_seconds':round(time.time()-crew_path.stat().st_mtime,1),'meaning':'runtime record only; not an active worker readiness probe'}
    except (OSError,ValueError):crew={'available':False}
    return json.dumps({'floor':floor.get('floor_mode'),'epoch':floor.get('floor_epoch'),
        'pending_record_count':len(pending),'pending_count_meaning':'append-only records; some may already be declined by cloud Jett',
        'session_action_receipts':receipts,'ollama':ollama,'crew':crew},indent=2)
