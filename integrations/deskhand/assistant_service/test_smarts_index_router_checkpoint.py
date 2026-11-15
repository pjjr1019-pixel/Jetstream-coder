"""Tests: index, checkpoints (Batch 3, v100.008).

B2-D (100.030): deskhand_step_router was removed (zero production
callers), so its tests were removed with it."""
import tempfile
import unittest
from pathlib import Path

from deskhand_index import AppFileIndex
from deskhand_checkpoint import save, load, resume

TASK = "task-1"


class TestIndex(unittest.TestCase):
    def test_search_apps_and_files_in_scope(self):
        index = AppFileIndex(roots=["C:\\Users\\Phlip\\Documents\\"])
        index.add_app("Calculator", "calc.exe", "calc.exe")
        index.add_app("Notepad", "notepad.exe", "notepad.exe")
        index.add_recent_file("C:\\Users\\Phlip\\Documents\\note.txt", mtime=100)
        index.add_recent_file("C:\\Users\\Phlip\\Documents\\other.txt", mtime=101)
        self.assertEqual(index.search("calc")["apps"][0]["name"], "Calculator")
        self.assertEqual(len(index.search("note")["files"]), 1)
        self.assertIsNotNone(index.launch_by_name("calculator"))
        self.assertIsNone(index.launch_by_name("definitely-not-an-app"))

    def test_out_of_scope_file_refused(self):
        index = AppFileIndex(roots=["C:\\Users\\Phlip\\Documents\\"])
        with self.assertRaises(ValueError):
            index.add_recent_file("C:\\Windows\\secret.txt")

    def test_sibling_prefix_is_out_of_scope(self):
        # B2-D M5: root /tmp/foo must not match /tmp/foobar/secret.txt.
        index = AppFileIndex(roots=["/tmp/foo"])
        with self.assertRaises(ValueError):
            index.add_recent_file("/tmp/foobar/secret.txt")
        index.add_recent_file("/tmp/foo/ok.txt")
        self.assertEqual(len(index.search("ok")["files"]), 1)

    def test_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            index = AppFileIndex(roots=["C:\\Users\\Phlip\\Documents\\"])
            index.add_app("Calculator", "calc.exe", "calc.exe")
            index.save(path)
            loaded = AppFileIndex.load(path)
            self.assertEqual(loaded.launch_by_name("calculator")["exe"], "calc.exe")



class TestCheckpoint(unittest.TestCase):
    def test_save_load_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            save(directory, TASK, goal="make a note",
                 completed_steps=[{"step": 1, "action": "filesystem.create",
                                   "status": "completed", "receipt_id": "r1"}],
                 next_step=2, receipts=[{"receipt_id": "r1"}])
            document = load(directory, TASK)
            self.assertEqual(document["goal"], "make a note")
            resumed = resume(directory, TASK, new_task_id="task-2",
                             new_turn_id="turn-2",
                             grants=[{"capability": "filesystem"}])
            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["next_step"], 2)
            self.assertFalse(resumed["tokens_valid"])
            self.assertIn("fresh grant", resumed["requires"][1])

    def test_uncertain_step_never_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                save(directory, TASK, goal="g",
                     completed_steps=[{"step": 1, "action": "window.activate",
                                       "status": "uncertain", "receipt_id": "r1"}],
                     next_step=2)

    def test_credential_like_receipt_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                save(directory, TASK, goal="g", completed_steps=[],
                     next_step=1, receipts=[{"api_token": "secret"}])

    def test_traversal_task_id_rejected(self):
        # B2-D M1: a task_id used as a filename must be one safe segment.
        with tempfile.TemporaryDirectory() as directory:
            for bad in ("../../x", "..", "/abs", "a/b", "a..b/c", ""):
                with self.assertRaises(ValueError, msg=bad):
                    save(directory, bad, goal="g", completed_steps=[],
                         next_step=1)
                with self.assertRaises(ValueError, msg=bad):
                    load(directory, bad)

    def test_resume_missing_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            resumed = resume(directory, "nope", new_task_id="t2", new_turn_id="u2",
                             grants=[{"capability": "window"}])
            self.assertFalse(resumed["resumed"])


if __name__ == "__main__":
    unittest.main()
