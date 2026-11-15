"""Own only DeskHand's dedicated Tailscale HTTPS path, never the root route.

This component is not enabled by import. The caller must verify STOP and
explicitly start it only for an owned, loopback-only gateway.
"""
import json
import os
import subprocess
from pathlib import Path


class MuseFunnel:
    PATH = '/deskhand-prototype'

    def __init__(self, executable=None, runner=subprocess.run):
        self.executable = str(executable or (Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Tailscale' / 'tailscale.exe'))
        self.runner = runner
        self.owned = None

    def _run(self, *args):
        result = self.runner([self.executable, *args], capture_output=True, text=True,
            timeout=15, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.stdout

    def _snapshot(self):
        value = json.loads(self._run('serve', 'status', '--json') or '{}')
        web = value.get('Web', {})
        if not isinstance(web, dict):
            raise RuntimeError('Tailscale route status is invalid')
        matches, roots = [], {}
        for host, settings in web.items():
            handlers = settings.get('Handlers', {}) if isinstance(settings, dict) else {}
            if not isinstance(handlers, dict):
                raise RuntimeError('Tailscale route status is invalid')
            if '/' in handlers:
                roots[host] = handlers['/']
            if self.PATH in handlers:
                matches.append((host, handlers[self.PATH]))
        if len(matches) > 1:
            raise RuntimeError('DeskHand HTTPS path exists on multiple hosts')
        return value, matches[0] if matches else None, roots

    def start(self, port):
        if type(port) is not int or not 1 <= port <= 65535 or self.owned is not None:
            raise ValueError('Invalid or already owned Muse gateway')
        _, existing, original_roots = self._snapshot()
        if existing is not None:
            raise RuntimeError('DeskHand HTTPS path is already in use')
        proxy = f'http://127.0.0.1:{port}'
        try:
            self._run('funnel', '--bg', '--yes', '--set-path', self.PATH, proxy)
            status, current, roots = self._snapshot()
            if (current is None or current[1] != {'Proxy': proxy} or roots != original_roots
                    or status.get('AllowFunnel', {}).get(current[0]) is not True):
                raise RuntimeError('DeskHand HTTPS route could not be confirmed')
            self.owned = current
            return current[0]
        except Exception:
            # Only remove the exact path if it still points to our loopback
            # port. Never reset Tailscale or change another path.
            try:
                _, current, _ = self._snapshot()
                if current is not None and current[1] == {'Proxy': proxy}:
                    self._run('funnel', '--https=443', '--set-path', self.PATH, 'off')
            except Exception:
                raise RuntimeError('DeskHand HTTPS route cleanup is unconfirmed') from None
            raise

    def close(self):
        if self.owned is None:
            return
        _, current, _ = self._snapshot()
        if current is None:
            self.owned = None
            return
        if current != self.owned:
            raise RuntimeError('DeskHand HTTPS route ownership changed')
        self._run('funnel', '--https=443', '--set-path', self.PATH, 'off')
        _, remaining, _ = self._snapshot()
        if remaining is not None:
            raise RuntimeError('DeskHand HTTPS route shutdown is unconfirmed')
        self.owned = None
