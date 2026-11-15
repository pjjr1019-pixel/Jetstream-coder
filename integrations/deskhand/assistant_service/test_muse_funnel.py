import copy
import json
import unittest
from types import SimpleNamespace

from deskhand_muse_funnel import MuseFunnel


class FakeTailscale:
    def __init__(self):
        self.host = 'fixture.ts.net:443'
        self.state = {'Web': {self.host: {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:47801'}}}},
                      'AllowFunnel': {self.host: True}}
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args[1:])
        if args[1:] == ['serve', 'status', '--json']:
            return SimpleNamespace(stdout=json.dumps(self.state))
        handlers = self.state['Web'][self.host]['Handlers']
        if args[-1] == 'off':
            handlers.pop('/deskhand-prototype', None)
        else:
            handlers['/deskhand-prototype'] = {'Proxy': args[-1]}
        return SimpleNamespace(stdout='')


class MuseFunnelTests(unittest.TestCase):
    def test_only_owns_and_removes_its_path(self):
        fake = FakeTailscale()
        original = copy.deepcopy(fake.state['Web'][fake.host]['Handlers']['/'])
        route = MuseFunnel('fixture', fake)
        self.assertEqual(route.start(43210), fake.host)
        self.assertEqual(fake.state['Web'][fake.host]['Handlers']['/'], original)
        self.assertEqual(fake.state['Web'][fake.host]['Handlers']['/deskhand-prototype'],
                         {'Proxy': 'http://127.0.0.1:43210'})
        route.close()
        self.assertNotIn('/deskhand-prototype', fake.state['Web'][fake.host]['Handlers'])
        self.assertEqual(fake.state['Web'][fake.host]['Handlers']['/'], original)

    def test_refuses_existing_path_and_changed_ownership(self):
        fake = FakeTailscale()
        handlers = fake.state['Web'][fake.host]['Handlers']
        handlers['/deskhand-prototype'] = {'Proxy': 'http://127.0.0.1:9999'}
        route = MuseFunnel('fixture', fake)
        with self.assertRaisesRegex(RuntimeError, 'already in use'):
            route.start(43210)
        self.assertEqual(handlers['/deskhand-prototype']['Proxy'], 'http://127.0.0.1:9999')
        del handlers['/deskhand-prototype']
        route.start(43210)
        handlers['/deskhand-prototype'] = {'Proxy': 'http://127.0.0.1:9999'}
        with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
            route.close()
        self.assertEqual(handlers['/deskhand-prototype']['Proxy'], 'http://127.0.0.1:9999')

    def test_import_does_not_publish_and_invalid_port_is_rejected(self):
        fake = FakeTailscale()
        route = MuseFunnel('fixture', fake)
        self.assertEqual(fake.calls, [])
        with self.assertRaises(ValueError):
            route.start(0)
        self.assertEqual(fake.calls, [])

    def test_changed_root_during_start_is_not_overwritten(self):
        fake = FakeTailscale()
        def runner(args, **kwargs):
            result = fake(args, **kwargs)
            if args[1:3] == ['funnel', '--bg']:
                fake.state['Web'][fake.host]['Handlers']['/'] = {'Proxy': 'http://127.0.0.1:1111'}
            return result
        route = MuseFunnel('fixture', runner)
        with self.assertRaisesRegex(RuntimeError, 'could not be confirmed'):
            route.start(43210)
        handlers = fake.state['Web'][fake.host]['Handlers']
        self.assertEqual(handlers['/'], {'Proxy': 'http://127.0.0.1:1111'})
        self.assertNotIn('/deskhand-prototype', handlers)
