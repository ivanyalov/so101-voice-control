import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import voice_toggle
from turn_log import TurnLog


class TurnLogTests(unittest.TestCase):
    def run_turn(self, processor, directory):
        path = Path(directory) / "session.jsonl"
        log = TurnLog(path)
        recorder = Mock(recording=True)
        recorder.stop.return_value = Path("command.wav")
        session = voice_toggle.Session(recorder, processor=processor, log=log)
        with patch.object(voice_toggle.readiness, "transcribe", return_value={"text": "move elbow up"}), \
             contextlib.redirect_stdout(io.StringIO()):
            session.toggle()
            session.worker.join(timeout=2)
            self.assertFalse(session.worker.is_alive())
            recorder.recording = False
            session.poll()
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_saved_events_link_audio_transcript_and_complete_result(self):
        plan = {"planner": "grok", "parameter_changes": [{"joint": "elbow_flex", "target": -10}], "executed_actions": 0}
        with tempfile.TemporaryDirectory() as directory:
            records = self.run_turn(lambda text: plan, directory)
            if os.name != "nt":
                self.assertEqual(Path(directory, "session.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual([r["event"] for r in records], ["recording_stopped", "transcript", "planner_started", "result"])
        self.assertEqual(records[0]["audio_file"], "command.wav")
        result = records[-1]["result"]
        self.assertEqual(result["text"], "move elbow up")
        self.assertEqual(result["parameter_changes"], plan["parameter_changes"])
        self.assertIn("command_processing_elapsed_seconds", result)
        self.assertNotIn("planning_elapsed_seconds", result)
        self.assertEqual(result["executed_actions"], 0)
        self.assertTrue(all(r["timestamp"].endswith("+00:00") for r in records))

    def test_provider_failure_keeps_transcript_and_error_in_log(self):
        with tempfile.TemporaryDirectory() as directory:
            records = self.run_turn(Mock(side_effect=RuntimeError("Grok HTTP 401")), directory)
        self.assertEqual(records[1]["result"]["text"], "move elbow up")
        self.assertEqual(records[-1]["event"], "planner_failed")
        self.assertEqual(records[-1]["error"], "Grok HTTP 401")
