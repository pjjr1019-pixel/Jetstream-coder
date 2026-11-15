"""Bounded interactive model residency; content-free latency diagnostics."""
import json
import os
from pathlib import Path
import time
import uuid


def keep_alive(facts=None):
    if facts is None:
        try:
            from jettcrew.support import machine_facts
            facts=machine_facts()
        except (OSError,ValueError,ImportError):facts={}
    free=facts.get('memory_free')
    if isinstance(free,(int,float)) and free>=6*1024**3 and facts.get('ac') is True:return '30m'
    if isinstance(free,(int,float)) and free>=3*1024**3:return '5m'
    return '2m'


def record_metrics(folder, endpoint, payload, result, elapsed):
    keys=('total_duration','load_duration','prompt_eval_count','prompt_eval_duration','eval_count','eval_duration')
    value={key:result[key] for key in keys if type(result.get(key)) in (int,float)}
    value.update(ts=time.time(),endpoint=endpoint,model=payload.get('model'),
                 elapsed_ms=round(elapsed*1000,3),keep_alive=payload.get('keep_alive'),
                 num_ctx=payload.get('options',{}).get('num_ctx'),
                 done=result.get('done'),done_reason=result.get('done_reason'))
    path=Path(folder)/'offline-model-metrics.json';temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value),encoding='utf-8');os.replace(temporary,path)
    except OSError:pass  # diagnostics never determine answer/action success
    finally:
        try:temporary.unlink(missing_ok=True)
        except OSError:pass
