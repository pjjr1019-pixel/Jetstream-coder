"""LOCAL operator adapter only. Not a MuseBridge protocol operation."""
from pathlib import Path
import ctypes,hashlib,os,threading
from ctypes import wintypes
from .windows import creation_time,current_creation_time

SUNSHINE_EXE_SHA256='9aaefc1b7b421ec1db54569d7950bff75f10d6c4888ecab411f34351dbf5c7d2'
# The accepted publisher-only staging exception supports only the test component.
# Production vendor admission is a separate rollout gate, not implicitly enabled.
def expected_test_image():
    return Path.home()/'Desktop/JettPad Sunshine Test/vendor/Sunshine/sunshine.exe'

class VerifiedTermination:
    def __init__(self,pid:int,created:int):
        if type(pid) is not int or pid<=0 or type(created) is not int or created<=0:
            raise ValueError('Exact streaming process identity required')
        self._lock=threading.Lock();self.handle=None
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        k=self.kernel
        k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
        k.CloseHandle.argtypes=[wintypes.HANDLE];k.CloseHandle.restype=wintypes.BOOL
        k.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT];k.TerminateProcess.restype=wintypes.BOOL
        k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.WaitForSingleObject.restype=wintypes.DWORD
        k.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype=wintypes.BOOL
        handle=k.OpenProcess(0x100001|0x1000,False,pid)
        if not handle:raise ValueError('Cannot verify the suspended Sunshine process')
        try:
            size=wintypes.DWORD(32768);image=ctypes.create_unicode_buffer(size.value)
            if not k.QueryFullProcessImageNameW(handle,0,image,ctypes.byref(size)):raise ValueError('Cannot inspect streaming process')
            expected=expected_test_image().resolve()
            if Path(image.value).resolve()!=expected:raise ValueError('Only the pinned staged Sunshine process is permitted')
            if hashlib.sha256(expected.read_bytes()).hexdigest()!=SUNSHINE_EXE_SHA256:raise ValueError('Sunshine executable hash mismatch')
            if creation_time(handle)!=created or k.WaitForSingleObject(handle,0)!=258:raise ValueError('Streaming process identity changed or exited')
            self._same_user(handle)
            self.handle=handle
        except Exception:k.CloseHandle(handle);raise
    def _same_user(self,process):
        adv=ctypes.WinDLL('advapi32',use_last_error=True);k=self.kernel
        adv.OpenProcessToken.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)];adv.OpenProcessToken.restype=wintypes.BOOL
        adv.GetTokenInformation.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)];adv.GetTokenInformation.restype=wintypes.BOOL
        adv.EqualSid.argtypes=[wintypes.LPVOID,wintypes.LPVOID];adv.EqualSid.restype=wintypes.BOOL
        k.GetCurrentProcess.restype=wintypes.HANDLE
        buffers=[]
        for owner in (process,k.GetCurrentProcess()):
            token=wintypes.HANDLE()
            if not adv.OpenProcessToken(owner,8,ctypes.byref(token)):raise ValueError('Cannot verify streaming process owner')
            try:
                length=wintypes.DWORD();adv.GetTokenInformation(token,1,None,0,ctypes.byref(length))
                buffer=ctypes.create_string_buffer(length.value)
                if not adv.GetTokenInformation(token,1,buffer,length.value,ctypes.byref(length)):raise ValueError('Cannot read process owner')
                buffers.append(buffer)
            finally:k.CloseHandle(token)
        # TOKEN_USER begins with SID_AND_ATTRIBUTES; keep both backing buffers alive.
        sids=[ctypes.cast(buffer,ctypes.POINTER(wintypes.LPVOID))[0] for buffer in buffers]
        if not adv.EqualSid(sids[0],sids[1]):raise ValueError('Streaming process belongs to a different Windows user')
    def alive(self):
        with self._lock:return self.handle is not None and self.kernel.WaitForSingleObject(self.handle,0)==258
    def terminate_now(self):
        with self._lock:
            handle=self.handle;self.handle=None
        if handle:
            try:
                if self.kernel.WaitForSingleObject(handle,0)==258 and not self.kernel.TerminateProcess(handle,1):raise OSError('Cannot stop the verified streaming process')
            finally:self.kernel.CloseHandle(handle)

def status(bridge):
    previous=getattr(bridge,'_stream_termination',None)
    if previous is not None and not previous.alive():
        bridge.stream_stop.release_local();bridge._stream_termination=None
    result=bridge.stream_stop.status()
    result['owner_created']=current_creation_time() if result['available'] else None
    return result

def arm(bridge,value):
    if set(value)!={'action','epoch','pid','created','privacy_warning_accepted'} or value['privacy_warning_accepted'] is not True:
        raise ValueError('Explicit local privacy acknowledgement and exact process identity required')
    if type(value['epoch']) is not int:raise ValueError('Exact STOP epoch required')
    status(bridge)
    publisher_created=current_creation_time()
    verified=VerifiedTermination(value['pid'],value['created'])
    try:
        result=bridge.stream_stop.arm_local(expected_epoch=value['epoch'],terminate_verified_process=verified.terminate_now,local_action_verified=True)
    except Exception:verified.terminate_now();raise
    bridge._stream_termination=verified
    result['owner_created']=publisher_created
    return result
