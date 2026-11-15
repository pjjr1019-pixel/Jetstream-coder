"""One bounded user-session step; stdin requests, metadata-only action receipts.

This module controls only positively resolved, non-protected windows. No focus
lock registry changes, dummy keys, secure desktop or operator overrides.
"""
import ctypes,json,os,shutil,subprocess,sys,time
from pathlib import Path
from ctypes import wintypes as w
from musebridge.window_targets import WindowCatalog,read_window,desktop_protection,activate_window
from musebridge.windows import api,window_info,structured
from musebridge.desktop import DesktopAccess
from musebridge.permissions import PermissionManager
from musebridge.redaction import redact_text,contains_sensitive
from .input_batch import key as balanced_key,click as balanced_click

issued=False

def capture(function,*args,**kwargs):
    print(json.dumps({'event':'capture.started'}),flush=True)
    try:return function(*args,**kwargs)
    finally:print(json.dumps({'event':'capture.ended'}),flush=True)

def orb_decorations(rows,renderer,root):
    environment=Path(root)/'backend/.orb-venv'
    expected={(environment/'Scripts/pythonw.exe').resolve()}
    # A Windows venv launcher redirects to its configured base interpreter.
    try:
        for line in (environment/'pyvenv.cfg').read_text().splitlines():
            key,separator,value=line.partition('=')
            if separator and key.strip().casefold()=='home':
                home=Path(value.strip())
                if home.is_absolute():expected.add((home/'pythonw.exe').resolve())
    except (OSError,ValueError):pass
    if renderer.get('status')!='running':return []
    return [r['hwnd'] for r in rows if not r.get('protected') and r['pid']==renderer.get('pid') and r['title']=='Jett — Orb' and Path(r['path']).resolve() in expected]

def receipt(op,info=None):
    global issued
    issued=True
    detail=dict(event='action.issued',monotonic_ns=time.monotonic_ns(),op=op)
    if info:detail.update(target_hwnd=info['hwnd'],process_name=os.path.basename(info['path']),title=redact_text(info['title'])[:160],target_protected=False)
    print(json.dumps(detail),flush=True)

def protected(info,operators):
    reason=info.get('protection_reason')
    return 'operator_window' if info['pid'] in operators else reason if reason!='unknown' else None

def resolve(target,operators):
    if not isinstance(target,dict) or not target:raise ValueError('A unique window target is required')
    if set(target)-{'hwnd','pid','process_name','title','title_contains','class_name'}:raise ValueError('Unsupported target selector')
    if 'hwnd' in target:
        info=read_window(int(target['hwnd']))
        if info['pid']!=target.get('pid'):raise ValueError('Window identity changed')
        matches=[info]
    else:
        catalog=WindowCatalog();listing=catalog.enumerate(lambda info:protected(info,operators));matches=[]
        for row in listing['windows']:
            if 'class_name' in target:
                u,_=api();name=ctypes.create_unicode_buffer(256);candidate=catalog.resolve(row['window_id'])
                u.GetClassNameW(candidate['hwnd'],name,len(name))
                if name.value!=target['class_name']:continue
            if 'process_name' in target and row['process_name'].casefold()!=target['process_name'].casefold():continue
            if 'title' in target and row['title']!=target['title']:continue
            if 'title_contains' in target and target['title_contains'].casefold() not in row['title'].casefold():continue
            matches.append(catalog.resolve(row['window_id']))
    if len(matches)!=1:raise ValueError('Window target must match exactly one current window')
    info=matches[0];reason=protected(info,operators)
    if reason:raise ValueError('protected context: '+reason)
    return info

def public(info):return dict(hwnd=info['hwnd'],pid=info['pid'],process_name=os.path.basename(info['path']),title=redact_text(info['title']))

def uia_root(info):
    from pywinauto import Desktop
    return Desktop(backend='uia').window(handle=info['hwnd']).wrapper_object()

def check_point(info,x,y):
    u,_=api();u.WindowFromPoint.argtypes=[w.POINT];u.WindowFromPoint.restype=w.HWND
    u.GetAncestor.argtypes=[w.HWND,w.UINT];u.GetAncestor.restype=w.HWND
    hit=u.WindowFromPoint(w.POINT(round(x),round(y)))
    if u.GetAncestor(hit,2)!=info['hwnd']:raise ValueError('Input point is obscured or belongs to another window')

