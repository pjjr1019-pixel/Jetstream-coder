"""Locally approved targets and immutable harnesses. No task-selected execution."""
import ast
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(path):
    path=Path(path);before=path.stat();raw=path.read_bytes();after=path.stat()
    if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):raise ValueError('Concurrent human edit while reading target')
    return {'sha256':hashlib.sha256(raw).hexdigest(),'mtime_ns':after.st_mtime_ns,'size':len(raw)}


class Conflict(ValueError):pass


class Approval:
    def __init__(self,path):self.path=Path(path)
    def resolve(self,task):
        value=json.loads(self.path.read_text(encoding='utf-8'))
        root=Path(task['project_root']).resolve(strict=True)
        projects=[p for p in value['projects'] if Path(p['root']).resolve()==root]
        if len(projects)!=1:raise ValueError('Project root has no local approval')
        rule=projects[0]
        targets=task['target_files']
        if not isinstance(targets,list) or len(targets)!=1:raise ValueError('Coding tasks must name exactly one approved target file')
        target=(root/targets[0]).resolve(strict=True)
        if not target.is_relative_to(root) or target.relative_to(root).as_posix() not in rule['target_files']:
            raise ValueError('Target is outside local approval')
        if target.stat().st_size>24000 or target.suffix!='.py':raise ValueError('Target must be one Python source file under 24000 bytes; split larger tasks')
        command=task['test_command']
        if not isinstance(command,list) or not command or any(not isinstance(s,str) or '\x00' in s for s in command) or command!=rule['test_command']:
            raise ValueError('test_command does not match the immutable locally approved argument array')
        executable=Path(command[0]).resolve(strict=True)
        if executable.suffix.lower()!='.exe' or executable.name.lower() not in ('python.exe','python3.exe'):
            raise ValueError('Only the locally approved isolated Python test harness is implemented')
        if command[1:2]!=['-I'] or len(command)!=3:raise ValueError('Harness must be Python -I approved_script.py, no inline commands or shell')
        harness=Path(command[2]).resolve(strict=True)
        if not harness.is_relative_to(root) or harness==target:raise ValueError('Harness must be separate from model targets inside the approved project')
        if digest(executable)!=rule['executable_sha256']:raise ValueError('Approved executable checksum changed')
        for name,expected in rule['harness_files'].items():
            file=(root/name).resolve(strict=True)
            if not file.is_relative_to(root) or file==target or digest(file)!=expected:raise ValueError('Approved harness/dependency checksum changed')
        if harness.relative_to(root).as_posix() not in rule['harness_files']:raise ValueError('Harness is not checksum-approved')
        return {'root':root,'target':target,'command':command,'rule':rule,
                'approval_sha256':digest(self.path)}


def checked_source(text,allowed_imports):
    tree=ast.parse(text)
    forbidden={'eval','exec','compile','__import__','getattr','setattr','globals','locals','vars','breakpoint','input','exit','quit','SystemExit'}
    banned_attributes={'system','popen','Popen','run','call','check_call','check_output','startfile','ShellExecuteW','WinExec',
                       'execv','execve','execvp','spawnv','spawnve','spawnvp','posix_spawn','fork','forkpty','load_module','exit','_exit'}
    for node in ast.walk(tree):
        if isinstance(node,(ast.Import,ast.ImportFrom)):
            modules=[n.name.split('.')[0] for n in node.names] if isinstance(node,ast.Import) else [(node.module or '').split('.')[0]]
            if any(module not in allowed_imports for module in modules):raise ValueError('Patch adds an unapproved import')
        if isinstance(node,ast.Name) and node.id in forbidden:raise ValueError('Dynamic execution/reflection is not allowed in crew patches')
        safe_exception_name=(isinstance(node,ast.Attribute) and node.attr=='__name__' and isinstance(node.value,ast.Call)
            and isinstance(node.value.func,ast.Name) and node.value.func.id=='type' and len(node.value.args)==1)
        if isinstance(node,ast.Attribute) and not safe_exception_name and (node.attr in banned_attributes or node.attr.startswith(('__','exec','spawn','posix_spawn'))):
            raise ValueError('Execution or introspection primitive is not allowed in crew patches')
    return text


