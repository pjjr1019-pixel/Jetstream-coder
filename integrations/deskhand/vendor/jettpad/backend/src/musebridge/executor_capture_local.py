"""Staged checked access to an executor's completed screenshot result."""
import base64,hashlib,os
from pathlib import Path
from executor.protocol import identity

def read_capture(bridge,job_id,index):
    job_id=identity(job_id)
    if type(index) is not int or not 1<=index<=128:raise ValueError('Invalid screenshot step')
    bridge.permissions.check_session('desktop');bridge.desktop._ensure_authority('desktop_control');bridge.desktop._ensure_authority('screenshots')
    if not bridge.executor.session_check():raise ValueError('Interactive Windows session unavailable')
    row=bridge.executor.get(job_id,private=True)
    if not row or row.get('status')!='done':raise ValueError('Screenshot job is not complete')
    matches=[step for step in row.get('steps',[]) if step.get('step')==index and step.get('op')=='screenshot']
    if len(matches)!=1:raise ValueError('No completed screenshot at this step')
    result=matches[0].get('result') or {}
    if result.get('protected_sources_captured') is not False:raise ValueError('Screenshot protection evidence is unavailable')
    expected=bridge.executor.output/'out'/(job_id+'-'+str(index)+'.jpg')
    if str(result.get('path'))!=str(expected) or expected.is_symlink() or not expected.is_file():raise ValueError('Screenshot file is unavailable')
    if expected.stat().st_size>2*1024*1024:raise ValueError('Screenshot is too large')
    data=expected.read_bytes()
    if hashlib.sha256(data).hexdigest()!=result.get('sha256'):raise ValueError('Screenshot integrity check failed')
    bridge.permissions.check_session('desktop');bridge.desktop._ensure_authority('desktop_control');bridge.desktop._ensure_authority('screenshots')
    if not bridge.executor.session_check():raise ValueError('Interactive Windows session unavailable')
    return {'format':'jpeg','image':base64.b64encode(data).decode('ascii'),'sha256':result['sha256'],'width':result.get('width'),'height':result.get('height')}
