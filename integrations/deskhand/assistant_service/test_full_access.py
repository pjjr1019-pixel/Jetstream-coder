import json
import tempfile
import unittest
from unittest.mock import MagicMock

from deskhand_service import ChatService


class FullAccessReadinessTests(unittest.TestCase):
    def test_missing_adapter_and_wrong_account_are_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder, fixture=True)
            try:
                for assistant, account in [('muse', 'deskhand'), ('codex', 'default'), ('local', ''), ('local', 'other')]:
                    with self.assertRaises(RuntimeError):
                        service.handle({'op': 'access_validate', 'assistant': assistant, 'account': account})
            finally:
                service.close()

    def test_removed_model_or_unreachable_server_cannot_enable_full_access(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ChatService(folder)
            try:
                service.handle({'op': 'local_configure', 'model': 'chosen', 'endpoint': 'http://127.0.0.1:1'})
                chat = next(iter(service.local_routes.values()))
                chat.client = MagicMock()
                response = chat.client.open.return_value.__enter__.return_value
                response.status = 200
                response.read.return_value = json.dumps({'models': [{'name': 'another:latest'}]}).encode()
                with self.assertRaisesRegex(RuntimeError, 'no longer available'):
                    service.handle({'op': 'access_validate', 'assistant': 'local', 'account': ''})
                response.read.return_value = json.dumps({'models': [{'name': 'chosen:latest'}]}).encode()
                self.assertTrue(service.handle({'op': 'access_validate', 'assistant': 'local', 'account': ''})['ready'])
                chat.client.open.side_effect = OSError('unreachable')
                with self.assertRaises(OSError):
                    service.handle({'op': 'access_validate', 'assistant': 'local', 'account': ''})
            finally:
                service.close()
