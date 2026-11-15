"""Bounded immutable task recipes, separate from crew/model task files."""
import hashlib,json,math,re,uuid
OPS={'open','close','find','activate','taskbar','click','type','key','wait','screenshot','run','inspect'}
TRANSPORT_END={'bridge_disconnect','server_stopping','session_monitor_stopping'}

def identity(value):
    try:return str(uuid.UUID(value))
    except (ValueError,TypeError,AttributeError):raise ValueError('Use a UUID job id') from None

def validate_task(value):
    if not isinstance(value,dict) or set(value)-{'title','steps','timeout_secs'}:raise ValueError('Use title, steps and timeout_secs')
    title=value.get('title','Windows task');steps=value.get('steps');budget=value.get('timeout_secs',300)
    if not isinstance(title,str) or not 1<=len(title)<=160:raise ValueError('Invalid task title')
    if type(budget) is not int or not 1<=budget<=1800:raise ValueError('Job budget must be 1–1800 seconds')
    if not isinstance(steps,list) or not 1<=len(steps)<=128:raise ValueError('Use 1–128 steps')
    if len(json.dumps(value,allow_nan=False).encode())>256*1024:raise ValueError('Task exceeds 256KiB')
    allowed={'op','target','selector','text','keys','x','y','button','seconds','path','args','app_id','save_as',
        'window','timeout_secs','retries','retry_safe','verify','fallbacks','argv','cwd','max_edge'}
    def target(value,alias=True):
        if alias and isinstance(value,str) and re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,31}',value):return
        if isinstance(value,dict) and 'window_id' in value:
            if set(value)!={'window_id'}:raise ValueError('window_id cannot be combined with other selector fields')
            identity(value['window_id']);return
        if not isinstance(value,dict) or not value or set(value)-{'hwnd','pid','process_name','title','title_contains','class_name'}:raise ValueError('Invalid window selector')
        if 'hwnd' in value and (type(value['hwnd']) is not int or type(value.get('pid')) is not int):raise ValueError('HWND targets require their process id')
        if any(not isinstance(v,str) or len(v)>512 for k,v in value.items() if k not in ('hwnd','pid')):raise ValueError('Invalid window text selector')
    def selector(value):
        if not isinstance(value,dict) or not value or set(value)-{'name','automation_id','control_type'} or any(not isinstance(v,str) or len(v)>200 for v in value.values()):raise ValueError('Invalid exact UIA selector')
    def step(row,nested=False):
        if not isinstance(row,dict) or set(row)-allowed:raise ValueError('Unsupported step fields')
        if row.get('op') not in OPS:raise ValueError('Unsupported executor operation')
        if type(row.get('retries',0)) is not int or not 0<=row.get('retries',0)<=3:raise ValueError('Use at most three retries')
        if type(row.get('retry_safe',False)) is not bool:raise ValueError('retry_safe must be boolean')
        timeout=row.get('timeout_secs',15)
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .05<=timeout<=1800:raise ValueError('Invalid step timeout')
        if row['op']=='wait' and (type(row.get('seconds')) not in (int,float) or not 0<=row['seconds']<=1800):raise ValueError('Invalid wait')
        if row['op'] in ('click','type','key') and not isinstance(row.get('verify'),dict):raise ValueError('GUI input requires a verify postcondition')
        if row['op'] in ('find','activate','close','click','type','key','inspect'):
            if 'target' not in row:raise ValueError(row['op']+' requires a target window selector or saved alias')
            target(row.get('target'))
        if 'selector' in row:selector(row['selector'])
        if row['op'] in ('open','taskbar'):target(row.get('window'),False)
        if row['op']=='taskbar':selector(row.get('selector'))
        if row['op']=='type' and (not isinstance(row.get('text'),str) or len(row['text'])>10000):raise ValueError('Invalid input text')
        if row['op']=='type' and 'selector' not in row and len(row['text'])>2000:raise ValueError('Long input requires an exact UIA selector; keyboard typing is capped at 2000 characters')
        if 'app_id' in row and (not isinstance(row['app_id'],str) or len(row['app_id'])>255 or not re.fullmatch(r'[A-Za-z0-9_.-]+![A-Za-z0-9_.-]+',row['app_id'])):raise ValueError('Use a nonempty installed packaged application AUMID')
        if row['op']=='key' and not (isinstance(row.get('keys'),str) or isinstance(row.get('keys'),list) and row['keys'] and all(isinstance(k,str) for k in row['keys'])):raise ValueError('Invalid key sequence')
        if row['op']=='click' and 'selector' not in row and any(type(row.get(k)) is not int or not 0<=row[k]<32768 for k in ('x','y')):raise ValueError('Click coordinates must be window-relative integers')
        if 'verify' in row:
            rule=row['verify']
            if not isinstance(rule,dict) or not rule or set(rule)-{'selector','value','name','foreground','window','focused'}:raise ValueError('Invalid postcondition')
            if 'window' in rule:target(rule['window'],False)
            if 'selector' in rule:selector(rule['selector'])
            if not ('window' in rule or 'selector' in rule or rule.get('foreground') is True):raise ValueError('A concrete postcondition is required')
            if any(type(rule[k]) is not bool for k in ('focused','foreground') if k in rule):raise ValueError('Invalid postcondition flags')
        if row['op']=='run':
            argv=row.get('argv')
            if not isinstance(argv,list) or not argv or not argv[0] or len(argv)>128 or any(not isinstance(a,str) or len(a)>8000 or '\0' in a for a in argv):raise ValueError('run requires an argument array, not a shell string')
        if 'save_as' in row and (not isinstance(row['save_as'],str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,31}',row['save_as'])):raise ValueError('Invalid window alias')
        if not isinstance(row.get('fallbacks',[]),list) or len(row.get('fallbacks',[]))>3:raise ValueError('Use at most three explicit fallbacks')
        if 'max_edge' in row and (type(row['max_edge']) is not int or not 320<=row['max_edge']<=2048):raise ValueError('Capture max_edge must be 320–2048')
        for fallback in row.get('fallbacks',[]):
            if not isinstance(fallback,dict) or nested or 'fallbacks' in fallback:raise ValueError('Fallbacks must be objects and cannot nest')
            step(fallback,True)
    for index,row in enumerate(steps,1):
        try:step(row)
        except ValueError as error:
            op=row.get('op') if isinstance(row,dict) else None
            # Only known operation names are echoed, never task text or paths.
            label=op if op in OPS else 'invalid operation'
            raise ValueError(f'Step {index} ({label}): {error}') from None
    clean=json.loads(json.dumps(dict(title=title,steps=steps,timeout_secs=budget),allow_nan=False))
    return clean,hashlib.sha256(json.dumps(clean,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def required(task):
    ops={s['op'] for s in task['steps']}
    for s in task['steps']:ops.update(f['op'] for f in s.get('fallbacks',[]))
    return {'desktop_control'}|({'screenshots'} if ops&{'click','type','key','screenshot'} else set())|({'exec'} if 'run' in ops else set())
