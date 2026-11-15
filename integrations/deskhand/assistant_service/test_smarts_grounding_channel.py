"""Tests: grounding registry (Batch 3, v100.008).

B2-D (100.030): the deskhand_channel parse/validate path was removed
(test-only; zero production callers), so the channel tests were
removed with it. Grounding tests remain."""
import unittest
import uuid

from deskhand_grounding import GroundingRegistry

TASK = str(uuid.uuid4())
TURN = str(uuid.uuid4())





class TestGrounding(unittest.TestCase):
    def setUp(self):
        self.registry = GroundingRegistry()
        self.elements = [
            {"name": "New", "kind": "button",
             "bbox": {"x_min": 0.1, "y_min": 0.1, "x_max": 0.2, "y_max": 0.2}},
            {"name": "Address", "kind": "edit",
             "bbox": {"x_min": 0.3, "y_min": 0.3, "x_max": 0.6, "y_max": 0.4}},
        ]

    def test_register_and_resolve(self):
        observation = self.registry.register(
            task_id=TASK, turn_id=TURN, screenshot_token="frame-1",
            elements=self.elements, now=1000.0)
        self.assertEqual(observation["element_count"], 2)
        self.assertEqual(observation["expires_at"], 1015.0)
        element_id = observation["elements"][0]["element_id"]
        resolved = self.registry.resolve(task_id=TASK, screenshot_token="frame-1",
                                         element_id=element_id, now=1001.0)
        self.assertAlmostEqual(resolved["x"], 0.15)
        self.assertAlmostEqual(resolved["y"], 0.15)

    def test_expired_frame_requires_reobserve(self):
        observation = self.registry.register(
            task_id=TASK, turn_id=TURN, screenshot_token="frame-1",
            elements=self.elements, now=1000.0)
        element_id = observation["elements"][0]["element_id"]
        with self.assertRaises(ValueError):
            self.registry.resolve(task_id=TASK, screenshot_token="frame-1",
                                  element_id=element_id, now=1015.0)

    def test_wrong_token_and_unknown_element(self):
        observation = self.registry.register(
            task_id=TASK, turn_id=TURN, screenshot_token="frame-1",
            elements=self.elements, now=1000.0)
        element_id = observation["elements"][0]["element_id"]
        with self.assertRaises(ValueError):
            self.registry.resolve(task_id=TASK, screenshot_token="frame-2",
                                  element_id=element_id, now=1001.0)
        with self.assertRaises(ValueError):
            self.registry.resolve(task_id=TASK, screenshot_token="frame-1",
                                  element_id=str(uuid.uuid4()), now=1001.0)

    def test_re_register_replaces_old_observation(self):
        first = self.registry.register(
            task_id=TASK, turn_id=TURN, screenshot_token="frame-1",
            elements=self.elements, now=1000.0)
        self.registry.register(
            task_id=TASK, turn_id=TURN, screenshot_token="frame-2",
            elements=self.elements, now=1001.0)
        with self.assertRaises(ValueError):
            self.registry.resolve(task_id=TASK, screenshot_token="frame-1",
                                  element_id=first["elements"][0]["element_id"],
                                  now=1002.0)

    def test_invalid_bbox_rejected(self):
        bad = [{"name": "X", "kind": "button",
                "bbox": {"x_min": 0.9, "y_min": 0.1, "x_max": 0.2, "y_max": 0.2}}]
        with self.assertRaises(ValueError):
            self.registry.register(task_id=TASK, turn_id=TURN,
                                   screenshot_token="f", elements=bad, now=1000.0)



if __name__ == "__main__":
    unittest.main()
