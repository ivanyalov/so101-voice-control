import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import command_preview


class CommandPreviewTests(unittest.TestCase):
    def test_reported_phrase_keeps_order_and_does_not_invent_top_target(self):
        result = command_preview.preview("Move to the left then rise all the way to the top")
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["proposed_actions"], [
            {"type": "jog_joint", "joint": "shoulder_pan", "direction": "left", "steps": 1},
            {"type": "request_pose", "pose": "top", "target": None},
        ])
        self.assertIsNone(result["parameter_changes"][0]["before"])
        self.assertIsNone(result["parameter_changes"][0]["after"])
        self.assertIn("direction_sign['left']", result["code_preview"][0])
        self.assertTrue(result["unresolved"])
        self.assertEqual(result["executed_actions"], 0)

    def test_unknown_or_negated_clause_rejects_whole_sequence(self):
        for text in ("do not move left", "don't move up", "move left then pick the ball",
                     "move left ten times", "move right 30 degrees", "what is left", ""):
            with self.subTest(text=text):
                result = command_preview.preview(text)
                self.assertEqual(result["status"], "unsupported_command")
                self.assertEqual(result["proposed_actions"], [])
                self.assertEqual(result["parameter_changes"], [])
                self.assertEqual(result["code_preview"], [])

    def test_basic_sequence_and_stop(self):
        result = command_preview.preview("MOVE RIGHT! Then move up a little.")
        self.assertEqual([a["joint"] for a in result["proposed_actions"]], ["shoulder_pan", "shoulder_lift"])
        stop = command_preview.preview("stop")
        self.assertEqual(stop["proposed_actions"], [{"type": "stop"}])
        self.assertEqual(stop["executed_actions"], 0)
        self.assertEqual(stop["parameter_changes"], [])
