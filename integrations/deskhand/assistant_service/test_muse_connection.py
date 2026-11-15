import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch
from deskhand_muse_connection import MuseConnection
from deskhand_muse_funnel import MuseFunnel
from test_muse_funnel import FakeTailscale


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.fake=FakeTailscale()
        self.bridge=Mock(home=Path(self.temp.name))
        self.state={'enabled':True,'readonly_gateway_port':43210}
        self.bridge.state.side_effect=lambda:dict(self.state)
        self.allowed=Mock()
        # v2: wedged-restart tests wire an explicit guard (not in flight)
        # and an interrupted handler; without a guard a force-restart is
        # refused, so the guard must be explicit here.
        self.interrupted=Mock(return_value=0)
        self.manager=MuseConnection(self.bridge,self.allowed,MuseFunnel('fixture',self.fake),
            in_flight=lambda: False, on_interrupted=self.interrupted)

    def test_start_stop_recovery_and_port_change(self):
        self.manager.reconcile();self.assertTrue(self.manager.status()['ready'])
        self.assertTrue(self.manager.record.exists())
        self.state['enabled']=False
        self.manager.reconcile();self.assertFalse(self.manager.status()['ready'])
        self.assertNotIn('/deskhand-prototype',self.fake.state['Web'][self.fake.host]['Handlers'])
        self.state['enabled']=True;self.state['readonly_gateway_port']=43211
        self.manager.reconcile();self.assertTrue(self.manager.status()['ready'])
        self.manager.close();self.assertFalse(self.manager.record.exists())
        self.bridge.local_action.assert_not_called()

    def test_host_stop_removes_endpoint_and_never_clears_stop(self):
        self.manager.reconcile()
        self.allowed.side_effect=ValueError('STOP')
        self.manager.reconcile()
        self.assertFalse(self.manager.status()['ready'])
        self.bridge.local_action.assert_not_called()

    def test_disabled_publication_is_not_reported_ready(self):
        self.manager.reconcile()
        self.fake.state['AllowFunnel'][self.fake.host]=False
        with self.assertRaisesRegex(RuntimeError,'unavailable'):self.manager.reconcile()
        # H4: the failed reconcile is recorded as a failure, never as
        # a success derived from the stale ready flag.
        snap = self.manager.diagnostics.snapshot()
        self.assertIs(snap['last_reconcile_ok'], False)
        self.assertIn('unavailable', snap['last_reconcile_error'])
        self.manager.close()

    def test_failed_route_change_is_recorded_as_failure_despite_stale_ready(self):
        # H4 regression: the route is deleted externally after a good
        # reconcile. The next reconcile raises, and diagnostics must
        # record failure even though status()['ready'] is still stale
        # True (old port, error still None) until the background loop
        # heals it -- diagnostics, not status(), is the authority.
        self.manager.reconcile()
        self.assertTrue(self.manager.status()['ready'])
        self.assertIs(self.manager.diagnostics.snapshot()['last_reconcile_ok'], True)
        del self.fake.state['Web'][self.fake.host]['Handlers']['/deskhand-prototype']
        with self.assertRaisesRegex(RuntimeError,'unavailable'):self.manager.reconcile()
        snap = self.manager.diagnostics.snapshot()
        self.assertIs(snap['last_reconcile_ok'], False)
        self.assertIn('unavailable', snap['last_reconcile_error'])
        self.assertTrue(self.manager.status()['ready'])
        self.manager.close()

    def test_stale_route_recovery_requires_saved_identity_and_closed_port(self):
        self.manager.reconcile()
        other=MuseConnection(self.bridge,self.allowed,MuseFunnel('fixture',self.fake))
        with patch('deskhand_muse_connection.socket.socket') as sock:
            sock.return_value.__enter__.return_value.connect_ex.return_value=1
            other.reconcile()
        self.assertTrue(other.status()['ready'])
        other.close()

    def test_live_previous_listener_is_adopted(self):
        # B2-D B-04: a still-serving old endpoint is ADOPTED instead
        # of raising forever every 5 s. The reconcile below verifies
        # the bridge still reports this port before reporting ready.
        self.manager.reconcile()
        other=MuseConnection(self.bridge,self.allowed,MuseFunnel('fixture',self.fake))
        with patch('deskhand_muse_connection.socket.socket') as sock:
            sock.return_value.__enter__.return_value.connect_ex.return_value=0
            other.reconcile()
        self.assertTrue(other.status()['ready'])
        self.assertIn('/deskhand-prototype',self.fake.state['Web'][self.fake.host]['Handlers'])
        self.assertTrue(other.record.exists())
        other._remove()
        self.assertFalse(other.record.exists())
        self.assertNotIn('/deskhand-prototype',self.fake.state['Web'][self.fake.host]['Handlers'])
        self.manager.close()

    def test_corrupt_route_record_is_quarantined_and_heals(self):
        # B2-D B-04: an unparseable https-route.json used to raise on
        # every 5 s tick forever; now it is quarantined aside and the
        # connection recovers with a fresh route.
        self.manager.reconcile()
        self.manager.record.write_text('{not json', encoding='utf-8')
        other=MuseConnection(self.bridge,self.allowed,MuseFunnel('fixture',self.fake))
        other.reconcile()
        self.assertTrue(other.status()['ready'])
        quarantined = list(Path(self.temp.name).glob('https-route.json.*.corrupt'))
        self.assertEqual(len(quarantined), 1)
        other.close()
        self.manager.close()

    def test_invalid_port_record_is_forgotten_and_heals(self):
        # B2-D B-04: a record with an invalid port is forgotten
        # instead of raising forever.
        self.manager.reconcile()
        import json as _json
        self.manager.record.write_text(_json.dumps({'host': 'h', 'port': 'nope'}), encoding='utf-8')
        other=MuseConnection(self.bridge,self.allowed,MuseFunnel('fixture',self.fake))
        other.reconcile()
        self.assertTrue(other.status()['ready'])
        other.close()
        self.manager.close()

    def test_paused_bridge_reports_paused_not_just_not_ready(self):
        # B2-D B-03: a bridge that reports not-enabled surfaces
        # paused=True so the panel can tell "paused" apart from a
        # missing/deleted funnel route (gateway 404).
        self.manager.reconcile()
        self.assertFalse(self.manager.status()['paused'])
        self.state['enabled']=False
        self.manager.reconcile()
        self.assertFalse(self.manager.status()['ready'])
        self.assertTrue(self.manager.status()['paused'])
        self.assertIn('paused', self.manager.status()['error'])
        self.manager.close()

    def test_stop_during_publication_removes_route(self):
        self.allowed.side_effect=[None,None,ValueError('STOP')]
        with self.assertRaisesRegex(ValueError,'STOP'):self.manager.reconcile()
        # Background owner cleanup removes even an interrupted publication.
        self.manager.close()
        self.assertNotIn('/deskhand-prototype',self.fake.state['Web'][self.fake.host]['Handlers'])

    def test_reconcile_never_clears_stop_flag_in_background(self):
        # STOP removed (Phil 2026-11-09): the background loop never
        # reads or records any STOP flag. A disabled bridge reports
        # not-ready with a failed reconcile; no local_action is ever
        # issued and diagnostics carry no stop observation at all.
        latched={'enabled':False,'stop_latched':True,
                 'stop_reason':'local_emergency_stop','readonly_gateway_port':43210}
        self.bridge.state.side_effect=[latched,latched]
        self.manager.reconcile()
        self.assertFalse(self.manager.status()['ready'])
        self.bridge.local_action.assert_not_called()
        snap = self.manager.diagnostics.snapshot()
        self.assertNotIn('last_stop_observation', snap)
        self.assertIs(snap['last_reconcile_ok'], False)
        self.manager.close()

    def test_reconcile_never_clears_latch_when_host_stop_is_set(self):
        latched={'enabled':False,'stop_latched':True,'readonly_gateway_port':43210}
        self.bridge.state.return_value=latched
        self.allowed.side_effect=ValueError('STOP')
        self.manager.reconcile()
        self.assertFalse(self.manager.status()['ready'])
        self.bridge.local_action.assert_not_called()
        self.manager.close()

    def test_wedged_live_bridge_is_force_restarted_after_threshold(self):
        # Process alive (poll None) but the operator endpoint never
        # answers: three consecutive failed reads force a restart.
        self.bridge.process=Mock()
        self.bridge.process.poll.return_value=None
        self.bridge.state.side_effect=[RuntimeError('wedged'),RuntimeError('wedged'),
            RuntimeError('wedged'),dict(self.state)]
        self.bridge.start.return_value=self.state
        self.manager.reconcile();self.assertFalse(self.manager.status()['ready'])
        self.manager.reconcile();self.assertFalse(self.manager.status()['ready'])
        self.manager.reconcile();self.assertTrue(self.manager.status()['ready'])
        self.bridge.close.assert_called_once()
        self.bridge.start.assert_called_once()
        self.bridge.local_action.assert_not_called()
        self.manager.close()

    def test_wedged_bridge_force_restart_is_cooled_down(self):
        import time as _time
        self.bridge.process=Mock()
        self.bridge.process.poll.return_value=None
        self.bridge.state.side_effect=RuntimeError('wedged')
        self.manager._last_force_restart=_time.monotonic()
        for _ in range(3):self.manager.reconcile()
        self.assertFalse(self.manager.status()['ready'])
        self.bridge.close.assert_not_called()
        self.bridge.start.assert_not_called()
        self.manager.close()

    def test_transient_endpoint_failures_below_threshold_do_not_restart(self):
        # Two failed reads then a good one: a blip, not a restart.
        self.bridge.process=Mock()
        self.bridge.process.poll.return_value=None
        self.bridge.state.side_effect=[RuntimeError('blip'),RuntimeError('blip'),
            dict(self.state),dict(self.state)]
        self.manager.reconcile();self.assertFalse(self.manager.status()['ready'])
        self.manager.reconcile();self.assertFalse(self.manager.status()['ready'])
        self.manager.reconcile();self.assertTrue(self.manager.status()['ready'])
        self.bridge.close.assert_not_called()
        self.bridge.start.assert_not_called()
        self.manager.close()

    def test_exited_owned_backend_is_restarted_without_replaying(self):
        self.bridge.process=Mock()
        self.bridge.process.poll.return_value=1
        self.bridge.state.side_effect=[RuntimeError('exited'),self.state]
        self.bridge.start.return_value=self.state
        self.manager.reconcile()
        self.bridge.start.assert_called_once()
        self.bridge.local_action.assert_not_called()
        self.assertTrue(self.manager.status()['ready'])
        self.manager.close()
