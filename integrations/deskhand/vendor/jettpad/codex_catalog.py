"""Read the Codex catalog for one account, over the stdio app-server interface."""
import json,os,queue,subprocess,threading,time
from codex_model import codex_path
from codex_accounts import home

class _Session:
    """One stdio app-server connection: spawn, reader thread, request/response, cleanup.

    models() and usage() share this; only the client name, the error wording and
    the post-handshake calls differ.
    """
    def __init__(self, account, client_name, unavailable, timed_out):
        self.account=account;self.client_name=client_name;self.unavailable=unavailable;self.timed_out=timed_out
    def __enter__(self):
        exe=codex_path()
        if not exe:raise RuntimeError('Codex is not installed.')
        env=os.environ.copy()
        if self.account!='codex':env['CODEX_HOME']=str(home(self.account.removeprefix('codex:')))
        self.proc=subprocess.Popen([exe,'app-server','--listen','stdio://'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.q=queue.Queue()
        def reader():
            try:
                for line in self.proc.stdout:
                    try:self.q.put(json.loads(line))
                    except ValueError:pass
            finally:self.q.put(None)
        threading.Thread(target=reader,daemon=True).start()
        return self
    def __exit__(self, *exc):
        proc=self.proc
        if proc.poll() is None:proc.terminate()
        try:proc.wait(timeout=3)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        return False
    def send(self, obj):self.proc.stdin.write(json.dumps(obj)+'\n');self.proc.stdin.flush()
    def get(self, identity):
        end=time.monotonic()+15
        while time.monotonic()<end:
            try:obj=self.q.get(timeout=max(.1,end-time.monotonic()))
            except queue.Empty:break
            if obj is None:break
            if obj.get('id')==identity:
                if obj.get('error'):raise RuntimeError(self.unavailable)
                return obj.get('result',{})
        raise RuntimeError(self.timed_out)
    def handshake(self):
        self.send({'id':1,'method':'initialize','params':{'clientInfo':{'name':self.client_name,'version':'1'}}});self.get(1)
        self.send({'method':'initialized','params':{}});self.send({'id':2,'method':'account/read','params':{'refreshToken':False}})
        if not self.get(2).get('account'):raise RuntimeError('Sign in to this Codex account in Settings.')


def models(account):
    with _Session(account,'jettpad_model_picker','Codex model list is unavailable.','Codex model list timed out.') as session:
        session.handshake()
        data=[];cursor=None;identity=3
        while True:
            params={'limit':100,'includeHidden':False}
            if cursor:params['cursor']=cursor
            session.send({'id':identity,'method':'model/list','params':params});res=session.get(identity);identity+=1
            for x in res.get('data',[]):
                if isinstance(x,dict) and isinstance(x.get('model'),str) and not x.get('hidden'):
                    data.append({'id':x['model'],'name':str(x.get('displayName') or x['model']),'default':x.get('isDefault') is True,'efforts':[e['reasoningEffort'] for e in x.get('supportedReasoningEfforts',[]) if isinstance(e,dict) and isinstance(e.get('reasoningEffort'),str)],'default_effort':x.get('defaultReasoningEffort'),'vision':isinstance(x.get('inputModalities'),list) and 'image' in x['inputModalities']})
            cursor=res.get('nextCursor')
            if not cursor:break
            if identity>25:raise RuntimeError('Codex returned too many model pages.')
        if not data:raise RuntimeError('No Codex models are available for this account.')
        return data


def usage(account):
    with _Session(account,'jettpad_usage','Codex usage is unavailable.','Codex usage check timed out.') as session:
        session.handshake()
        session.send({'id':3,'method':'account/rateLimits/read','params':{}})
        return session.get(3)
