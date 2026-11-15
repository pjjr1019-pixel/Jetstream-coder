"""Load staged bridge dependencies without importing JettPad's installation."""
import os
import sys
from pathlib import Path

_dll_handles = []


def setup():
    source = Path(__file__).resolve().parent
    packages = source.parent / "jettpad-dependencies" / "site-packages"
    if not packages.is_dir():
        raise RuntimeError("Deskhand MuseBridge dependencies are missing")
    for path in (source, source / "backend" / "src", packages,
                 packages / "win32", packages / "win32" / "lib", packages / "pythonwin"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    if os.name == "nt":
        # pywin32_system32 holds pywin32's DLLs (pywintypes312.dll,
        # pythoncom312.dll). It is absent from a stripped staging copy, and
        # os.add_dll_directory() raises FileNotFoundError on a missing
        # directory, so only register it when it exists. Skipping it here
        # leaves pywin32 imports to fail later with their own ImportError
        # instead of killing setup() before any bridge import runs.
        dll_dir = packages / "pywin32_system32"
        if dll_dir.is_dir():
            _dll_handles.append(os.add_dll_directory(str(dll_dir)))


def main():
    value = os.environ.get("DESKHAND_JETT_RUNTIME")
    if not value or not Path(value).is_absolute():
        raise RuntimeError("An absolute Deskhand MuseBridge runtime path is required")
    # Never inherit the installed JettPad's override credential.
    os.environ.pop("MUSEBRIDGE_TOKEN", None)
    setup()
    from musebridge.cli import main as bridge_main
    return bridge_main()


if __name__ == "__main__":
    raise SystemExit(main())
