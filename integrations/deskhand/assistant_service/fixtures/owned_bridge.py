"""Subprocess fixture: real local operator with desktop hooks disabled."""
import os
import sys
import asyncio
import uuid
from pathlib import Path

source = Path(__file__).resolve().parents[2] / 'vendor' / 'jettpad'
sys.path.insert(0, str(source))
import deskhand_bootstrap
deskhand_bootstrap.setup()
from musebridge import cli
from musebridge.operator import OperatorServer
from musebridge.host_stop import ensure_host_allows

original_action = OperatorServer._perform_action

async def fixture_action(self, value):
    if value['action'] == 'fixture_begin_executor':
        ensure_host_allows()
        class FixtureSocket:
            async def close(self, *args): pass
        bridge = self.bridge
        bridge.config['enabled'] = True
        bridge.config['stop_latched'] = False
        bridge.config['features']['desktop_control'] = True
        bridge.desktop.connected = True
        bridge.desktop.enabled = True
        bridge.client = FixtureSocket()
        bridge.executor.session_check = lambda: True
        job = await asyncio.to_thread(bridge.executor.submit, str(uuid.uuid4()),
            {'title': 'STOP fixture wait', 'timeout_secs': 60, 'steps': [{'op': 'wait', 'seconds': 30}]})
        self.fixture_job = job['id']
        return job
    if value['action'] == 'fixture_executor_status':
        return self.bridge.executor.get(self.fixture_job)
    if value['action'] == 'fixture_begin_wait':
        ensure_host_allows()
        async def work():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.fixture_result = 'cancelled'
                raise
        self.fixture_result = 'running'
        task = asyncio.create_task(work())
        self.bridge._request_tasks.add(task)
        task.add_done_callback(self.bridge._request_tasks.discard)
        return {'status': self.fixture_result}
    if value['action'] == 'fixture_wait_status':
        return {'status': getattr(self, 'fixture_result', 'missing')}
    return await original_action(self, value)

OperatorServer._perform_action = fixture_action


class NoDesktopHooks:
    available = True
    session_watch_available = True
    def __init__(self, *args): pass
    def start(self): pass
    def stop(self): pass


cli.DesktopGuards = NoDesktopHooks
cli._lock_windows_data = lambda home: None
raise SystemExit(cli.main(['--home', str(Path(os.environ['DESKHAND_JETT_RUNTIME']) / 'bridge'), 'run', '--no-window']))
