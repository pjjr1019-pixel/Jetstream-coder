import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from deskhand_mailbox import Mailbox


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Mailbox(Path(self.tmp.name) / "turns.sqlite")
        self.id = str(uuid.uuid4())

    def test_duplicate_preserves_route_and_refuses_changed_request(self):
        self.box.submit(self.id, "hello", "codex", "model-a")
        self.assertEqual(self.box.submit(self.id, "hello", "codex", "model-a")["status"], "queued")
        with self.assertRaises(ValueError):
            self.box.submit(self.id, "hello", "codex", "model-b")

    def test_interruption_is_unknown_and_never_reclaimed(self):
        self.box.submit(self.id, "hello", "codex", "default")
        self.box.claim()
        self.assertEqual(self.box.recover_interrupted(), 1)
        self.assertEqual(self.box.get(self.id)["status"], "unknown")
        self.assertIsNone(self.box.claim())

    def test_only_one_worker_claims_a_turn(self):
        self.box.submit(self.id, "hello", "local", "example")
        found = []
        workers = [threading.Thread(target=lambda: found.append(self.box.claim())) for _ in range(4)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(sum(item is not None for item in found), 1)
        self.box.finish(self.id, reply="done")
        self.assertEqual(self.box.get(self.id)["reply"], "done")
        with self.assertRaises(ValueError): self.box.finish(self.id, reply="again")

    def test_account_and_effort_are_immutable(self):
        self.box.submit(self.id, "hello", "codex", "model-a", "account-1", "high")
        with self.assertRaises(ValueError):
            self.box.submit(self.id, "hello", "codex", "model-a", "account-2", "high")
        with self.assertRaises(ValueError):
            self.box.submit(self.id, "hello", "codex", "model-a", "account-1", "low")
        self.assertEqual(self.box.claim()["account"], "account-1")

    def test_agent_task_identity_persists_and_cannot_be_replaced(self):
        task = str(uuid.uuid4())
        self.box.submit(self.id, "hello", "codex", "model-a", agent_task_id=task)
        self.assertEqual(self.box.claim()["agent_task_id"], task)
        with self.assertRaises(ValueError):
            self.box.submit(self.id, "hello", "codex", "model-a", agent_task_id=str(uuid.uuid4()))


if __name__ == "__main__": unittest.main()
