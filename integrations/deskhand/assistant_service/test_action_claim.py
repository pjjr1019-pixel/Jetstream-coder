"""Tests for deskhand_action_claim.py stale-claim recovery (B2-D H12).

A crash between claim and settle used to leave a claim "claimed"
forever: resolve() only accepted "uncertain", claim()/settle() refused
"claimed", and only manual file deletion recovered the task ID. Now a
claim provably older than CLAIM_STALE_SECS can be reclaimed/retried or
operator-resolved, while a fresh "claimed" claim still refuses exactly
as before (it may be in flight).

Run: python3 test_action_claim.py
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deskhand_action_claim import (
    CLAIM_STALE_SECS,
    ClaimStateError,
    ClaimStore,
    canonical_action_id,
)

ACTION = {"kind": "exec", "command": "echo hi"}


def _age_claim(store, task_id, age_seconds):
    """Backdate a claim's timestamps by age_seconds (simulates a crash
    long ago)."""
    import json
    from deskhand_action_claim import _claim_filename
    path = store.store_dir / _claim_filename(task_id)
    claim = json.loads(path.read_text(encoding="utf-8"))
    old = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds))
    stamp = old.isoformat(timespec="seconds")
    claim["ts_claimed"] = stamp
    claim["ts_updated"] = stamp
    path.write_text(json.dumps(claim), encoding="utf-8")


class StaleClaimTests(unittest.TestCase):
    def test_fresh_claimed_refuses_reclaim(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ClaimStore(folder)
            claim, owned = store.claim("t1", ACTION, route="r")
            self.assertTrue(owned)
            again, owned = store.claim("t1", ACTION, route="r")
            self.assertFalse(owned)
            self.assertEqual(again["state"], "claimed")

    def test_stale_claimed_reclaims(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ClaimStore(folder)
            store.claim("t1", ACTION, route="r")
            _age_claim(store, "t1", CLAIM_STALE_SECS + 60)
            claim, owned = store.claim("t1", ACTION, route="r")
            self.assertTrue(owned)
            self.assertEqual(claim["state"], "claimed")
            self.assertIn("stale claimed",
                          claim["history"][-1]["detail"])

    def test_stale_claimed_settles_after_reclaim(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ClaimStore(folder)
            store.claim("t1", ACTION, route="r")
            _age_claim(store, "t1", CLAIM_STALE_SECS + 60)
            claim, owned = store.claim("t1", ACTION, route="r")
            settled = store.settle("t1", claim["action_id"], "executed")
            self.assertEqual(settled["state"], "executed")

    def test_resolve_stale_claimed_allowed_fresh_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ClaimStore(folder)
            claim, _ = store.claim("t1", ACTION, route="r")
            action_id = claim["action_id"]
            with self.assertRaises(ClaimStateError):
                store.resolve("t1", action_id, "failed", resolved_by="op")
            _age_claim(store, "t1", CLAIM_STALE_SECS + 60)
            resolved = store.resolve("t1", action_id, "failed",
                                     resolved_by="op", detail="never ran")
            self.assertEqual(resolved["state"], "failed")

    def test_resolve_uncertain_still_works(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ClaimStore(folder)
            claim, _ = store.claim("t1", ACTION, route="r")
            store.settle("t1", claim["action_id"], "uncertain")
            resolved = store.resolve("t1", claim["action_id"], "executed",
                                     resolved_by="op")
            self.assertEqual(resolved["state"], "executed")

    def test_unparseable_timestamps_are_not_stale(self):
        # Fail closed: age unproven, so the claim is NOT stale.
        import json
        from deskhand_action_claim import _claim_filename
        with tempfile.TemporaryDirectory() as folder:
            store = ClaimStore(folder)
            store.claim("t1", ACTION, route="r")
            path = store.store_dir / _claim_filename("t1")
            claim = json.loads(path.read_text(encoding="utf-8"))
            claim["ts_updated"] = "not-a-date"
            claim["ts_claimed"] = "not-a-date"
            path.write_text(json.dumps(claim), encoding="utf-8")
            _, owned = store.claim("t1", ACTION, route="r")
            self.assertFalse(owned)

    def test_executed_never_reclaims_even_when_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ClaimStore(folder)
            claim, _ = store.claim("t1", ACTION, route="r")
            store.settle("t1", claim["action_id"], "executed")
            _age_claim(store, "t1", CLAIM_STALE_SECS + 3600)
            _, owned = store.claim("t1", ACTION, route="r")
            self.assertFalse(owned)


if __name__ == "__main__":
    unittest.main()
