import contextlib
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build"))
sys.path.insert(0, str(ROOT / "vendor/cyberwave_nl_arm"))
import agent_preview


class AgentPreviewTests(unittest.TestCase):
    def test_local_voice_reaches_existing_planner_and_spoken_reply(self):
        plan = SimpleNamespace(say="Previewing a left move.", actions=[])
        result = SimpleNamespace(ok=True, plan=plan)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-placeholder"}), \
             patch("builtins.input", side_effect=["", "quit"]), \
             patch.object(agent_preview.readiness, "resolve_model"), \
             patch.object(agent_preview.readiness, "record", return_value=Path("mock.wav")), \
             patch.object(agent_preview.readiness, "transcribe", return_value={"text": "move left"}), \
             patch.object(agent_preview.readiness, "synthesize") as speak, \
             patch.object(agent_preview, "plan_from_utterance", return_value=result) as planner, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(agent_preview.run(voice=True, speak=True), 0)
            planner.assert_called_once_with("move left")
            speak.assert_called_once_with("Previewing a left move.")
            self.assertIn('"executed_actions": 0', output.getvalue())

    def test_missing_camera_frame_never_reaches_planner(self):
        camera = Mock()
        camera.grab_frame_b64.return_value = None
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-placeholder"}), \
             patch("builtins.input", side_effect=["pick up the red ball", "quit"]), \
             patch("vision.open_camera_from_env", return_value=camera), \
             patch.object(agent_preview, "plan_from_utterance_with_image") as planner, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(agent_preview.run(vision=True), 0)
            planner.assert_not_called()
            camera.close.assert_called_once()
            self.assertIn("camera frame is unavailable", output.getvalue())


if __name__ == "__main__":
    unittest.main()
