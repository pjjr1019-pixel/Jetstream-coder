"""Staged Muse components. STOP latch removed (Phil 2026-11-09); no STOP gates."""
import os
import sys
import threading
import time
from pathlib import Path

from deskhand_muse_relay import MuseRelay
from deskhand_musebridge_runtime import MuseBridgeRuntime
from deskhand_reconnect_v2 import (
    ENSURE_RECONCILE_THROTTLE_SECS,
    ReconnectDiagnostics,
    mark_interrupted_uncertain,
)


class MuseService:
    def __init__(self, root, source, python=None, *, manage_connection=False,
                 in_flight=None, on_interrupted=None):
        self.root = Path(root).resolve()
        self.source = Path(source).resolve()
        self.bridge = MuseBridgeRuntime(self.root, self.source, python)
        self.mailbox = self.browser = self.relay = None
        self.delivery_ready = False
        self.lock = threading.RLock()
        self.connection=None
        self.manage_connection=manage_connection
        self._last_ensure_reconcile = 0.0
        # v2: in-flight guard for force-restarts (callable -> bool) and
        # interrupted-turn handler. Both are wired through from the
        # owning chat service; until the dispatcher guard is wired,
        # force-restarts are refused rather than guessed (fail closed).
        self._in_flight = in_flight
        self._on_interrupted = on_interrupted

    def start(self):
        with self.lock:
            if self.mailbox is not None:
                return self.bridge.state()
            existing = os.environ.get('DESKHAND_JETT_RUNTIME')
            if existing and Path(existing).resolve() != self.root:
                raise RuntimeError('A different Muse runtime is already bound to this process')
            os.environ['DESKHAND_JETT_RUNTIME'] = str(self.root)
            sys.path.insert(0, str(self.source))
            try:
                import deskhand_bootstrap
                deskhand_bootstrap.setup()
                from musebridge.cli import _lock_windows_data
                from browser_mailbox import BrowserMailbox
                from headless_browser import HeadlessBrowser
                pad = self.root / 'pad'
                inbox = self.root / 'documents' / 'jett-pad-inbox'
                self.root.mkdir(parents=True, exist_ok=True)
                _lock_windows_data(self.root)
                pad.mkdir(parents=True, exist_ok=True)
                inbox.mkdir(parents=True, exist_ok=True)
                self.mailbox = BrowserMailbox(pad)
                # Initial admissions stay held until the host has wired access
                # and the user enables it.
                self.mailbox.set_offline_hold(True)
                # The Deskhand queue already recovered interrupted turns as
                # unknown. Never send an old browser job when this hold clears.
                self.mailbox.abandon_queued_after_restart()
                # The dispatcher already holds exclusive ownership and has
                # marked interrupted outer turns unknown. Preserve their
                # browser records, but do not let them block distinct new work.
                self.mailbox.retire_interrupted_after_restart()
                self.browser = HeadlessBrowser(pad, delivery_enabled=True)
                self.relay = MuseRelay(self.mailbox, inbox, ensure_ready=self.ensure_chat_ready)
                state = self.bridge.start()
                self.browser.start()
                if self.manage_connection:
                    from deskhand_muse_connection import MuseConnection
                    from musebridge.host_stop import ensure_host_allows
                    self.connection=MuseConnection(self.bridge,ensure_host_allows,
                        in_flight=self._in_flight,
                        on_interrupted=self._on_interrupted)
                    if self._on_interrupted is None and hasattr(
                            self.mailbox, 'recover_interrupted'):
                        # Mark dispatching turns uncertain after a restart
                        # when the mailbox supports it; otherwise the
                        # connection records the restart as unmarked in
                        # diagnostics (the dispatcher entry path still
                        # recovers interrupted turns as unknown).
                        self.connection.set_interrupted_handler(
                            lambda reason: mark_interrupted_uncertain(
                                self.mailbox, reason))
                    self.connection.start()
                # STOP latch removed (Phil 2026-11-09): the host event is
                # created RESET and never set, so the vendor bridge never
                # trips STOP and there is no stale latch to recover.
                return state
            except BaseException:
                self.close()
                raise
            finally:
                if str(self.source) in sys.path:
                    sys.path.remove(str(self.source))

    def stop_all(self):
        # STOP latch removed (Phil 2026-11-09): stopping is removed,
        # not a no-op that reports success. This touches no mailbox,
        # relay, or bridge state and never raises: there is no latch
        # to stop or confirm. Callers must report 'not_enabled' /
        # stop_removed, never 'confirmed'.
        with self.lock:
            self.delivery_ready = False
            return {'stopped': False, 'stop_removed': True}

    def prepare_shutdown(self):
        """Drain the owned backend without converting app exit into emergency STOP."""
        with self.lock:
            self.delivery_ready = False
            if self.connection is not None:self.connection.stopping.set()
            try:
                if self.mailbox is not None:self.mailbox.set_offline_hold(True)
                if self.relay is not None:self.relay.request_stop()
            finally:
                try:self.bridge.close()
                finally:
                    if self.connection is not None:self.connection.close()

    def status(self):
        with self.lock:
            if self.browser is None:
                raise RuntimeError('Muse is not running')
            return {'browser': self.browser.status(), 'bridge': self.bridge.state(),
                    'cloud_ready': getattr(self.browser, 'cloud_ready', False) is True,
                    'delivery_ready': self.delivery_ready is True,
                    'https_connection':self.connection.status() if self.connection is not None else None}

    def recent_file_activity(self, limit=8):
        """Recent file/command operations from the vendor bridge audit.

        The bridge audits every fs.* / exec.run operation with its path
        (fs.read, fs.write, fs.list, fs.delete, fs.mkdir, exec.run with
        its working directory). Returns the most recent entries,
        most-recent-first, as {operation, path, ok, ts} dicts, up to
        `limit`. Empty when the bridge is unavailable or has no such
        operations — the panel then simply shows no files. Never raises:
        a failed audit read must not break the activity panel.
        """
        try:
            state = self.bridge.full_state()
        except Exception:
            return []
        audit = state.get('audit') if isinstance(state, dict) else None
        if not isinstance(audit, list):
            return []
        out = []
        for row in audit:
            if not isinstance(row, dict):
                continue
            op = row.get('operation') or ''
            if not (op.startswith('fs.') or op == 'exec.run'):
                continue
            out.append({'operation': op, 'path': row.get('target') or '',
                        'ok': row.get('ok'), 'ts': row.get('ts')})
            if len(out) >= limit:
                break
        return out

    def prepare_host_tools(self):
        """Prepare text/receipt transport only; native bridge actions stay blocked."""
        with self.lock:
            if self.browser is None or not getattr(self.browser, 'cloud_ready', False):
                raise RuntimeError('Needs sign-in: open Muse sign-in and finish signing in to the bound Deskhand chat')
            state = self.bridge.state()
            if state.get('shared_actions_host_only') is not True:
                raise RuntimeError('Muse bridge must be restarted with host-only action routing')
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            from musebridge.host_stop import ensure_host_allows
            ensure_host_allows()
            # STOP latch removed (Phil 2026-11-09): no latch is ever
            # cleared here. The bridge state is observed only; a
            # stopped bridge reports not-ready until it is enabled
            # locally. The reconcile below is what heals the funnel
            # route -- it never touches any STOP flag.
            if self.connection is None:
                raise RuntimeError('Muse HTTPS connection is unavailable')
            try:
                self.connection.reconcile()
            except Exception:
                # A transient funnel-ownership mismatch (the route changed
                # under us) must not abort the whole 25s enable attempt.
                # The background reconcile loop re-derives the route, so
                # retry within the window instead of failing outright.
                # This cannot mask a persistent failure: the loop still
                # ends at the 25s deadline with 'not ready'.
                time.sleep(.2)
                continue
            if self.connection.status()['ready']:
                self.ensure_chat_ready()
                return {'ready': True}
            time.sleep(.2)
        raise RuntimeError('Muse HTTPS connection is not ready')

    def _connection_reconcile_failed(self):
        """True when the last recorded reconcile failed (v2, GPT gap 3).

        The connection records every reconcile outcome in diagnostics
        (monotonic timestamp, ok flag, error). A failed last reconcile
        means the connection is not ready even if status() still shows
        a stale ready flag. Returns False when there is no connection,
        no diagnostics (e.g. a test double), or no reconcile has been
        recorded / the last one succeeded.
        """
        conn = self.connection
        if conn is None:
            return False
        diag = getattr(conn, 'diagnostics', None)
        if not isinstance(diag, ReconnectDiagnostics):
            return False
        return diag.snapshot()['last_reconcile_ok'] is False

    def ensure_chat_ready(self):
        """Hold browser delivery if local authority or the session fails."""
        with self.lock:
            reason = 'Muse connection is not ready; check DeskHand access.'
            try:
                from musebridge.host_stop import ensure_host_allows
                ensure_host_allows()
                state = self.bridge.state()
                if self.connection is not None:
                    # H4 (100.012): never trust a stale status()['ready'].
                    # Run one on-demand reconcile on EVERY call --
                    # throttled to at most once per 2s (v2:
                    # ENSURE_RECONCILE_THROTTLE_SECS, a tunable
                    # starting value, not a load-tested constant),
                    # since the relay calls this in its wait loop and
                    # must not hammer the funnel. A route that died
                    # since the last 5s background tick must not slip
                    # a turn through on a stale ready flag.
                    # This method holds self.lock (an RLock)
                    # throughout, so concurrent callers serialize here
                    # instead of overlapping -- but prepare_host_tools
                    # can still call reconcile() while the background
                    # loop does, so overlapping reconciles are
                    # prevented by the connection's single-flight
                    # guard, not by this lock. Failures are recorded
                    # in the connection diagnostics (from the
                    # reconcile's own ok flag, never from a stale
                    # ready flag) and consulted below: a failed
                    # reconcile NEVER lets a stale ready flag through.
                    # The background loop retries.
                    now = time.monotonic()
                    if now - self._last_ensure_reconcile >= ENSURE_RECONCILE_THROTTLE_SECS:
                        self._last_ensure_reconcile = now
                        try:
                            self.connection.reconcile()
                        except Exception:
                            pass
                    if self._connection_reconcile_failed():
                        reason='Muse connection is not ready: last reconcile failed.'
                        raise RuntimeError(reason)
                    if not self.connection.status()['ready']:
                        reason='Muse connection is not ready: HTTPS endpoint is connecting.'
                        raise RuntimeError(reason)
                if not state.get('enabled'):
                    reason = 'Muse connection is not ready: the bridge listener is stopped.'
                    raise RuntimeError(reason)
                # The first message can bootstrap Muse's episodic client: it
                # opens its own short-lived session before any action or
                # Interrupted jobs remain uncertain and are never replayed.
                if not self.delivery_ready:
                    self.mailbox.set_offline_hold(False)
                ensure_host_allows()
                state = self.bridge.state()
                if not state.get('enabled'):
                    reason = 'Muse connection is not ready: the bridge listener stopped.'
                    raise RuntimeError('Muse connection is not ready')
                self.delivery_ready = True
            except Exception:
                self.delivery_ready = False
                if self.mailbox is not None:
                    self.mailbox.set_offline_hold(True)
                raise RuntimeError(reason) from None

    def resume_local(self):
        """Release delivery after the host allows the session."""
        with self.lock:
            if self.mailbox is None:
                raise RuntimeError('Muse is not running')
            from musebridge.host_stop import ensure_host_allows
            ensure_host_allows()
            state = self.bridge.state()
            if not state.get('enabled') or not state.get('connected'):
                raise RuntimeError('Connect MuseBridge before resuming Muse')
            ensure_host_allows()
            state = self.bridge.state()
            if not state.get('enabled') or not state.get('connected'):
                raise RuntimeError('MuseBridge access is not ready')
            self.mailbox.set_offline_hold(False)
            try:
                ensure_host_allows()
            except ValueError:
                self.mailbox.set_offline_hold(True)
                raise
            self.delivery_ready = True
            return {'status': 'Muse delivery resumed.'}

    def sign_in(self):
        with self.lock:
            if self.browser is None: raise RuntimeError('Muse is not running')
            self.browser.sign_in()

    def finish_sign_in(self):
        with self.lock:
            if self.browser is None: raise RuntimeError('Muse is not running')
            self.browser.finish_sign_in()

    def close(self):
        with self.lock:
            browser, mailbox = self.browser, self.mailbox
            self.browser = self.mailbox = self.relay = None
            self.delivery_ready = False
            try:
                if browser is not None: browser.stop()
            finally:
                try: self.bridge.close()
                finally:
                    try:
                        if self.connection is not None:self.connection.close()
                    finally:
                        if mailbox is not None: mailbox.close()
