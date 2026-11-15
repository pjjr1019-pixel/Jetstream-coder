"""App-owned HTTPS endpoint; Muse opens a fresh authenticated lease per task."""
import json
import socket
import threading
import time
from pathlib import Path

from deskhand_muse_funnel import MuseFunnel
from deskhand_reconnect_v2 import (
    ENSURE_RECONCILE_THROTTLE_SECS,  # noqa: F401  (re-exported for callers)
    FORCE_RESTART_COOLDOWN_SECS,
    RECONCILE_SINGLE_FLIGHT_WAIT_SECS,
    WEDGED_FAILURE_THRESHOLD,
    ReconnectDiagnostics,
    SingleFlight,
)


class MuseConnection:
    # A live bridge process whose operator endpoint stops answering is
    # wedged: reconcile() never restarts a live process, so without a
    # threshold it would stay down forever. Three consecutive failed
    # state() reads (~15s at the 5s tick) is enough to rule out a
    # transient blip without restarting on a single hiccup.
    # v2: these are TUNABLE STARTING VALUES, not proven-safe constants
    # (see deskhand_reconnect_v2). A slow/overloaded bridge can take
    # longer than this to answer; validate against a loaded live bridge.
    WEDGED_FAILURE_THRESHOLD = WEDGED_FAILURE_THRESHOLD
    # At most one force-restart per minute, so a persistently wedged
    # bridge degrades to a restart a minute instead of a restart loop.
    FORCE_RESTART_COOLDOWN_SECS = FORCE_RESTART_COOLDOWN_SECS

    def __init__(self, bridge, allowed, route=None, *, in_flight=None,
                 on_interrupted=None):
        self.bridge,self.allowed=bridge,allowed
        self.route=route or MuseFunnel()
        self.record=Path(bridge.home)/'https-route.json'
        self.stopping=threading.Event()
        self.thread=None
        self.port=None
        self.error=None
        self.hostname=None
        self._wedged_failures=0
        self._last_force_restart=0.0
        # v2: `in_flight` is a callable returning True while a turn is
        # dispatching. A force-restart is REFUSED when no guard is wired
        # (fail closed) and DEFERRED while a turn is dispatching, so a
        # restart can never kill the bridge under live work. Wiring the
        # real dispatcher guard is still open; see the Round 2 v2 notes.
        self._in_flight = in_flight
        # v2: `on_interrupted(reason)` marks dispatching turns uncertain
        # after a restart (never replays them). May be None; a restart
        # with no handler is recorded in diagnostics, not hidden.
        self._on_interrupted = on_interrupted
        self.diagnostics = ReconnectDiagnostics()
        self._reconcile_flight = SingleFlight()
        # B2-D B-03: True only when the bridge itself reports not
        # enabled / no gateway port (paused), as opposed to an unknown
        # failure. Surfaced in status() so a paused bridge reads as
        # "paused" instead of looking like a missing funnel route
        # (gateway 404). The funnel route itself cannot answer while
        # there is no port to point it at -- see _reconcile_once.
        self.paused = False

    def set_interrupted_handler(self, handler):
        """Wire the callback that marks dispatching turns uncertain."""
        self._on_interrupted = handler

    def _recover(self):
        """Adopt or repair the saved https-route.json record.

        B2-D B-04: the old code raised forever, every 5 s, for a
        stale/corrupt record or a still-serving old endpoint, and the
        connection could never become ready until someone hand-deleted
        the file. Now every failure mode heals or is forgotten:
          * unparseable/unreadable record -> quarantined aside, fresh
            reconcile proceeds (a fresh route is created below);
          * invalid port -> record forgotten, fresh reconcile proceeds;
          * live route no longer matches the record (ownership
            changed / stale record) -> record forgotten and the live
            route adopted from the snapshot, so the reconcile below
            can verify it against the live bridge (same port ->
            ready; different port -> remove + restart) instead of
            bricking on route.start()'s already-in-use refusal;
          * previous endpoint still serving on the recorded port ->
            ADOPTED (route + port taken over) instead of refused: the
            bridge state check that follows still verifies the port
            matches the live bridge, and a mismatch removes + restarts
            the route, so a foreign listener on a reused port cannot
            be kept.
        A missing live route still just forgets the record (as before).
        """
        if not self.record.exists():
            return
        try:
            saved = json.loads(self.record.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            self._quarantine_record('unparseable')
            self._adopt_live_route()
            return
        if not isinstance(saved, dict):
            self._quarantine_record('invalid')
            self._adopt_live_route()
            return
        port = saved.get('port')
        if type(port) is not int or not 1 <= port <= 65535:
            self._quarantine_record('invalid-port')
            self._adopt_live_route()
            return
        _, current, _ = self.route._snapshot()
        expected = (saved.get('host'), {'Proxy': f'http://127.0.0.1:{port}'})
        if current is None:
            self.record.unlink()
            return
        if current != expected:
            self.record.unlink()
            self._adopt_live_route()
            return
        with socket.socket() as probe:
            probe.settimeout(.2)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                # Previous endpoint still serving: adopt it. The
                # reconcile below verifies the bridge still reports
                # this port before reporting ready.
                self.route.owned = current
                self.port, self.hostname = port, saved.get('host')
                return
        self.route.owned = current
        self.route.close()
        self.record.unlink()

    def _adopt_live_route(self):
        """Best-effort adoption of the live /deskhand-prototype route
        from the Tailscale snapshot (host + port parsed from its Proxy
        handler). Used when the saved record is unusable, so the
        reconcile below can verify the route against the live bridge
        instead of failing on an already-in-use path. A route whose
        handler does not parse as our loopback proxy is left alone
        (no adoption, no removal): reconcile then reports not-ready
        rather than stealing a route it cannot identify."""
        try:
            _, current, _ = self.route._snapshot()
        except Exception:
            return
        if current is None:
            return
        host, handler = current
        proxy = handler.get('Proxy') if isinstance(handler, dict) else None
        if not isinstance(proxy, str) or not proxy.startswith('http://127.0.0.1:'):
            return
        try:
            port = int(proxy.rsplit(':', 1)[1])
        except ValueError:
            return
        if not 1 <= port <= 65535:
            return
        self.route.owned = current
        self.port, self.hostname = port, host

    def _quarantine_record(self, reason):
        """Move a broken https-route.json aside (never delete evidence
        of what it held) so reconcile can proceed fresh."""
        try:
            target = self.record.with_name(
                "https-route.json.%s-%d.corrupt"
                % (reason, int(time.time())))
            self.record.replace(target)
        except OSError:
            try:
                self.record.unlink()
            except OSError:
                pass

    def _force_restart_wedged_bridge(self):
        """Force-restart a live-but-unresponsive bridge process.

        v2 (GPT gap 2): the restart is coordinated with in-flight work.
        It is REFUSED when no in-flight guard is wired (fail closed),
        DEFERRED while a turn is dispatching, and the guard is re-checked
        immediately before the kill, so a turn that started between the
        threshold check and the kill still blocks it. After the restart,
        the interrupted handler (when wired) marks any dispatching turns
        uncertain; a restart with no handler is recorded in diagnostics.
        Never replays work: a restart only re-creates the listener; no
        request is sent again. Raises while cooling down so the caller
        reports not-ready instead of hammering restarts.
        """
        now = time.monotonic()
        if now - self._last_force_restart < self.FORCE_RESTART_COOLDOWN_SECS:
            raise RuntimeError('Deskhand MuseBridge is not responding; force-restart is cooling down')
        if self._in_flight is None:
            self.diagnostics.record_restart_refused_no_guard()
            raise RuntimeError('Deskhand MuseBridge is not responding; force-restart refused: no in-flight guard wired')
        if self._in_flight():
            self.diagnostics.record_restart_deferred('turn dispatching')
            raise RuntimeError('Deskhand MuseBridge is not responding; force-restart deferred: turn dispatching')
        self._last_force_restart = now
        # Re-check immediately before the kill: a turn may have started
        # since the threshold check above.
        if self._in_flight():
            self.diagnostics.record_restart_deferred('turn dispatching')
            raise RuntimeError('Deskhand MuseBridge is not responding; force-restart deferred: turn dispatching')
        self.bridge.close()
        if self.stopping.is_set():
            raise RuntimeError('Muse connection is shutting down')
        state = self.bridge.start()
        self.diagnostics.record_force_restart('wedged live bridge')
        self._mark_interrupted('force-restart of wedged bridge')
        return state

    def _mark_interrupted(self, reason):
        """Mark dispatching turns uncertain after a restart, if wired."""
        if self._on_interrupted is None:
            self.diagnostics.record_restart_without_interrupted_handler()
            return
        try:
            marked = self._on_interrupted(reason)
        except Exception:
            self.diagnostics.record_restart_without_interrupted_handler()
            return
        if isinstance(marked, int) and marked >= 0:
            self.diagnostics.record_interrupted(marked)
        else:
            # Handler could not mark (-1 / unexpected): record it as
            # unmarked, never silently pretend the turns were recorded.
            self.diagnostics.record_restart_without_interrupted_handler()

    def reconcile(self):
        """Single-flight reconcile; concurrent callers join, never overlap.

        v2 (GPT gap 3): the background 5s loop, prepare_host_tools and
        ensure_chat_ready can all call reconcile() concurrently. The
        SingleFlight guard runs the body once; a concurrent caller waits
        for it and receives the same outcome. Only the runner records
        diagnostics, so one execution is one record. Failures are
        recorded in diagnostics, never swallowed into a ready verdict.
        Raises the body's exception when the reconcile failed.
        """
        ran, ok, value = self._reconcile_flight.run(self._reconcile_once)
        if ran:
            # H4 (100.012): derive the recorded outcome from the
            # reconcile's own ok flag, never from status()['ready'].
            # A failed reconcile (ok False) used to record success
            # whenever the stale port/error still looked ready -- the
            # exact live gateway-404 case (route deleted externally).
            # ok is necessary but not sufficient: a reconcile that ran
            # cleanly but left the endpoint not-ready (bridge paused)
            # is also not a success.
            if not ok:
                self.diagnostics.record_reconcile(False, str(value))
            else:
                ready = self.status()['ready']
                error = None if ready else (
                    getattr(self, '_last_reconcile_detail', None) or self.error)
                self.diagnostics.record_reconcile(ready, error)
        if not ok:
            raise value

    def _reconcile_once(self):
        self._recover() if self.port is None and self.route.owned is None else None
        # B2-D B-03: True only when the bridge itself was read and
        # reports not-enabled / no port (paused). A state-read failure
        # leaves this False -- the cause is unknown, not a pause.
        bridge_paused = False
        try:
            self.allowed()
            try:
                state=self.bridge.state()
                self._wedged_failures=0
                # STOP latch removed (Phil 2026-11-09): the bridge
                # state is observed only. No STOP flag is read,
                # recorded, or cleared anywhere in this module.
            except RuntimeError:
                process=self.bridge.process
                if self.stopping.is_set() or process is None:
                    raise
                if process.poll() is not None:
                    # Only restart an owned process that actually exited. A
                    # deliberately stopped listener remains stopped, and no old
                    # work is submitted again.
                    state=self.bridge.start()
                    if self.stopping.is_set():
                        self.bridge.close()
                        raise RuntimeError('Muse connection is shutting down')
                    self._wedged_failures=0
                else:
                    # Live process, dead endpoint: wedged. Count
                    # consecutive failures; force-restart only after the
                    # threshold, and at most once per cooldown window.
                    # A single failed read is a blip, not a restart.
                    self._wedged_failures+=1
                    if self._wedged_failures>=self.WEDGED_FAILURE_THRESHOLD:
                        state=self._force_restart_wedged_bridge()
                        self._wedged_failures=0
                    else:
                        raise
            port=state.get('readonly_gateway_port')
            ready=state.get('enabled') and type(port) is int
            bridge_paused = not ready
        except Exception as exc:
            # v2: keep the underlying failure for diagnostics; the
            # generic self.error below is what status() reports, but
            # diagnostics must name the real cause.
            self._last_reconcile_detail = str(exc)
            ready=False
        if not ready:
            self._remove()
            # B2-D B-03: a paused bridge is reported as paused (with a
            # distinct status flag -- see status()), not as a missing
            # funnel route. The funnel route only exists while the
            # bridge reports enabled, so while paused the gateway
            # still 404s at the Tailscale layer -- no route can answer
            # "paused" without a port to point at -- but callers and
            # the panel can now tell "paused" apart from "route
            # deleted / never configured" from status() instead of
            # guessing from a 404.
            self.paused = bridge_paused
            self.error = ('Muse is paused by access settings or backend availability.'
                          if bridge_paused else
                          'Muse HTTPS connection could not be confirmed. '
                          'Check Tailscale and endpoint ownership.')
            return
        if self.port==port:
            status,current,_=self.route._snapshot()
            if current!=self.route.owned or not status.get('AllowFunnel',{}).get(current[0]):
                raise RuntimeError('Muse HTTPS endpoint changed or is unavailable')
            self.error=None
            self.paused=False
            return
        self._remove()
        self.allowed()
        host=self.route.start(port)
        self.port,self.hostname=port,host
        # The queue/bridge owner lock prevents a second manager for this home.
        temp=self.record.with_suffix('.tmp')
        temp.write_text(json.dumps({'host':host,'port':port}),encoding='utf-8')
        temp.replace(self.record)
        self.allowed()
        state=self.bridge.state()
        if state.get('readonly_gateway_port')!=port:
            self._remove()
            raise RuntimeError('Muse authority changed during endpoint startup')
        self.error=None
        self.paused=False

    def _remove(self):
        owned=self.route.owned is not None
        self.route.close()
        self.port=self.hostname=None
        # An unowned or changed route must retain its recovery record until
        # ownership has been verified; never forget a still-serving endpoint.
        if owned:self.record.unlink(missing_ok=True)

    def start(self):
        if self.thread is not None:raise RuntimeError('Muse endpoint manager already started')
        self.thread=threading.Thread(target=self._run,name='DeskHand Muse connection',daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while not self.stopping.is_set():
                try:self.reconcile()
                except Exception:
                    self.error='Muse HTTPS connection could not be confirmed. Check Tailscale and endpoint ownership.'
                    try:self._remove()
                    except Exception:self.error='Muse HTTPS endpoint cleanup is unconfirmed.'
                self.stopping.wait(5)
        finally:
            try:self._remove()
            except Exception:self.error='Muse HTTPS endpoint cleanup is unconfirmed.'

    def status(self):
        # B2-D B-03: 'paused' is True only when the bridge itself
        # reports not-enabled / no port. A paused bridge is not a
        # missing funnel route -- the route only exists while enabled.
        return {'ready':self.port is not None and self.error is None,'error':self.error,'paused':self.paused}

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(40)
            if self.thread.is_alive():raise RuntimeError('Muse endpoint shutdown is unconfirmed')
            self.thread=None
        else:self._remove()
        if self.route.owned is not None:raise RuntimeError('Muse endpoint shutdown is unconfirmed')
