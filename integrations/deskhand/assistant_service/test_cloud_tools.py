import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from contextlib import closing

from deskhand_cloud_tools import HostToolChat, ProposalTransport
from deskhand_agent_session import run_agent_session
from deskhand_codex import account_identity
from deskhand_dispatch import Reply, Request, Route, StopConfirmed
from deskhand_local_tools import TOOLS


class CloudToolsTests(unittest.TestCase):
    def request(self, assistant):
        return Request(str(uuid.uuid4()), 'List the desktop', Route(assistant, 'fixture', 'deskhand' if assistant == 'muse' else 'codex:' + 'a' * 64, 'high'), str(uuid.uuid4()))

    def test_both_providers_round_trip_through_host_with_exact_route_and_durable_receipt(self):
        for assistant in ('muse', 'codex'):
            with self.subTest(assistant=assistant), tempfile.TemporaryDirectory() as folder:
                request = self.request(assistant)
                class Provider:
                    calls = 0
                    ack = 0
                    def __call__(self, inner):
                        self.calls += 1
                        self_outer.assertEqual(inner.route, request.route)
                        data = json.loads(inner.text.split('\n', 1)[1])
                        response = {'exchange_id': data['exchange_id'], **({'tool': {'name': 'desktop.list', 'arguments': {'folder_token': None}}} if self.calls == 1 else {'final': 'Found fixture.txt'})}
                        return Reply(inner.turn_id, inner.route, json.dumps(response))
                    def reply_saved(self, inner):
                        with closing(sqlite3.connect(Path(folder) / 'receipts.db')) as db:
                            self_outer.assertIsNotNone(db.execute('SELECT id FROM exchanges WHERE id=?', (inner.turn_id,)).fetchone())
                        self.ack += 1
                self_outer = self
                provider = Provider()
                calls = []
                class Host:
                    def call(self, op, payload):
                        if op == 'task.check': return {'active': True}
                        calls.append(payload)
                        return {'actionId': payload['action_id'], 'status': 'completed', 'data': {'items': [{'name': 'fixture.txt'}]}}
                def run(req, adapter):
                    result = run_agent_session(task_id=req.agent_task_id, turn_id=req.turn_id, adapter=adapter, host=Host(), stopped=lambda: False)
                    return Reply(req.turn_id, req.route, result.text)
                reply = HostToolChat(provider, run, Path(folder) / 'receipts.db')(request)
                self.assertEqual(reply.text, 'Found fixture.txt')
                self.assertEqual(calls[0]['task_id'], request.agent_task_id)
                self.assertEqual(calls[0]['turn_id'], request.turn_id)
                self.assertEqual(provider.ack, 2)

    def test_stale_duplicate_or_unadvertised_proposals_do_not_reach_execution(self):
        cases = [lambda x: {'exchange_id': str(uuid.uuid4()), 'final': 'wrong'},
                 lambda x: {'exchange_id': x, 'tool': {'name': 'shell.exec', 'arguments': {}}},
                 lambda x: {'exchange_id': x, 'final': 'one', 'tool': {'name': 'desktop.list', 'arguments': {'folder_token': None}}}]
        for make in cases:
            with tempfile.TemporaryDirectory() as folder:
                def provider(inner):
                    data = json.loads(inner.text.split('\n', 1)[1])
                    return Reply(inner.turn_id, inner.route, json.dumps(make(data['exchange_id'])))
                transport = ProposalTransport(self.request('muse'), provider, Path(folder) / 'receipts.db', lambda: False)
                with self.assertRaises(ValueError): transport([], TOOLS)

    def test_stop_during_exchange_prevents_the_returned_action(self):
        with tempfile.TemporaryDirectory() as folder:
            stopped = [False]
            def provider(inner):
                data = json.loads(inner.text.split('\n', 1)[1])
                stopped[0] = True
                return Reply(inner.turn_id, inner.route, json.dumps({'exchange_id': data['exchange_id'], 'tool': {'name': 'desktop.list', 'arguments': {'folder_token': None}}}))
            transport = ProposalTransport(self.request('codex'), provider, Path(folder) / 'receipts.db', lambda: stopped[0])
            with self.assertRaises(StopConfirmed): transport([], TOOLS)

    def test_close_forwards_to_provider_when_present(self):
        # Batch 1, v100.003: service shutdown releases warm processes
        # through HostToolChat.close() -> provider.close().
        closed = []
        class Provider:
            def close(self):
                closed.append(True)
        chat = HostToolChat(Provider(), lambda req, adapter: None, Path("receipts.db"))
        chat.close()
        self.assertEqual(closed, [True])

    def test_close_is_noop_when_provider_has_no_close(self):
        chat = HostToolChat(object(), lambda req, adapter: None, Path("receipts.db"))
        chat.close()  # must not raise (e.g. the Muse relay has no close)

    def test_account_identity_is_stable_and_changes_with_account(self):
        one = account_identity({'type': 'chatgpt', 'email': 'one@example.test', 'planType': 'pro'})
        self.assertEqual(one, account_identity({'type': 'chatgpt', 'email': 'one@example.test', 'planType': 'plus'}))
        self.assertNotEqual(one, account_identity({'type': 'chatgpt', 'email': 'two@example.test'}))
        with self.assertRaises(RuntimeError): account_identity({'type': 'apiKey'})
