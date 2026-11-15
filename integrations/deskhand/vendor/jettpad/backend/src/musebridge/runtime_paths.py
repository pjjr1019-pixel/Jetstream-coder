"""Optional isolated storage roots for the Deskhand-owned bridge copy."""
import os
import sys
from pathlib import Path

DESKHAND_MUSE_CHAT = 'https://muse.ai/thread/a68df4eb-1e66-4b85-bc01-863d12f103d4'


def root():
    value = os.environ.get("DESKHAND_JETT_RUNTIME")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("DESKHAND_JETT_RUNTIME must be an absolute path")
    return path


def documents():
    base = root()
    return base / "documents" if base else Path.home() / "Documents"


def pad_data():
    base = root()
    return base / "pad" if base else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JettPad"


def orb_data():
    base = root()
    return base / "orb" if base else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JettOrb"


def muse_chat_url():
    if root() is None:
        return None
    url = os.environ.get('DESKHAND_MUSE_CHAT_URL', DESKHAND_MUSE_CHAT)
    if url != DESKHAND_MUSE_CHAT:
        raise ValueError('Deskhand Muse chat must match the selected Deskhand side conversation')
    return url


def helper_command(module, *arguments):
    """Start copied helpers with their isolated roots, even with a scrubbed env."""
    base = root()
    if base is None:
        return [sys.executable, '-B', '-m', module, *arguments]
    source = Path(__file__).resolve().parents[3]
    script = ("import os,sys,runpy;os.environ['DESKHAND_JETT_RUNTIME']=sys.argv.pop(1);"
              "os.environ['DESKHAND_HOST_STOP_EVENT']=sys.argv.pop(1);"
              "sys.path.insert(0,sys.argv.pop(1));import deskhand_bootstrap;"
              "deskhand_bootstrap.setup();runpy.run_module(sys.argv.pop(1),run_name='__main__')")
    return [sys.executable, '-B', '-u', '-c', script, str(base), os.environ.get('DESKHAND_HOST_STOP_EVENT', ''), str(source), module, *arguments]
