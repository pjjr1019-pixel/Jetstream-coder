"""Tests: evidence (Batch 3, v100.008).

B2-D (100.030): deskhand_plan and deskhand_recovery were removed
(zero production callers), so their tests were removed with them.
Evidence tests remain (deskhand_evidence.py is still live)."""
import unittest

from deskhand_evidence import build_evidence, downgrade_status, receipt_is_verified


class TestEvidence(unittest.TestCase):
    def test_launch_evidence(self):
        evidence = build_evidence("filesystem.launch", {"pid": 123, "process_name": "notepad.exe"})
        self.assertTrue(evidence["verified"])
        evidence = build_evidence("filesystem.launch", {"process_name": "notepad.exe"})
        self.assertFalse(evidence["verified"])

    def test_write_evidence_needs_read_back_match(self):
        self.assertTrue(build_evidence("filesystem.create",
                                       {"read_back_match": True, "length": 12})["verified"])
        self.assertFalse(build_evidence("filesystem.create",
                                        {"read_back_match": False, "length": 12})["verified"])

    def test_downgrade_completed_without_evidence(self):
        self.assertEqual(downgrade_status("completed", {"verified": False}), "uncertain")
        self.assertEqual(downgrade_status("completed", {"verified": True}), "completed")
        self.assertEqual(downgrade_status("failed", {"verified": False}), "failed")

    def test_receipt_is_verified(self):
        receipt = {"status": "completed", "data": {"evidence": {"verified": True}}}
        self.assertTrue(receipt_is_verified(receipt))
        lying = {"status": "completed", "data": {}}
        self.assertFalse(receipt_is_verified(lying))

    def test_command_run_only_exit_zero_is_verified(self):
        # B2-D M6: any observed exit code used to count as verified.
        self.assertTrue(build_evidence(
            "command.run", {"exit_code": 0, "output": "ok"})["verified"])
        self.assertFalse(build_evidence(
            "command.run", {"exit_code": 1, "output": "boom"})["verified"])
        self.assertFalse(build_evidence(
            "command.run", {"exit_code": -1, "output": ""})["verified"])
        self.assertEqual(build_evidence(
            "command.run", {"exit_code": 1, "output": "boom"})["detail"]["exit_code"], 1)



if __name__ == "__main__":
    unittest.main()
