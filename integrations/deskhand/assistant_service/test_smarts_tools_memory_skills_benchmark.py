"""Tests: new tools, memory, skills (Batch 3, v100.008).

B2-D (100.030): deskhand_benchmark was removed (zero production
callers), so its tests were removed with it."""
import tempfile
import unittest
from pathlib import Path

from deskhand_tools import parse_new_tool, redacted_summary
from deskhand_memory import add_fact, list_facts, search_facts, delete_fact, build_snapshot, snapshot_expired
from deskhand_skills import load_skill, list_skills, run_skill


class TestNewTools(unittest.TestCase):
    def test_wait_for_needs_target_and_bounded_timeout(self):
        self.assertEqual(parse_new_tool("window.wait_for", {"text": "Calculator", "timeout_secs": 10})["timeout_secs"], 10)
        with self.assertRaises(ValueError):
            parse_new_tool("window.wait_for", {"timeout_secs": 10})
        with self.assertRaises(ValueError):
            parse_new_tool("window.wait_for", {"text": "x", "timeout_secs": 31})

    def test_clipboard_get_set_and_redaction(self):
        self.assertEqual(parse_new_tool("clipboard.get", {}), {})
        self.assertEqual(parse_new_tool("clipboard.set", {"text": "hello"})["text"], "hello")
        summary = redacted_summary("clipboard.set", {"text": "sensitive contents"})
        self.assertEqual(summary, {"tool": "clipboard.set", "text_length": 18})
        self.assertNotIn("sensitive", str(summary))

    def test_search_launch_monitor(self):
        parse_new_tool("filesystem.search", {"query": "note"})
        parse_new_tool("app.launch_by_name", {"name": "Calculator", "purpose": "type digits"})
        parse_new_tool("screenshot.monitor", {"monitor_index": 0})
        with self.assertRaises(ValueError):
            parse_new_tool("screenshot.monitor", {"monitor_index": 16})
        with self.assertRaises(ValueError):
            parse_new_tool("clipboard.set", {"text": "x" * 5000})


class TestMemory(unittest.TestCase):
    def test_add_list_search_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "memory.json"
            fact = add_fact(store, "Phil keeps notes in Documents\\notes",
                            category="desktop", approved=True, approved_by="phil",
                            source="phil")
            self.assertEqual(len(list_facts(store)), 1)
            self.assertEqual(search_facts(store, "notes")[0]["id"], fact["id"])
            self.assertTrue(delete_fact(store, fact["id"]))
            self.assertEqual(list_facts(store), [])

    def test_denied_categories_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "memory.json"
            with self.assertRaises(ValueError):
                add_fact(store, "dad's cancer treatment schedule",
                         category="family", approved=True, approved_by="phil")
            with self.assertRaises(ValueError):
                add_fact(store, "the court date for the case",
                         category="legal", approved=True, approved_by="phil")
            with self.assertRaises(ValueError):
                add_fact(store, "afterpay balance is overdue",
                         category="money", approved=True, approved_by="phil")

    def test_unapproved_fact_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "memory.json"
            with self.assertRaises(ValueError):
                add_fact(store, "Phil likes rice and beans",
                         category="food", approved=False, approved_by="phil")

    def test_snapshot_expires(self):
        snapshot = build_snapshot(foreground_app="notepad.exe", open_windows=["Notepad"],
                                  recent_files=[], screenshot_token="frame-1")
        self.assertFalse(snapshot_expired(snapshot, now=snapshot["created_at"]))
        self.assertTrue(snapshot_expired(snapshot, now=snapshot["expires_at"] + 1))


class TestSkills(unittest.TestCase):
    def test_list_and_load_built_in_skills(self):
        names = list_skills()
        self.assertIn("notepad_write_verify.json", names)
        self.assertIn("calculator_type_verify.json", names)
        skill = load_skill("notepad_write_verify.json")
        self.assertEqual(skill["name"], "notepad_write_verify")
        self.assertFalse(skill["destructive"])

    def test_run_skill_with_verified_receipts(self):
        skill = load_skill("calculator_type_verify.json")

        def executor(action):
            return {"status": "completed", "error": None,
                    "data": {"evidence": {"verified": True, "kind": action["name"], "detail": {}}}}

        result = run_skill(skill, [{"capability": "filesystem"}, {"capability": "window"},
                                   {"capability": "accessibility"}], executor)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["completed_steps"], [1, 2, 3])

    def test_run_skill_fails_on_unverified_receipt(self):
        skill = load_skill("notepad_write_verify.json")

        def lying(action):
            return {"status": "completed", "error": None, "data": {}}

        result = run_skill(skill, [{"capability": "filesystem"}], lying)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["failed_step"], 1)

    def test_run_skill_blocked_without_grant(self):
        skill = load_skill("notepad_write_verify.json")
        result = run_skill(skill, [{"capability": "window"}], lambda action: {})
        self.assertEqual(result["outcome"], "blocked")



if __name__ == "__main__":
    unittest.main()
