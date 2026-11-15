"""Apply the existing user-only data policy without spawning per-file tools."""
import os
from pathlib import Path


def secure_tree(folder):
    import win32api
    import win32con
    import win32security
    import ntsecuritycon
    folder=Path(folder).resolve()
    token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
    try:
        sid=win32security.GetTokenInformation(token,win32security.TokenUser)[0]
    finally:token.Close()
    def secure(path):
        info=path.lstat()
        if getattr(info,'st_file_attributes',0)&0x400:
            raise RuntimeError('Linked entries are not permitted in private Muse data')
        flags=(win32con.OBJECT_INHERIT_ACE|win32con.CONTAINER_INHERIT_ACE) if path.is_dir() else 0
        acl=win32security.ACL()
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,flags,ntsecuritycon.FILE_ALL_ACCESS,sid)
        win32security.SetNamedSecurityInfo(str(path),win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION|win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,None,acl,None)
    secure(folder)
    for current,dirs,files in os.walk(folder,followlinks=False):
        for name in dirs+files:
            try:secure(Path(current)/name)
            except FileNotFoundError:continue # Browser cache entries can expire.
