"""Single per-user guardian. No cloud supervision and no independent queue consumer."""
from pathlib import Path
import json,os,subprocess,time
from companion_data import home,atomic

DELAYS=(1,2,5,10,30)
def run():
    root=Path(__file__).resolve().parent;data=home();delay=0
    from pad_core import InstanceGuard
    guard=InstanceGuard(data/'away-guardian')
    if not guard.acquire():return
    try:
        while True:
            try:policy=json.loads((data/'guardian-policy.json').read_text())
            except (OSError,ValueError):return
            if policy.get('explicit_quit') is True or policy.get('enabled') is not True:return
            from app_runtime import lock_held
            if lock_held(data/'instance.lock'):
                delay=0;time.sleep(2);continue
            try:
                # Let Pad own its existing bridge/browser/controller services.
                process=subprocess.Popen([str(root/'backend/.venv/Scripts/pythonw.exe'),str(root/'jett_pad.py'),'--background'],cwd=root,
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000)
                began=time.monotonic();process.wait()
                if time.monotonic()-began>60:delay=0
            except OSError:pass
            time.sleep(DELAYS[min(delay,len(DELAYS)-1)]);delay+=1
    finally:guard.close()

def start_guardian(root,*,explicit_start=False):
    path=home()/'guardian-policy.json'
    try:policy=json.loads(path.read_text())
    except (OSError,ValueError):policy={}
    if not policy.get('enabled'):return
    if policy.get('explicit_quit') and not explicit_start:return
    if explicit_start:atomic(path,dict(policy,explicit_quit=False))
    from app_runtime import lock_held
    if not lock_held(home()/'away-guardian/instance.lock'):
        subprocess.Popen([str(root/'backend/.venv/Scripts/pythonw.exe'),str(root/'away_guardian.py')],cwd=root,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000)

def configure_sign_in(root,enabled):
    """Called only by the local Away checkbox; owns one specific Startup link."""
    import win32com.client
    folder=Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/Startup'
    path=folder/'Jett Pad Away Guardian.lnk'
    shell=win32com.client.Dispatch('WScript.Shell')
    if path.exists():
        previous=shell.CreateShortcut(str(path))
        if Path(previous.TargetPath).resolve()!=Path(root/'backend/.venv/Scripts/pythonw.exe').resolve() or 'away_guardian.py' not in previous.Arguments:
            raise ValueError('Existing Startup shortcut belongs to another program')
    if enabled:
        link=shell.CreateShortcut(str(path));link.TargetPath=str(root/'backend/.venv/Scripts/pythonw.exe')
        link.Arguments='"'+str(root/'away_guardian.py')+'"';link.WorkingDirectory=str(root);link.WindowStyle=7;link.Save()
    elif path.exists():path.unlink()

if __name__=='__main__':run()