def build_patch(original,answer,relative,allowed_imports):
    edits=answer.get('edits')
    if not isinstance(edits,list) or not 1<=len(edits)<=6:raise ValueError('Model must return one to six exact text edits')
    result=original
    for edit in edits:
        if edit.get('path')!=relative or not isinstance(edit.get('find'),str) or not edit['find'] or not isinstance(edit.get('replace'),str):
            raise ValueError('Edit must name only the approved target and an exact nonempty anchor')
        if result.count(edit['find'])!=1:raise ValueError('Patch anchor is absent or ambiguous')
        result=result.replace(edit['find'],edit['replace'],1)
    if len(result.encode())>24000:raise ValueError('Patched source exceeds bounded file size')
    if result==original:raise ValueError('Patch makes no source change')
    return checked_source(result,allowed_imports)


def apply_locked(path,text,expected):
    """Exclusive Windows write blocks editor saves during compare/commit; no blind overwrite."""
    path=Path(path)
    if os.name=='nt':
        from ctypes import wintypes as w
        import msvcrt
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE];k.CreateFileW.restype=w.HANDLE
        handle=k.CreateFileW(str(path),0x80000000|0x40000000,1,None,3,0x80,None)
        if handle==w.HANDLE(-1).value:raise Conflict('Concurrent human editor or target unavailable; exclusive commit refused')
        stream=os.fdopen(msvcrt.open_osfhandle(handle,os.O_RDWR|os.O_BINARY),'r+b')
    else:stream=path.open('r+b')
    with stream:
        if fingerprint(path)!=expected:raise Conflict('Concurrent human edit detected; target was not overwritten')
        stream.seek(0);stream.write(text.encode('utf-8'));stream.truncate();stream.flush();os.fsync(stream.fileno())
        info=os.fstat(stream.fileno())
        committed={'sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),'mtime_ns':info.st_mtime_ns,'size':len(text.encode('utf-8'))}
    if fingerprint(path)!=committed:raise Conflict('Concurrent human edit immediately after commit; further execution refused')
    return committed


def run_harness(approval,task,expected,log_path,timeout=60):
    resolved=approval.resolve(task)  # re-check checksums immediately before the sole execution path
    if fingerprint(resolved['target'])!=expected:raise Conflict('Concurrent human edit before tests; execution refused')
    # The command comes from local approval, never the model response.
    environment={key:os.environ[key] for key in ('SYSTEMROOT','WINDIR','TEMP','TMP','LOCALAPPDATA','USERPROFILE') if key in os.environ}
    started=time.monotonic();job=None
    with Path(log_path).open('wb') as output:
        process=subprocess.Popen(resolved['command'],cwd=resolved['root'],env=environment,stdin=subprocess.DEVNULL,
            stdout=output,stderr=subprocess.STDOUT,shell=False,
            creationflags=(subprocess.CREATE_NO_WINDOW|0x4) if os.name=='nt' else 0)
        try:
            if os.name=='nt':
                from musebridge.execution import _WindowsJob
                job=_WindowsJob(process);job.resume(process)
            try:code=process.wait(timeout=timeout);timed_out=False
            except subprocess.TimeoutExpired:
                timed_out=True
                if job:job.terminate()
                else:process.kill()
                process.wait(timeout=5);code=process.returncode
        finally:
            if job:job.close()
            if process.poll() is None:process.kill();process.wait()
    raw=Path(log_path).read_bytes()
    if len(raw)>8*1024*1024:raise ValueError('Harness output exceeded 8 MiB; full log retained, task needs review')
    return {'name':Path(task['test_command'][2]).name,'pass':code==0 and not timed_out,
        'output_tail':raw[-4000:].decode('utf-8',errors='replace')}, {'returncode':code,'timeout':timed_out,
        'wall_seconds':time.monotonic()-started,'log_path':str(log_path)}