def select(root,selector):
    if not isinstance(selector,dict) or not selector or set(selector)-{'name','automation_id','control_type'}:raise ValueError('Use an exact UIA selector')
    if any(not isinstance(v,str) or len(v)>200 for v in selector.values()):raise ValueError('Invalid UIA selector')
    matches=[]
    for index,node in enumerate(root.descendants()):
        if index>=1000:raise ValueError('Accessibility tree exceeds local bound')
        if all((node.window_text() if k=='name' else getattr(node.element_info,k))==v for k,v in selector.items()):matches.append(node)
    if len(matches)!=1:raise ValueError('Control selector must match exactly one element')
    node=matches[0]
    if node.element_info.element.CurrentIsPassword or not node.is_visible() or not node.is_enabled():raise ValueError('Control is protected or unavailable')
    return node

def verify(rule,info,operators):
    if not isinstance(rule,dict) or not rule:raise ValueError('A concrete postcondition is required')
    if set(rule)-{'selector','value','name','foreground','window','focused'}:raise ValueError('Unsupported postcondition')
    if 'window' in rule:
        info=resolve(rule['window'],operators)
        if len(rule)==1:return True
    current=read_window(info['hwnd'])
    if current['pid']!=info['pid'] or protected(current,operators):raise ValueError('Target changed or protected')
    if rule.get('foreground') is True:
        if window_info()['hwnd']!=info['hwnd']:raise ValueError('Foreground does not match target')
    elif 'selector' not in rule:raise ValueError('Postcondition needs selector, foreground or window')
    if 'selector' in rule:
        node=select(uia_root(current),rule['selector'])
        if 'value' in rule and node.get_value()!=rule['value']:raise ValueError('Expected control value not observed')
        if 'name' in rule and node.window_text()!=rule['name']:raise ValueError('Expected control name not observed')
        if 'focused' in rule and bool(node.has_keyboard_focus())!=rule['focused']:raise ValueError('Expected control focus not observed')
    return True

