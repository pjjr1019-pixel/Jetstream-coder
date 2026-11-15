"""Local account profiles: no copying or reading login credentials."""
import json, os, subprocess, uuid
from pathlib import Path

def root():return Path(os.environ['LOCALAPPDATA'])/'JettPad/codex-accounts'
def list_accounts():
    try:
        data=json.loads((root()/'accounts.json').read_text(encoding='utf-8'))
        if not isinstance(data,list):return []
        return [x for x in data if isinstance(x,dict) and isinstance(x.get('name'),str) and len(x['name'])<=50 and _valid(x.get('id'))]
    except (OSError,ValueError,TypeError):return []
def _valid(value):
    try:return str(uuid.UUID(value))==value
    except (ValueError,TypeError,AttributeError):return False
def add_account(name):
    name=name.strip()
    if not name or len(name)>50:raise ValueError('Enter an account name of up to 50 characters.')
    accounts=list_accounts();index=root()/'accounts.json'
    if index.exists():
        try:
            current=json.loads(index.read_text(encoding='utf-8'))
            if not isinstance(current,list) or len(current)!=len(accounts):raise ValueError('Account list is invalid; it was not changed.')
        except (OSError,ValueError,TypeError) as error:raise ValueError('Account list is invalid; it was not changed.') from error
    identity=str(uuid.uuid4());accounts.append({'id':identity,'name':name});directory=root();directory.mkdir(parents=True,exist_ok=True)
    tmp=directory/('accounts.json.'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps(accounts,ensure_ascii=False),encoding='utf-8');os.replace(tmp,directory/'accounts.json')
    return identity
def home(identity):
    if not _valid(identity) or not any(x['id']==identity for x in list_accounts()):raise ValueError('Unknown Codex account')
    result=root()/identity;result.mkdir(parents=True,exist_ok=True);return result
def login(identity):
    from codex_model import codex_path
    executable=codex_path()
    if not executable:raise RuntimeError('Codex is not installed.')
    env=os.environ.copy();env['CODEX_HOME']=str(home(identity))
    subprocess.Popen([executable,'login','--device-auth'],env=env,creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',0))
