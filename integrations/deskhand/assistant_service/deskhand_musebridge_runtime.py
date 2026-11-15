"""Owned lifecycle for the isolated, staged MuseBridge process.

This module does not start the bridge by import. Callers must hold the chat
service owner lock. STOP latch removed (Phil 2026-11-09): no STOP wiring
exists here, and nothing in this module sets or clears a STOP flag.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from deskhand_runtime import _make_kill_job


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class MuseBridgeRuntime:
    def __init__(self, root, source, python=None):
        self.root = Path(root).resolve()
        self.home = self.root / 'bridge'
        self.source = Path(source).resolve()
        self.python = str(python or sys.executable)
        self.process = None
        self.started_after = None
        self.close_job = None
        self.lock = threading.RLock()
        self.client = build_opener(ProxyHandler({}), _NoRedirect())

    def _environment(self):
        env = os.environ.copy()
        host_stop = env.get('DESKHAND_HOST_STOP_EVENT')
        for name in tuple(env):
            if name.startswith('DESKHAND_MUSE_') or name == 'DESKHAND_HOST_STOP_EVENT':
                env.pop(name, None)
        if host_stop:
            env['DESKHAND_HOST_STOP_EVENT'] = host_stop
        env['DESKHAND_JETT_RUNTIME'] = str(self.root)
        env['PYTHONNOUSERSITE'] = '1'
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        # Only read-only status and authenticated session setup are enabled.
        # Desktop action admission remains disabled in bridge startup.
        env['DESKHAND_MUSE_READONLY_GATEWAY'] = '1'
        env['DESKHAND_MUSE_SESSION_GATEWAY'] = '1'
        env['DESKHAND_MUSE_LOCAL_ADAPTER'] = '1'
        env['DESKHAND_MUSE_SHARED_ACTIONS'] = '1'
        for name in ('MUSEBRIDGE_TOKEN', 'PYTHONPATH', 'PYTHONHOME'):
            env.pop(name, None)
        return env

    def _command(self, *args):
        script = self.source / 'deskhand_bootstrap.py'
        if not script.is_file():
            raise RuntimeError('Deskhand MuseBridge source is missing')
        return [self.python, '-B', str(script), '--home', str(self.home), *args]

    def start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return self.state()
            if self.process is not None:
                self.close()
            self.root.mkdir(parents=True, exist_ok=True)
            env = self._environment()
            if not (self.home / 'musebridge.config.json').exists():
                result = subprocess.run(self._command('init'), env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode:
                    raise RuntimeError('Could not initialize Deskhand MuseBridge: ' + result.stderr[-500:])
            started_after = time.time()
            process = subprocess.Popen(self._command('run', '--no-window'), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.process = process
            self.started_after = started_after
            try:
                self.close_job = _make_kill_job(process)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('Deskhand MuseBridge exited before it was ready')
                    try:
                        return self.state()
                    except (OSError, ValueError, RuntimeError):
                        time.sleep(.1)
                raise RuntimeError('Deskhand MuseBridge did not become ready')
            except BaseException:
                self.close()
                raise

    def _request(self, action=None, payload=None, compact=True):
        process = self.process
        if process is None or process.poll() is not None:
            raise RuntimeError('Deskhand MuseBridge is not running')
        runtime = json.loads((self.home / 'runtime.json').read_text(encoding='utf-8'))
        port = runtime.get('operator_port')
        started = runtime.get('started_at')
        if (runtime.get('pid') != process.pid or type(port) is not int or not 1 <= port <= 65535 or
                self.started_after is None or type(started) not in (int, float) or started < self.started_after):
            raise RuntimeError('Deskhand MuseBridge ownership could not be verified')
        token = (self.home / 'operator.secret').read_text(encoding='ascii').strip()
        origin = f'http://127.0.0.1:{port}'
        body = json.dumps({'action': action, **(payload or {})}).encode() if action else None
        url = origin + ('/api/action' if action else ('/api/state?compact=1' if compact else '/api/state'))
        request = Request(url, data=body,
            headers={'Authorization': 'Bearer ' + token, 'Origin': origin, 'Content-Type': 'application/json'})
        try:
            with self.client.open(request, timeout=3) as response:
                return json.load(response)
        except HTTPError as error:
            try: message = json.load(error).get('error', error.reason)
            except (ValueError, AttributeError): message = error.reason
            raise RuntimeError('MuseBridge refused the request: ' + str(message)[:500]) from error
        except URLError as error:
            raise RuntimeError('Deskhand MuseBridge is not reachable') from error

    def state(self):
        with self.lock:
            return self._request()

    def full_state(self):
        """Bridge state including the audit trail (not compact).

        Used by the runtime activity panel: the audit carries the
        recent file/command operations (fs.* / exec.run) with their
        paths. Heavier than state() — only call on a poll cadence,
        never in a hot loop.
        """
        with self.lock:
            return self._request(compact=False)

    # B2-D (100.030): stop_all() was removed -- zero production
    # callers (its only callers were tests); the vendor bridge
    # stop_all action is unreachable from production, and the STOP
    # latch itself was removed in 100.012.
    def local_action(self, action, payload=None):
        allowed = {'configure', 'start', 'stop', 'desktop_control', 'session',
                   'revoke_day_authorization', 'connection_details',
                   'reconnect', 'ping_muse', 'export_audit'}
                   # NOTE (100.012, STOP removed Phil 2026-11-09):
                   # 'clear_stop' was removed from this allowlist with
                   # the in-flight latch recovery that was its only
                   # caller. It is not exposed as a service op, and no
                   # code in this service may clear a STOP flag.
        if action not in allowed:
            raise ValueError('Unsupported MuseBridge local setting')
        with self.lock:
            return self._request(action, payload)

    def close(self):
        with self.lock:
            process = self.process
            if process is not None and process.poll() is None:
                try: self._request('shutdown')
                except (OSError, ValueError, RuntimeError): pass
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if self.close_job is None: process.terminate()
            if self.close_job is not None:
                self.close_job()
            if process is not None and process.poll() is None:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.process, self.close_job, self.started_after = None, None, None