def taskbar(step,operators):
    from pywinauto import Desktop
    u,_=api();u.FindWindowW.argtypes=[w.LPCWSTR,w.LPCWSTR];u.FindWindowW.restype=w.HWND
    hwnd=u.FindWindowW('Shell_TrayWnd',None)
    if not hwnd:raise ValueError('Windows taskbar unavailable')
    info=read_window(hwnd)
    if protected(info,operators):raise ValueError('Protected taskbar context')
    node=select(Desktop(backend='uia').window(handle=hwnd).wrapper_object(),step.get('selector'))
    # Check the requested target and actual taskbar label before invoking it;
    # postcondition failure is too late if it activated an operator window.
    target=step.get('window')
    if not target:raise ValueError('Taskbar activation requires expected window selector')
    try:resolve(target,operators)
    except ValueError as error:
        if 'protected context:' in str(error):raise
    label=node.window_text().casefold().strip()
    catalog=WindowCatalog();listing=catalog.enumerate(lambda item:protected(item,operators))
    for item in listing['windows']:
        title=item.get('title','').casefold().strip()
        if item.get('protected') and title and (label==title or label.startswith(title+' ') or title.startswith(label+' ') and label):
            raise ValueError('Protected operator/credential taskbar item')
    receipt('taskbar',info)
    try:node.invoke()
    except Exception:
        # Re-observe the actual hit window before a physical taskbar click.
        rect=node.rectangle();point=w.POINT((rect.left+rect.right)//2,(rect.top+rect.bottom)//2)
        u.WindowFromPoint.argtypes=[w.POINT];u.WindowFromPoint.restype=w.HWND
        hit=u.WindowFromPoint(point);u.GetAncestor.argtypes=[w.HWND,w.UINT];u.GetAncestor.restype=w.HWND
        if u.GetAncestor(hit,2)!=hwnd:raise ValueError('Taskbar icon is obscured; no blind click')
        balanced_click(point.x,point.y)
    deadline=time.monotonic()+min(10,step.get('timeout_secs',10))
    while time.monotonic()<deadline:
        try:
            found=resolve(target,operators)
            if window_info()['hwnd']==found['hwnd']:return dict(ok=True,window=public(found),verified='foreground')
        except ValueError:pass
        time.sleep(.05)
    raise ValueError('Taskbar action did not produce the expected foreground')

def activate_app(app_id):
    import comtypes
    from comtypes import GUID,HRESULT,IUnknown,COMMETHOD
    class ActivationManager(IUnknown):
        _iid_=GUID('{2e941141-7f97-4756-ba1d-9decde894a3d}')
        _methods_=[COMMETHOD([],HRESULT,'ActivateApplication',(['in'],w.LPCWSTR,'appUserModelId'),
            (['in'],w.LPCWSTR,'arguments'),(['in'],ctypes.c_int,'options'),(['out'],ctypes.POINTER(w.DWORD),'processId'))]
    comtypes.CoInitialize()
    try:
        manager=comtypes.CoCreateInstance(GUID('{45ba127d-10a8-46ea-8ab7-56ea9078943c}'),interface=ActivationManager,clsctx=comtypes.CLSCTX_INPROC_SERVER|comtypes.CLSCTX_LOCAL_SERVER)
        return int(manager.ActivateApplication(app_id,'',0))
    finally:comtypes.CoUninitialize()

def run(request):
    step=request['step'];op=step['op'];operators=set(request['operators']);cfg=request['config']
    if desktop_protection() is not None:raise ValueError('Interactive desktop unavailable')
    if op=='run':
        permissions=PermissionManager(cfg,Path(request['config_path']))
        cwd=permissions.authorize_cwd(step['cwd'])
        if not cfg['features'].get('exec'):raise ValueError('Execution is disabled')
        if cfg.get('execution_mode','host')!='host':raise ValueError('Host execution is disabled')
        receipt('run')
        # Owned by the parent's Windows Job Object; no breakaway flags.
        process=subprocess.Popen(step['argv'],cwd=cwd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        tail=bytearray();total=0
        while True:
            chunk=process.stdout.read(8192)
            if not chunk:break
            total+=len(chunk);tail.extend(chunk)
            if len(tail)>8192:del tail[:-8192]
        code=process.wait()
        return dict(ok=code==0,exit_code=code,output_tail=redact_text(tail.decode('utf-8',errors='replace')),output_truncated=total>8192,
            error=None if code==0 else 'Script returned a failing exit code')
    if op=='screenshot':
        from .capture import run as compose,snapshot
        rows=snapshot(operators);u,_=api();x,y,width,height=[u.GetSystemMetrics(n) for n in (76,77,78,79)]
        try:renderer=json.loads(Path(request.get('orb_renderer') or (Path(os.environ['LOCALAPPDATA'])/'JettOrb/renderer.json')).read_text())
        except (OSError,ValueError):renderer={}
        decorations=orb_decorations(rows,renderer,Path(__file__).resolve().parents[1])
        result=capture(compose,dict(mode='desktop',target=None,bounds=[x,y,x+width,y+height],operators=list(operators),decorations=decorations,
            allowed=[dict(hwnd=i['hwnd'],pid=i['pid']) for i in rows if not i['protected']],excluded=[],max_edge=step.get('max_edge',1600),quality=80))
        if result['all_unavailable']:raise ValueError('No permitted window could be captured')
        receipt('screenshot')
        return dict(ok=True,format='jpeg',data_b64=result['data_b64'],width=result['width'],height=result['height'],
            protected_regions=result['protected_regions'],unavailable=result['unavailable'],capture_scope='permitted_window_desktop_composition',
            target_protected=False,protected_sources_captured=False,protection_policy='protected sources excluded before capture; their regions are black')
    if op=='taskbar':return taskbar(step,operators)
    if op=='open':
        if step.get('app_id'):
            receipt('open_store_app');pid=activate_app(step['app_id'])
        else:
            path=step.get('path');args=step.get('args',[])
            if not isinstance(path,str) or not isinstance(args,list) or any(not isinstance(a,str) for a in args):raise ValueError('Invalid application launch')
            exe=shutil.which(path) or (path if Path(path).is_file() else None)
            if not exe:raise ValueError('Application executable not found')
            if Path(exe).name.casefold() in {'cmd.exe','powershell.exe','pwsh.exe','python.exe','pythonw.exe','wscript.exe','cscript.exe','mshta.exe','rundll32.exe'}:raise ValueError('Use the separately authorized run operation for script hosts')
            if any(part.casefold() in {'jettpad','musebridge'} for part in Path(exe).parts):raise ValueError('Operator application launch is protected')
            if not step.get('window'):raise ValueError('Application launch requires an expected window selector')
            # Explorer's existing user-session broker owns the launched app;
            # ending this bounded automation helper must not close the app.
            import win32com.client
            receipt('open');win32com.client.Dispatch('Shell.Application').ShellExecute(exe,subprocess.list2cmdline(args),'','open',1);pid=None
        if not step.get('window'):raise ValueError('Application launch requires an expected window selector')
        end=time.monotonic()+min(20,step.get('timeout_secs',15))
        while time.monotonic()<end:
            try:return dict(ok=True,pid=pid,window=public(resolve(step['window'],operators)),verified='expected_window')
            except ValueError:time.sleep(.05)
        raise ValueError('Application launch not verified by expected window')
    info=resolve(step.get('target'),operators)
    if op=='find':return dict(ok=True,window=public(info),verified='unique_window')
    if op=='close':
        # Post the application's normal close request. Never force terminate,
        # dismiss prompts, or switch focus. Preserve ambiguous outcomes.
        fresh=read_window(info['hwnd'])
        if fresh['pid']!=info['pid'] or protected(fresh,operators):raise ValueError('Target changed or protected')
        u,_=api();u.PostMessageW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM];u.PostMessageW.restype=w.BOOL
        u.IsWindow.argtypes=[w.HWND];u.IsWindow.restype=w.BOOL
        receipt('close',fresh)
        if not u.PostMessageW(fresh['hwnd'],0x0010,0,0):raise ValueError('Windows refused the close request')
        deadline=time.monotonic()+min(8,step.get('timeout_secs',15)*.6)
        while time.monotonic()<deadline:
            if not u.IsWindow(fresh['hwnd']):return dict(ok=True,window=public(fresh),verified='window_closed')
            try:current=read_window(fresh['hwnd'])
            except (ValueError,OSError):raise ValueError('Window identity cannot be verified after close') from None
            if current['pid']!=fresh['pid']:return dict(ok=True,window=public(fresh),verified='original_window_closed')
            time.sleep(.05)
        raise ValueError('Close was requested but the window is still open; check for a confirmation or unsaved work')
    if op=='activate':
        receipt(op,info)
        try:activate_window(info,__import__('threading').Event(),away=True)
        except ValueError:
            if not step.get('selector'):raise
            return taskbar({**step,'window':public(info)},operators)
        return dict(ok=True,window=public(info),verified='foreground')
    if op=='inspect':return dict(ok=True,window=public(info),**structured(info,'inspect',{}))
    if op not in ('click','type','key'):raise ValueError('Unsupported native operation')
    # Verify the intended foreground before every input. Never type through a
    # protected/changed foreground, even if a target selector still matches.
    if window_info()['hwnd']!=info['hwnd']:activate_window(info,__import__('threading').Event(),away=True)
    current=read_window(info['hwnd'])
    if protected(current,operators) or current['pid']!=info['pid']:raise ValueError('Input target changed or protected')
    desktop=DesktopAccess(cfg);desktop.connected=True;desktop.enabled=True;desktop.operator_pids=operators
    if step.get('selector'):
        node=select(uia_root(current),step['selector'])
        if window_info()['hwnd']!=current['hwnd']:raise ValueError('Foreground changed before UIA input')
        receipt(op,current)
        if op=='type':
            text=step.get('text')
            if not isinstance(text,str) or len(text)>10000 or contains_sensitive(text):raise ValueError('Text is invalid or contains sensitive data')
            node.set_edit_text(text)
        elif op=='click':
            try:interface=node.iface_invoke
            except Exception:
                rect=node.rectangle();x,y=(rect.left+rect.right)//2,(rect.top+rect.bottom)//2;check_point(current,x,y);balanced_click(x,y)
            else:node.invoke()
        else:raise ValueError('Key input uses the window target, not a selector')
    else:
        frame=capture(desktop.screenshot,max_edge=2048,window_id=desktop.catalog.remember(current))
        payload={'frame_id':frame['frame_id']}
        if op=='click':
            check_point(current,current['rect'][0]+step['x'],current['rect'][1]+step['y'])
            payload.update(x=round(step['x']*frame['width']/frame['source_width']),y=round(step['y']*frame['height']/frame['source_height']),button=step.get('button','left'))
        elif op=='type':payload['text']=step['text']
        else:payload['key']='+'.join(step['keys']) if isinstance(step['keys'],list) else step['keys']
        receipt(op,current)
        if op in ('key','click'):
            desktop._ensure_control_context()
            if window_info()!=frame['window'] or time.monotonic()-desktop._frame_at>15:raise ValueError('Target changed after local observation')
            desktop._consume_frame()
            if op=='key':balanced_key(step['keys'])
            else:balanced_click(current['rect'][0]+step['x'],current['rect'][1]+step['y'],step.get('button','left'))
        else:desktop.act(op,payload)
    # Return a failed postcondition before the parent's hard deadline.
    deadline=time.monotonic()+min(5,step.get('timeout_secs',15)*.5)
    while True:
        try:verify(step['verify'],current,operators);break
        except ValueError:
            if time.monotonic()>=deadline:raise ValueError('Requested postcondition was not observed') from None
            time.sleep(.05)
    return dict(ok=True,window=public(read_window(info['hwnd'])),verified='requested_postcondition')

if __name__=='__main__':
    try:result=run(json.loads(sys.stdin.read(512*1024)))
    except Exception as error:result=dict(ok=False,error=redact_text(str(error))[:500],issued=issued)
    print(json.dumps(result,ensure_ascii=True),flush=True)
