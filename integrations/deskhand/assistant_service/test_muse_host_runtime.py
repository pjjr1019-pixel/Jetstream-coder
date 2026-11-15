"""Test-only setup for a real owned bridge and a simulated private browser."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

from deskhand_muse_service import MuseService


def create_fixture(data):
    source = Path(__file__).resolve().parent.parent / 'vendor' / 'jettpad'
    sys.path.insert(0, str(source))
    import deskhand_bootstrap
    deskhand_bootstrap.setup()
    import headless_browser
    from musebridge import cli
    class FakeBrowser:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
        def stop(self): pass
        def status(self): return 'fixture'
    muse = MuseService(Path(data) / 'muse-runtime', source, sys.executable)
    muse.bridge.home.mkdir(parents=True, exist_ok=True)
    config = muse.bridge.home / 'musebridge.config.json'
    if not config.exists():
        config.write_text(json.dumps({'token': 'fixture-' + 'x' * 40}), encoding='utf-8')
    bridge_fixture = Path(__file__).resolve().parent / 'fixtures' / 'owned_bridge.py'
    muse.bridge._command = lambda *args: [sys.executable, '-B', str(bridge_fixture)]
    try:
        with patch.object(headless_browser, 'HeadlessBrowser', FakeBrowser), patch.object(cli, '_lock_windows_data'):
            muse.start()
            # This fixture has no browser/listener transport. Retain its real
            # host STOP gate while testing interruption of the waiting relay.
            from musebridge.host_stop import ensure_host_allows
            muse.relay.ensure_ready=ensure_host_allows
    finally:
        sys.path.remove(str(source))
    return muse
