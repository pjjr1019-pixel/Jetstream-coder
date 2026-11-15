import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from deskhand_service import ChatService


class PhoneStopCandidateTest(unittest.TestCase):
    def test_muse_signin_does_not_resume_explicitly_stopped_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ChatService(directory, fixture=True)
            try:
                service.muse = Mock()
                service.muse.browser = SimpleNamespace(cloud_ready=True)
                service.handle({"op": "stop_chat"})
                self.assertTrue(service.dispatcher.admission_state()["stopped"])
                service.handle({"op": "muse_signin_done"})
                self.assertTrue(service.dispatcher.admission_state()["stopped"])
                service.muse.finish_sign_in.assert_called_once()
                service.handle({"op": "resume_chat_local"})
                self.assertFalse(service.dispatcher.admission_state()["stopped"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
