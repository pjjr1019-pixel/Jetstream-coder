"""Paired human input only. No command runner, cloud frames or grant writes."""
import ctypes,json,math,os,secrets,subprocess,sys,threading,time
from pathlib import Path
from ctypes import wintypes as w

class PhoneInput:
    def __init__(self,bridge,native=None,clock=time.monotonic):
        self.bridge=bridge;self.native=native or NativeInput(bridge);self.clock=clock
        self.lock=threading.RLock();self.session=None;self.last_reason='Unlock PC access to control'
        self.stop=threading.Event();self.cancel_requested=threading.Event()
        threading.Thread(target=self._watch,name='Paired phone input watchdog',daemon=True).start()
    @property
    def active(self):return self.session is not None
    def _watch(self):
        while not self.stop.wait(.05):
            with self.lock:
                if self.session:
                    try:self._check(self.session['owner']);self._fresh(self.session['last_age']+(self.clock()-self.session['last']))
                    except Exception:self.end('Input authority or fresh video ended')
    def _audit(self,reason,op=None):
        # Fixed metadata only. Never input text, PINs, titles or coordinates.
        from companion_data import home
        with (home()/'phone-input-events.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'ts':time.time(),'event':'phone.input','reason':reason,'op':op})+'\n')
    def _state(self):
        from companion_data import home
        state=json.loads((home()/'companion-stream.json').read_text())
        if not 0<=time.time()-state.get('ts',0)<2 or state.get('phase')!='streaming':raise ValueError('Live viewing is required')
        return state
    def _check(self,owner):
        b=self.bridge
        if self.session and self.cancel_requested.is_set():raise ValueError('Input interrupted')
        if not owner():raise ValueError('Phone connection ended')
        if not b.config['features'].get('desktop_control'):raise ValueError('PC access stopped or disabled')
        if not b.remote_control.check():raise ValueError('Viewing only—unlock PC access to control')
        b.permissions.check_session('desktop')
        if b.executor.reserved or b.executor.input_active:raise ValueError('Jett executor owns desktop input')
        if not b.desktop.enabled:raise ValueError('No active desktop control session')
        state=self._state();geometry=self.native.geometry()
        if self.session and (self.session['epoch']!=b.remote_control.epoch or self.session['generation']!=b.desktop._generation
            or self.session['geometry']!=geometry or self.session['stream']!=(state.get('boot_id'),state.get('generation'),state.get('intent_id'))):
            raise ValueError('Display, stream or authorization changed')
        return state,geometry
    @staticmethod
    def _fresh(age):
        if type(age) not in (int,float) or not math.isfinite(age) or not 0<=age<1.5:raise ValueError('Fresh decoded video required')
    def status(self,owner):
        try:
            state,geometry=self._check(owner)
            return {'available':True,'active':self.active,'geometry':geometry,'reason':'Direct touch ready','unlock_expires_at':self.bridge.remote_control.expires}
        except Exception as error:return {'available':False,'active':False,'reason':str(error)}
    def begin(self,owner,age):
        with self.lock:
            self._fresh(age)
            if self.session:raise ValueError('Another phone gesture is active')
            with self.bridge.desktop._lock:
                if self.bridge.desktop._active_control_operations:raise ValueError('Jett owns desktop input')
                state,geometry=self._check(owner)
                self.cancel_requested.clear()
                self.bridge.desktop._active_control_operations+=1
                token=secrets.token_urlsafe(24)
                self.session={'token':token,'owner':owner,'epoch':self.bridge.remote_control.epoch,'generation':self.bridge.desktop._generation,
                    'geometry':geometry,'stream':(state.get('boot_id'),state.get('generation'),state.get('intent_id')),'seq':0,'last':self.clock(),'last_age':age,'held':False,'target':None}
            self._audit('begin');return {'session':token,'geometry':geometry}
    def events(self,token,seq,events,age):
        with self.lock:
            s=self.session
            if not s or token!=s['token']:raise ValueError('Input session ended; make a fresh gesture')
            try:
                self._fresh(age);self._check(s['owner'])
                if type(seq) is not int or seq!=s['seq']:raise ValueError('Duplicate or out-of-order input refused')
                if not isinstance(events,list) or not 1<=len(events)<=8:raise ValueError('Bounded input batch required')
                s['seq']+=1;s['last']=self.clock();s['last_age']=age
                for event in events:
                    self._check(s['owner']);op=event.get('op')
                    if op not in ('click','double_click','right_click','down','move','up','scroll','text','key','keepalive'):raise ValueError('Unsupported phone input')
                    if op=='keepalive':continue
                    if op in ('text','key'):
                        if s['held']:raise ValueError('Finish dragging before typing')
                        target=self.native.target(None)
                    else:
                        x,y=event.get('x'),event.get('y')
                        if any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in (x,y)):raise ValueError('Point outside displayed desktop')
                        x=round(x*(s['geometry']['width']-1));y=round(y*(s['geometry']['height']-1))
                        target=self.native.target((x,y))
                        if s['held'] and target!=s['target']:raise ValueError('Drag target changed or obscured')
                        if op=='down':
                            if s['held']:raise ValueError('Mouse button already down')
                            s['target']=target;s['held']=True
                        if op=='move' and not s['held']:raise ValueError('No active drag')
                    self.native.perform(op,event,(x,y) if op not in ('text','key') else None,lambda:self._check(s['owner']))
                    if op=='up':s['held']=False;s['target']=None
                    self._audit('action',op)
                return {'ok':True,'next_seq':s['seq']}
            except Exception:
                self.end('Input refused or interrupted');raise
    def end(self,reason='Phone gesture ended',token=None):
        if token is None or self.session and token==self.session['token']:self.cancel_requested.set()
        with self.lock:
            if not self.session or token is not None and token!=self.session['token']:return {'ok':True}
            session=self.session;self.session=None;self.last_reason=reason
            try:self.native.release()
            finally:
                with self.bridge.desktop._lock:self.bridge.desktop._active_control_operations=max(0,self.bridge.desktop._active_control_operations-1)
            self._audit('ended');return {'ok':True}

