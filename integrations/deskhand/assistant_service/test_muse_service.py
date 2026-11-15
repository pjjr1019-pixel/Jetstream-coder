import tempfile
import time
import unittest
import uuid
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock, call, patch

from deskhand_muse_service import MuseService
from deskhand_service import ChatService
from deskhand_dispatch import Reply


class MuseServiceTests(unittest.TestCase):
    def test_status_reports_cloud_and_delivery_independently(self):
        with tempfile.TemporaryDirectory() as folder:
            service = MuseService(Path(folder) / 'runtime', folder)
            service.browser = Mock()
            service.browser.status.return_value = 'Signed out'
            service.browser.cloud_ready = False
            service.bridge = Mock()
            service.bridge.state.return_value = {'shared_actions_host_only': True, 'https_actions_available': False}
            service.delivery_ready = True
            status = service.status()
            self.assertFalse(status['cloud_ready'])
            self.assertTrue(status['delivery_ready'])
            self.assertFalse(status['bridge']['https_actions_available'])
            service.browser.cloud_ready = True
            service.delivery_ready = False
            status = service.status()
            self.assertTrue(status['cloud_ready'])
            self.assertFalse(status['delivery_ready'])

    def test_normal_shutdown_drains_without_emergency_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            service=MuseService(Path(folder)/'runtime',folder)
            events=Mock()
            service.mailbox,service.relay,service.bridge=events.mailbox,events.relay,events.bridge
            service.prepare_shutdown()
            self.assertEqual(events.mock_calls,[call.mailbox.set_offline_hold(True),
                call.relay.request_stop(),call.bridge.close()])
            events.bridge.stop_all.assert_not_called()

    def test_shutdown_still_closes_backend_when_mailbox_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            service=MuseService(Path(folder)/'runtime',folder)
            service.mailbox,service.relay,service.bridge=Mock(),Mock(),Mock()
            service.mailbox.set_offline_hold.side_effect=RuntimeError('fixture')
            with self.assertRaisesRegex(RuntimeError,'fixture'):
                service.prepare_shutdown()
            service.bridge.close.assert_called_once()
    def test_stop_reports_removal_and_touches_nothing(self):
        # STOP removed (Phil 2026-11-09): stop_all() reports the
        # removal (stopped False, stop_removed True), never raises,
        # and touches no mailbox, relay, or bridge state -- there is
        # no latch to stop and nothing is written anywhere.
        with tempfile.TemporaryDirectory() as folder:
            service = MuseService(Path(folder) / 'runtime', folder)
            events = Mock()
            service.mailbox = events.mailbox
            service.relay = events.relay
            service.bridge = events.bridge
            self.assertEqual(service.stop_all(), {'stopped': False, 'stop_removed': True})
            self.assertEqual(events.mock_calls, [])
            self.assertFalse(hasattr(service, '_last_stop_decision'))

    def test_stop_reports_removal_even_if_bridge_is_unreachable(self):
        # A bridge that cannot be reached changes nothing: stopping
        # is removed, so there is no 'unconfirmed' outcome to report
        # and no state to write.
        with tempfile.TemporaryDirectory() as folder:
            service = MuseService(Path(folder) / 'runtime', folder)
            service.mailbox, service.relay, service.bridge = Mock(), Mock(), Mock()
            service.bridge.stop_all.side_effect = RuntimeError('unreachable')
            self.assertEqual(service.stop_all(), {'stopped': False, 'stop_removed': True})
            service.mailbox.set_offline_hold.assert_not_called()
            service.bridge.stop_all.assert_not_called()

    def test_stopped_listener_reholds_browser_delivery(self):
        source = Path(__file__).resolve().parent.parent / 'vendor' / 'jettpad' / 'backend' / 'src'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                service.bridge.state.return_value = {'enabled': False, 'connected': False}
                with patch.object(host_stop, 'ensure_host_allows'):
                    with self.assertRaisesRegex(RuntimeError, 'not ready'):
                        service.ensure_chat_ready()
                service.mailbox.set_offline_hold.assert_called_once_with(True)
        finally:
            sys.path.remove(str(source))

    def test_chat_can_bootstrap_session_and_reholds_on_race(self):
        source = Path(__file__).resolve().parent.parent / 'vendor' / 'jettpad' / 'backend' / 'src'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                ready = {'enabled': True, 'connected': False}
                service.bridge.state.return_value = ready
                with patch.object(host_stop, 'ensure_host_allows'):
                    service.ensure_chat_ready()
                service.mailbox.set_offline_hold.assert_called_once_with(False)
                service.bridge.local_action.assert_not_called()
                service.mailbox.reset_mock()
                with patch.object(host_stop, 'ensure_host_allows'):
                    service.ensure_chat_ready()
                service.mailbox.set_offline_hold.assert_not_called()
                service.delivery_ready = False
                service.bridge.state.side_effect = [ready, {**ready, 'enabled': False}]
                with patch.object(host_stop, 'ensure_host_allows'):
                    with self.assertRaisesRegex(RuntimeError, 'not ready'):
                        service.ensure_chat_ready()
                self.assertEqual(service.mailbox.set_offline_hold.call_args_list, [call(False), call(True)])
        finally:
            sys.path.remove(str(source))

    def test_stop_touches_nothing_even_when_mailbox_would_fail(self):
        # STOP removed: stop_all() never touches the mailbox (or the
        # bridge), so a mailbox failure cannot surface through it.
        with tempfile.TemporaryDirectory() as folder:
            service = MuseService(Path(folder) / 'runtime', folder)
            service.mailbox, service.relay, service.bridge = Mock(), Mock(), Mock()
            service.mailbox.set_offline_hold.side_effect = RuntimeError('fixture')
            self.assertEqual(service.stop_all(), {'stopped': False, 'stop_removed': True})
            service.mailbox.set_offline_hold.assert_not_called()
            service.bridge.stop_all.assert_not_called()

    def test_resume_reholds_delivery_if_host_stop_arrives_during_release(self):
        source = Path(__file__).resolve().parent.parent / 'vendor' / 'jettpad' / 'backend' / 'src'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                service.bridge.state.return_value = {'enabled': True, 'connected': True}
                with patch.object(host_stop, 'ensure_host_allows', side_effect=[None, None, ValueError('STOP')]):
                    with self.assertRaisesRegex(ValueError, 'STOP'):
                        service.resume_local()
                self.assertEqual(service.mailbox.set_offline_hold.call_args_list, [call(False), call(True)])
        finally:
            sys.path.remove(str(source))

    def test_ensure_chat_ready_refuses_latched_bridge(self):
        # Fail-closed decision (audit P1, emergency-stop semantics): the
        # name is historical. A latched bridge is NEVER cleared by
        # software, even for the ambiguous startup reason
        # 'local_emergency_stop' (indistinguishable from an operator
        # emergency STOP). ensure_chat_ready must refuse, leave the
        # flag set, hold delivery, and report not-ready.
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                latched = {'enabled': False, 'connected': False, 'stop_latched': True, 'stop_reason': 'local_emergency_stop'}
                service.bridge.state.return_value = latched
                with patch.object(host_stop, 'ensure_host_allows'):
                    with self.assertRaisesRegex(RuntimeError, 'not ready'):
                        service.ensure_chat_ready()
                service.bridge.local_action.assert_not_called()
                self.assertFalse(service.delivery_ready)
                service.mailbox.set_offline_hold.assert_called_once_with(True)
        finally:
            sys.path.remove(str(source))

    def test_ensure_chat_ready_does_not_clear_latched_flag_when_bridge_enabled(self):
        # Fail-closed decision: the name is historical. When the bridge
        # is enabled but its STOP flag is still latched, the service
        # must NOT clear the flag itself. Readiness follows the enabled
        # flag (so delivery can still become ready), but no clear_stop
        # is ever issued; the flag is left for a local clear.
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                latched = {'enabled': True, 'connected': False, 'stop_latched': True, 'stop_reason': 'local_emergency_stop'}
                cleared = {'enabled': True, 'connected': False}
                service.bridge.state.side_effect = [latched, cleared, cleared]
                with patch.object(host_stop, 'ensure_host_allows'):
                    service.ensure_chat_ready()
                service.bridge.local_action.assert_not_called()
                self.assertTrue(service.delivery_ready)
                service.mailbox.set_offline_hold.assert_called_once_with(False)
        finally:
            sys.path.remove(str(source))

    def test_ensure_chat_ready_does_not_clear_when_host_stop_is_set(self):
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                service.bridge.state.return_value = {'enabled': False, 'connected': False, 'stop_latched': True}
                with patch.object(host_stop, 'ensure_host_allows', side_effect=ValueError('STOP')):
                    with self.assertRaisesRegex(RuntimeError, 'not ready'):
                        service.ensure_chat_ready()
                service.bridge.local_action.assert_not_called()
                service.mailbox.set_offline_hold.assert_called_once_with(True)
        finally:
            sys.path.remove(str(source))

    def test_ensure_chat_ready_throttled_reconcile_heals_not_ready_connection(self):
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                service.bridge.state.return_value = {'enabled': True, 'connected': False}
                service.connection = Mock()
                # H4: the on-demand reconcile runs FIRST, then one
                # status read sees the healed route. The old two-read
                # shape (status, reconcile, status) is gone.
                service.connection.status.return_value = {'ready': True}
                with patch.object(host_stop, 'ensure_host_allows'):
                    service.ensure_chat_ready()
                service.connection.reconcile.assert_called_once()
                service.mailbox.set_offline_hold.assert_called_once_with(False)
                self.assertTrue(service.delivery_ready)
        finally:
            sys.path.remove(str(source))

    def test_ensure_chat_ready_does_not_trust_stale_ready_after_failed_reconcile(self):
        # H4: status()['ready'] can still say True after the funnel
        # route died (stale port, error still None). When the last
        # recorded reconcile failed, ensure_chat_ready must run its
        # on-demand reconcile and refuse -- never let the stale ready
        # flag slip a turn through.
        from deskhand_reconnect_v2 import ReconnectDiagnostics
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                service.bridge.state.return_value = {'enabled': True, 'connected': False}
                service.connection = Mock()
                diagnostics = ReconnectDiagnostics()
                diagnostics.record_reconcile(False, 'Muse HTTPS endpoint changed or is unavailable')
                service.connection.diagnostics = diagnostics
                service.connection.status.return_value = {'ready': True}
                with patch.object(host_stop, 'ensure_host_allows'):
                    with self.assertRaisesRegex(RuntimeError, 'not ready'):
                        service.ensure_chat_ready()
                service.connection.reconcile.assert_called_once()
                self.assertFalse(service.delivery_ready)
                service.mailbox.set_offline_hold.assert_called_once_with(True)
        finally:
            sys.path.remove(str(source))

    def test_ensure_chat_ready_reconcile_is_throttled_within_two_seconds(self):
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                service.bridge.state.return_value = {'enabled': True, 'connected': False}
                service.connection = Mock()
                service.connection.status.return_value = {'ready': False}
                with patch.object(host_stop, 'ensure_host_allows'):
                    with self.assertRaisesRegex(RuntimeError, 'not ready'):
                        service.ensure_chat_ready()
                    with self.assertRaisesRegex(RuntimeError, 'not ready'):
                        service.ensure_chat_ready()
                # The relay calls this in its wait loop; reconcile must
                # run at most once per 2s, not on every call.
                service.connection.reconcile.assert_called_once()
        finally:
            sys.path.remove(str(source))

    def test_prepare_host_tools_never_clears_latched_bridge(self):
        # Fail-closed decision: the name is historical. The service must
        # never clear a latched STOP itself. It may still reach ready
        # when the latch is cleared locally (the fixture's state
        # sequence simulates an external/local clear between polls),
        # but no clear_stop or start is ever issued by the service.
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.browser = Mock()
                service.browser.cloud_ready = True
                service.bridge = Mock()
                service.connection = Mock()
                latched = {'enabled': False, 'connected': False, 'stop_latched': True, 'stop_reason': 'local_emergency_stop', 'shared_actions_host_only': True}
                cleared_disabled = {'enabled': False, 'connected': False, 'shared_actions_host_only': True}
                cleared = {'enabled': True, 'connected': False, 'readonly_gateway_port': 12345, 'shared_actions_host_only': True}
                service.bridge.state.side_effect = [cleared, latched, cleared_disabled, cleared, cleared]
                service.connection.status.side_effect = [{'ready': False}, {'ready': True}]
                with patch.object(host_stop, 'ensure_host_allows'), \
                     patch.object(service, 'ensure_chat_ready') as ready, \
                     patch('deskhand_muse_service.time.sleep'):
                    self.assertEqual(service.prepare_host_tools(), {'ready': True})
                service.bridge.local_action.assert_not_called()
                ready.assert_called_once()
        finally:
            sys.path.remove(str(source))

    def test_prepare_host_tools_retries_transient_reconcile_error(self):
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.browser = Mock()
                service.browser.cloud_ready = True
                service.bridge = Mock()
                service.connection = Mock()
                ready_state = {'enabled': True, 'connected': False, 'readonly_gateway_port': 12345, 'shared_actions_host_only': True}
                service.bridge.state.return_value = ready_state
                # First reconcile raises a transient funnel-ownership error;
                # the second succeeds. The 25s attempt must not abort.
                service.connection.reconcile.side_effect = [
                    RuntimeError('Muse HTTPS endpoint changed or is unavailable'),
                    None,
                ]
                service.connection.status.side_effect = [{'ready': True}]
                with patch.object(host_stop, 'ensure_host_allows'), \
                     patch.object(service, 'ensure_chat_ready') as ready, \
                     patch('deskhand_muse_service.time.sleep'):
                    self.assertEqual(service.prepare_host_tools(), {'ready': True})
                self.assertEqual(service.connection.reconcile.call_count, 2)
                ready.assert_called_once()
        finally:
            sys.path.remove(str(source))

    def test_prepare_host_tools_does_not_clear_when_host_stop_is_set(self):
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.browser = Mock()
                service.browser.cloud_ready = True
                service.bridge = Mock()
                service.connection = Mock()
                service.bridge.state.return_value = {'enabled': False, 'connected': False, 'stop_latched': True, 'shared_actions_host_only': True}
                with patch.object(host_stop, 'ensure_host_allows', side_effect=ValueError('STOP')):
                    with self.assertRaises(ValueError):
                        service.prepare_host_tools()
                service.bridge.local_action.assert_not_called()
        finally:
            sys.path.remove(str(source))

    def test_resume_local_refuses_latched_bridge(self):
        # Fail-closed decision: the name is historical. A latched
        # bridge is NEVER cleared by software, even for the ambiguous
        # startup reason 'local_emergency_stop'. resume_local must
        # refuse (the bridge is not connected), leave the flag set, and
        # not release delivery.
        source = Path(__file__).resolve().parent.parent / 'vendor-stub'
        sys.path.insert(0, str(source))
        try:
            from musebridge import host_stop
            with tempfile.TemporaryDirectory() as folder:
                service = MuseService(Path(folder) / 'runtime', folder)
                service.mailbox, service.bridge = Mock(), Mock()
                latched = {'enabled': False, 'connected': False, 'stop_latched': True, 'stop_reason': 'local_emergency_stop'}
                service.bridge.state.return_value = latched
                with patch.object(host_stop, 'ensure_host_allows'):
                    with self.assertRaisesRegex(RuntimeError, 'Connect MuseBridge before resuming'):
                        service.resume_local()
                service.bridge.local_action.assert_not_called()
                self.assertFalse(service.delivery_ready)
                service.mailbox.set_offline_hold.assert_not_called()
        finally:
            sys.path.remove(str(source))

    def test_injected_muse_route_uses_shared_chat_and_reports_stop_removed(self):
        class Relay:
            def __call__(self, request):
                return Reply(request.turn_id, request.route, 'fixture Muse reply')
        with tempfile.TemporaryDirectory() as folder:
            muse = Mock()
            muse.relay = Relay()
            service = ChatService(folder, fixture=True, muse=muse)
            try:
                routes = service.handle({'op': 'catalog'})['routes']
                route = next(row for row in routes if row['assistant'] == 'muse')
                self.assertTrue(route['default'])
                self.assertFalse(any(row.get('default') for row in routes if row['assistant'] != 'muse'))
                turn = str(uuid.uuid4())
                service.handle({'op': 'submit', 'id': turn, 'text': 'hello',
                    **{key: route[key] for key in ('assistant', 'model', 'account', 'effort')}})
                for _ in range(100):
                    row = service.handle({'op': 'get', 'id': turn})
                    if row['status'] == 'completed': break
                    time.sleep(.02)
                self.assertEqual(row['reply'], 'fixture Muse reply')
                # STOP removed: stop_chat never calls muse.stop_all()
                # and reports the Muse leg as not-enabled, not confirmed.
                self.assertEqual(service.handle({'op': 'stop_chat'})['muse_stop'], 'not_enabled')
                self.assertTrue(service.handle({'op': 'stop_chat'})['stop_removed'])
                muse.stop_all.assert_not_called()
            finally:
                service.close()

    def test_muse_stop_is_reported_removed_not_unconfirmed(self):
        # STOP removed: stop_chat never calls muse.stop_all(), so a
        # muse object whose stop would raise is irrelevant -- the Muse
        # leg is reported not-enabled, never 'unconfirmed'.
        with tempfile.TemporaryDirectory() as folder:
            muse = Mock()
            muse.stop_all.side_effect = RuntimeError('unreachable')
            service = ChatService(folder, fixture=True, muse=muse)
            try:
                result = service.handle({'op': 'stop_chat'})
                self.assertEqual(result['muse_stop'], 'not_enabled')
                self.assertTrue(result['stop_removed'])
                muse.stop_all.assert_not_called()
            finally:
                service.close()

    def test_local_setup_commands_require_attached_muse(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                with self.assertRaisesRegex(RuntimeError, 'not enabled'):
                    service.handle({'op': 'muse_signin'})
                muse = Mock()
                service.attach_muse(muse)
                service.handle({'op': 'muse_signin'})
                service.handle({'op': 'muse_signin_done'})
                muse.sign_in.assert_called_once()
                muse.finish_sign_in.assert_called_once()
                self.assertTrue(any(r['assistant'] == 'muse' for r in service.handle({'op': 'catalog'})['routes']))
            finally:
                service.close()

    def test_stop_muse_reports_removal_without_stopping_other_chat_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            muse = Mock()
            service = ChatService(folder, fixture=True, muse=muse)
            try:
                self.assertEqual(service.handle({'op': 'stop_muse'}),
                                 {'muse_stop': 'not_enabled', 'stop_removed': True})
                self.assertFalse(service.handle({'op': 'history'})['chatAdmission']['stopped'])
                muse.stop_all.assert_not_called()
            finally:
                service.close()

    def test_local_settings_do_not_expose_clear_stop_or_action_override(self):
        with tempfile.TemporaryDirectory() as folder:
            muse = Mock()
            service = ChatService(folder, fixture=True, muse=muse)
            try:
                service.handle({'op': 'muse_action', 'action': 'configure', 'payload': {'port': 9000}})
                muse.bridge.local_action.assert_called_once_with('configure', {'port': 9000})
                with self.assertRaises(ValueError):
                    service.handle({'op': 'muse_action', 'action': 'clear_stop'})
                with self.assertRaises(ValueError):
                    service.handle({'op': 'muse_action', 'action': 'configure', 'payload': {'action': 'clear_stop'}})
            finally:
                service.close()

    def test_resume_chat_reports_muse_blockers(self):
        with tempfile.TemporaryDirectory() as folder:
            muse = Mock()
            muse.bridge.state.return_value = {'enabled': True, 'connected': False}
            service = ChatService(folder, fixture=True, muse=muse)
            try:
                result = service.handle({'op': 'resume_chat_local'})
                self.assertIn('waiting for its bridge connection', result['status'])
                muse.bridge.local_action.assert_not_called()
                muse.bridge.state.return_value = {'enabled': True, 'connected': True}
                result = service.handle({'op': 'resume_chat_local'})
                self.assertEqual('Chat is ready.', result['status'])
            finally:
                service.close()

    def test_shared_chat_through_real_mailboxes_and_owned_bridge_fixture(self):
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
        try:
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=False):
                os.environ.pop('DESKHAND_JETT_RUNTIME', None)
                muse = MuseService(Path(folder) / 'muse', source, sys.executable)
                muse.bridge.home.mkdir(parents=True)
                (muse.bridge.home / 'musebridge.config.json').write_text(json.dumps({'token': 'fixture-' + 'x' * 40}))
                fixture = Path(__file__).resolve().parent / 'fixtures' / 'owned_bridge.py'
                muse.bridge._command = lambda *args: [sys.executable, '-B', str(fixture)]
                with patch.object(headless_browser, 'HeadlessBrowser', FakeBrowser), \
                     patch.object(cli, '_lock_windows_data'), \
                     patch.object(muse, 'ensure_chat_ready'), \
                     patch('deskhand_musebridge_runtime._make_kill_job', return_value=None):
                    muse.start()
                    chat = ChatService(Path(folder) / 'chat', fixture=True, muse=muse)
                    try:
                        route = next(r for r in chat.handle({'op': 'catalog'})['routes'] if r['assistant'] == 'muse')
                        turn = str(uuid.uuid4())
                        chat.handle({'op': 'submit', 'id': turn, 'text': 'fixture prompt',
                            **{k: route[k] for k in ('assistant', 'model', 'account', 'effort')}})
                        for _ in range(100):
                            job = muse.mailbox.get(turn)
                            if job: break
                            time.sleep(.02)
                        self.assertIsNotNone(job)
                        attempt = str(uuid.uuid4())
                        with muse.mailbox.lock, muse.mailbox.db:
                            muse.mailbox.db.execute("UPDATE jobs SET state='sent',attempt=? WHERE id=?", (attempt, turn))
                        receipt = muse.root / 'documents' / 'jett-pad-inbox' / (job['reply_id'] + '.json')
                        receipt.write_text(json.dumps({'id': job['reply_id'], 'from': 'jett', 'text': 'fixture answer'}))
                        for _ in range(100):
                            row = chat.handle({'op': 'get', 'id': turn})
                            if row['status'] == 'completed': break
                            time.sleep(.02)
                        self.assertEqual(row['reply'], 'fixture answer')
                        self.assertEqual(chat.handle({'op': 'stop_chat'})['muse_stop'], 'not_enabled')
                    finally:
                        chat.close()
        finally:
            if str(source) in sys.path: sys.path.remove(str(source))

    def test_recent_file_activity_filters_bridge_audit_to_files_and_exec(self):
        with tempfile.TemporaryDirectory() as folder:
            service = MuseService(Path(folder) / 'runtime', folder)
            service.bridge = Mock()
            service.bridge.full_state.return_value = {'audit': [
                {'ts': 3, 'operation': 'fs.read', 'target': 'C:/Docs/notes.txt', 'ok': True, 'detail': ''},
                {'ts': 2, 'operation': 'bridge.start', 'target': '', 'ok': True, 'detail': ''},
                {'ts': 1, 'operation': 'exec.run', 'target': 'C:/Docs', 'ok': True, 'detail': '{"command": "dir"}'},
                {'ts': 0, 'operation': 'fs.write', 'target': 'C:/Docs/out.txt', 'ok': False, 'detail': ''},
            ]}
            rows = service.recent_file_activity(limit=8)
            self.assertEqual([r['operation'] for r in rows], ['fs.read', 'exec.run', 'fs.write'])
            self.assertEqual(rows[0]['path'], 'C:/Docs/notes.txt')
            self.assertEqual(rows[2]['ok'], False)
            self.assertEqual(len(service.recent_file_activity(limit=2)), 2)

    def test_recent_file_activity_is_empty_when_bridge_is_unreachable(self):
        with tempfile.TemporaryDirectory() as folder:
            service = MuseService(Path(folder) / 'runtime', folder)
            service.bridge = Mock()
            service.bridge.full_state.side_effect = RuntimeError('not running')
            self.assertEqual(service.recent_file_activity(), [])
            service.bridge.full_state.side_effect = None
            service.bridge.full_state.return_value = {'enabled': True}
            self.assertEqual(service.recent_file_activity(), [])

    def test_activity_op_shows_current_task_and_recent_files(self):
        with tempfile.TemporaryDirectory() as folder:
            muse = Mock()
            muse.recent_file_activity.return_value = [
                {'operation': 'fs.read', 'path': 'C:/Docs/notes.txt', 'ok': True, 'ts': 3},
                {'operation': 'fs.write', 'path': 'C:/Docs/out.txt', 'ok': True, 'ts': 2},
            ]
            service = ChatService(folder, fixture=True, muse=muse)
            try:
                # Idle with no turns: no task, empty status.
                idle = service.handle({'op': 'activity'})
                self.assertEqual(idle['status'], 'idle')
                self.assertIsNone(idle['task'])
                self.assertEqual(len(idle['files']), 2)
                # A queued turn is the current task and makes it busy.
                turn = str(uuid.uuid4())
                service.box.submit(turn, 'open calculator', 'muse', 'muse', 'deskhand')
                busy = service.handle({'op': 'activity'})
                self.assertEqual(busy['status'], 'busy')
                self.assertEqual(busy['task']['text'], 'open calculator')
                self.assertEqual(busy['task']['status'], 'queued')
                # Completing it returns to idle and keeps it as the last task.
                claimed = service.box.claim()
                self.assertEqual(claimed['id'], turn)
                service.box.finish(turn, reply='done')
                done = service.handle({'op': 'activity'})
                self.assertEqual(done['status'], 'idle')
                self.assertEqual(done['task']['text'], 'open calculator')
                self.assertEqual(done['task']['status'], 'completed')
            finally:
                service.close()

    def test_activity_op_without_muse_has_no_files(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                result = service.handle({'op': 'activity'})
                self.assertEqual(result['status'], 'idle')
                self.assertEqual(result['files'], [])
            finally:
                service.close()
