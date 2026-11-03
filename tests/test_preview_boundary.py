"""Check that the development launcher never enables robot execution."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dev", ROOT / "build" / "dev.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


class PreviewBoundaryTests(unittest.TestCase):
    def test_preview_forces_dry_run_even_when_environment_requests_live(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-placeholder", "CW_MODE": "live"}), \
             patch.object(dev, "load_environment"), \
             patch.object(dev.runpy, "run_path") as launch, \
             patch.object(dev.sys, "argv", []), \
             contextlib.redirect_stdout(io.StringIO()):
            dev.preview()
            self.assertIn("--dry-run", dev.sys.argv)
            self.assertNotIn("--execute", dev.sys.argv)
            self.assertEqual(os.environ["CW_MODE"], "simulation")
            launch.assert_called_once()

    def test_missing_key_does_not_launch_upstream(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(dev, "load_environment"), \
             patch.object(dev.runpy, "run_path") as launch:
            with self.assertRaises(SystemExit):
                dev.preview()
            launch.assert_not_called()

    def test_upstream_accepts_no_object_response_without_actions(self):
        from planner import parse_plan_json

        data = (ROOT / "inputs/plans/2026-10-27_object-not-visible.json").read_text()
        plan, error = parse_plan_json(data)
        self.assertIsNone(error)
        self.assertEqual(plan.actions, [])

    def test_upstream_rejects_unknown_action(self):
        from planner import parse_plan_json

        plan, error = parse_plan_json('{"actions":[{"type":"invented_action"}]}')
        self.assertIsNone(plan)
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