class NativeInput:
    def __init__(self,bridge):self.bridge=bridge;self.held=False;self.release_guard=None
    def arm_release_guard(self):
        if self.release_guard:raise ValueError('Drag cleanup still active')
        from .runtime_paths import helper_command
        p=subprocess.Popen(helper_command('musebridge.phone_input_release'),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            text=True,bufsize=1,creationflags=0x08000000,close_fds=True)
        self.release_guard=p
        ready=threading.Event();answer=[]
        def read():
            try:answer.append(p.stdout.readline().strip())
            finally:ready.set()
        threading.Thread(target=read,daemon=True).start()
        if not ready.wait(2) or answer!=['ready']:
            try:p.stdin.close()
            finally:self.release_guard=None
            raise ValueError('Drag cleanup watchdog unavailable')
    def disarm_release_guard(self):
        p=self.release_guard;self.release_guard=None
        if p:
            try:p.stdin.write('disarm\n');p.stdin.flush()
            except (OSError,ValueError):pass
            finally:p.stdin.close()
    def geometry(self):
        from .windows import api
        u,_=api()
        # The current Sunshine configuration selects the sole primary monitor.
        # Refuse ambiguous multi-monitor mapping rather than click the wrong display.
        if u.GetSystemMetrics(80)!=1:raise ValueError('Touch control requires one active display; viewing is still available')
        width,height=u.GetSystemMetrics(0),u.GetSystemMetrics(1)
        return {'width':width,'height':height,'id':'primary','origin_x':0,'origin_y':0}
    def target(self,point):
        from .windows import api,window_info
        from .window_targets import desktop_protection,read_window
        if desktop_protection() is not None:raise ValueError('Secure or unavailable Windows desktop')
        u,_=api()
        if point is None:hwnd=window_info()['hwnd']
        else:
            u.WindowFromPoint.argtypes=[w.POINT];u.WindowFromPoint.restype=w.HWND
            u.GetAncestor.argtypes=[w.HWND,w.UINT];u.GetAncestor.restype=w.HWND
            hwnd=u.GetAncestor(u.WindowFromPoint(w.POINT(*point)),2)
        info=read_window(hwnd);reason=self.bridge.desktop._target_reason(info)
        if reason:raise ValueError('Protected window: '+reason)
        if not info.get('path'):raise ValueError('Cannot establish input target identity')
        return (info['hwnd'],info['pid'])
    def perform(self,op,event,point,check):
        from executor.input_batch import INPUT,DATA,MOUSE,send,key,click
        from .windows import unicode_text
        if op in ('click','double_click','right_click'):
            for n in range(2 if op=='double_click' else 1):
                check();self.target(point);click(*point,'right' if op=='right_click' else 'left')
        elif op=='key':
            value=event.get('key')
            if not isinstance(value,str) or len(value)>40:raise ValueError('Invalid key')
            check();self.target(None);key(value)
        elif op=='text':
            text=event.get('text')
            if not isinstance(text,str) or not 1<=len(text)<=100 or '\0' in text:raise ValueError('Send 1–100 characters at a time')
            # Existing password-window exclusion applies before every text segment.
            for c in text:check();self.target(None);unicode_text(c)
        else:
            geo=self.geometry();x,y=point
            check();self.target(point)
            move=INPUT(0,DATA(mouse=MOUSE(round(x*65535/max(1,geo['width']-1)),round(y*65535/max(1,geo['height']-1)),0,0x8001,0,0)))
            if op=='down':
                self.arm_release_guard();check();self.held=True;send([move,INPUT(0,DATA(mouse=MOUSE(0,0,0,2,0,0)))])
            elif op=='up':send([move,INPUT(0,DATA(mouse=MOUSE(0,0,0,4,0,0)))]);self.held=False;self.disarm_release_guard()
            elif op=='move':send([move])
            elif op=='scroll':
                delta=event.get('delta')
                if type(delta) is not int or not -1200<=delta<=1200:raise ValueError('Invalid scroll')
                send([move,INPUT(0,DATA(mouse=MOUSE(0,0,delta & 0xffffffff,0x800,0,0)))])
    def release(self):
        if self.held:
            from executor.input_batch import INPUT,DATA,MOUSE,send
            try:send([INPUT(0,DATA(mouse=MOUSE(0,0,0,4,0,0)))])
            finally:self.held=False;self.disarm_release_guard()
        elif self.release_guard:self.disarm_release_guard()
