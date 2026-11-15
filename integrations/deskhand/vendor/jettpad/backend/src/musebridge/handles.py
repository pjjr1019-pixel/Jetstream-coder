"""Verify the file actually opened, rather than only its requested path."""
from __future__ import annotations
import ctypes
import os
from pathlib import Path


def authorize_open_file(stream, permissions, permission="read"):
    info = os.fstat(stream.fileno())
    if info.st_nlink > 1:
        raise ValueError("Hard-linked files require an explicit independent copy")
    if os.name != "nt":
        actual = Path(os.readlink(f"/proc/self/fd/{stream.fileno()}")) if Path("/proc/self/fd").exists() else Path(stream.name)
    else:
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        size = kernel.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            raise ValueError("Could not verify the opened file")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        actual = Path(value)
    return permissions.authorize(str(actual), permission)
