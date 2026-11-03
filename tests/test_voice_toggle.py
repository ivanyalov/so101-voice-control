import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import sounddevice as sd
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build"))
import voice_toggle


@dataclass(frozen=True)
class Key:
    vk: int
    char: str = ""


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.keys = SimpleNamespace(ctrl_l=Key(59), ctrl_r=Key(62), alt_l=Key(58), alt_r=Key(61),
                                    cmd_l=Key(55), cmd_r=Key(54), shift_l=Key(56), shift_r=Key(60))
        self.emit = Mock()
        self.shortcut = voice_toggle.Shortcut(SimpleNamespace(Key=self.keys), self.emit, mac=True)

    def test_repeated_r_with_held_modifiers_toggles_exactly_twice(self):
        self.shortcut.press(self.keys.ctrl_l)
        self.shortcut.press(self.keys.cmd_l)
        self.shortcut.press(self.keys.shift_l)
        # Shift and repeat keydowns must not change physical-key matching.
        for _ in range(10):
            self.shortcut.press(Key(15, "R"))
        self.emit.assert_called_once()
        self.shortcut.release(Key(15, "r"))
        self.shortcut.press(Key(15, "к"))
        self.assertEqual(self.emit.call_count, 2)

    def test_either_modifier_side_and_different_release_order(self):
        for ctrl, cmd, shift in ((self.keys.ctrl_l, self.keys.cmd_r, self.keys.shift_r),
                                 (self.keys.ctrl_r, self.keys.cmd_l, self.keys.shift_l)):
            self.shortcut.press(cmd)
            self.shortcut.press(shift)
            self.shortcut.press(ctrl)
            self.shortcut.press(Key(15))
            self.shortcut.release(ctrl)
            self.shortcut.release(cmd)
            self.shortcut.release(shift)
            self.shortcut.release(Key(15))
        self.assertEqual(self.emit.call_count, 2)

    def test_plain_r_wrong_key_modifier_only_and_injected_do_not_toggle(self):
        self.shortcut.press(Key(15, "r"))
        self.shortcut.release(Key(15))
        self.shortcut.press(self.keys.ctrl_l)
        self.shortcut.press(self.keys.cmd_l)
        self.shortcut.press(self.keys.shift_l)
        self.shortcut.press(Key(17, "t"))
        self.shortcut.press(Key(15), injected=True)
        self.emit.assert_not_called()

    def test_option_combinations_never_activate(self):
        self.shortcut.press(self.keys.ctrl_l)
        self.shortcut.press(self.keys.shift_l)
        self.shortcut.press(self.keys.alt_l)
        self.shortcut.press(Key(15, "R"))
        self.shortcut.release(Key(15))
        self.shortcut.press(self.keys.cmd_l)
        self.shortcut.press(Key(15, "R"))
        self.emit.assert_not_called()


class VoiceToggleTests(unittest.TestCase):
    def test_missing_permission_falls_back_to_enter_without_opening_mic(self):
        keyboard = SimpleNamespace(Listener=Mock(IS_TRUSTED=False))
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(voice_toggle.readiness, "ROOT", Path(directory)), \
             patch.dict(sys.modules, {"pynput": SimpleNamespace(keyboard=keyboard)}), \
             patch.object(voice_toggle.readiness, "resolve_model"), \
             patch("builtins.input", return_value="quit"), \
             patch("sounddevice.InputStream") as microphone, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(voice_toggle.run(planner="basic"), 0)
        microphone.assert_not_called()
        keyboard.Listener.assert_not_called()
        self.assertIn("Using Enter instead", output.getvalue())
        self.assertIn("Enter: start recording", output.getvalue())

    def test_voice_to_parameter_preview_integration(self):
        recorder = Mock(recording=True)
        recorder.stop.return_value = Path("mock.wav")
        session = voice_toggle.Session(recorder)
        with patch.object(voice_toggle.readiness, "transcribe", return_value={"text": "Move to the left then rise all the way to the top"}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            session.toggle()
            session.worker.join(timeout=2)
            recorder.recording = False
            session.poll()
        self.assertIn('"parameter": "shoulder_pan.position"', output.getvalue())
        self.assertIn('"pose": "top"', output.getvalue())
        self.assertIn('"after": null', output.getvalue())
        self.assertIn('"executed_actions": 0', output.getvalue())

    def test_two_presses_capture_then_transcribe_without_speech(self):
        stream = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(voice_toggle.readiness, "ROOT", Path(directory)), \
             patch("sounddevice.InputStream", return_value=stream) as factory, \
             patch.object(voice_toggle.readiness, "transcribe", return_value={"text": "run a slow servo check"}) as stt, \
             patch.object(voice_toggle.readiness, "synthesize") as tts, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            recorder = voice_toggle.Recorder()
            session = voice_toggle.Session(recorder)
            factory.assert_not_called()
            session.toggle()
            self.assertTrue(recorder.recording)
            factory.call_args.kwargs["callback"](np.ones((8000, 1), dtype=np.int16), 8000, None, None)
            stt.assert_not_called()
            session.toggle()
            self.assertFalse(recorder.recording)
            session.worker.join(timeout=2)
            session.poll()
            self.assertFalse(session.busy)
            stream.stop.assert_called_once()
            stream.close.assert_called_once()
            stt.assert_called_once()
            audio, rate = sf.read(stt.call_args.args[0])
            self.assertEqual((len(audio), rate), (8000, 16000))
            self.assertIn('"text": "run a slow servo check"', output.getvalue())
            self.assertIn('"executed_actions": 0', output.getvalue())
            tts.assert_not_called()

    def test_recording_limit_bounds_audio_and_stops_callback(self):
        recorder = voice_toggle.Recorder(max_seconds=1)
        with self.assertRaises(sd.CallbackStop):
            recorder._capture(np.zeros((20000, 1), dtype=np.int16), 20000, None, None)
        self.assertEqual(recorder.samples, 16000)
        self.assertEqual(len(recorder.chunks[0]), 16000)
        self.assertTrue(recorder.full.is_set())

    def test_limit_event_uses_same_stop_and_transcription_path(self):
        recorder = Mock(recording=True)
        recorder.full.is_set.return_value = True
        recorder.stop.return_value = Path("mock.wav")
        session = voice_toggle.Session(recorder)
        with patch.object(voice_toggle.readiness, "transcribe", return_value={"text": "move left"}) as stt, \
             contextlib.redirect_stdout(io.StringIO()):
            session.poll()
            session.worker.join(timeout=2)
            recorder.recording = False
            session.poll()
        recorder.stop.assert_called_once()
        stt.assert_called_once_with(Path("mock.wav"))
        self.assertFalse(session.busy)

    def test_toggle_during_transcription_does_not_open_microphone(self):
        recorder = Mock()
        session = voice_toggle.Session(recorder)
        session.busy = True
        with contextlib.redirect_stdout(io.StringIO()):
            session.toggle()
        recorder.start.assert_not_called()
        recorder.stop.assert_not_called()

    def test_failed_microphone_start_closes_stream(self):
        stream = Mock()
        stream.start.side_effect = RuntimeError("permission denied")
        recorder = voice_toggle.Recorder()
        with patch("sounddevice.InputStream", return_value=stream), self.assertRaises(RuntimeError):
            recorder.start()
        stream.close.assert_called_once()
        self.assertFalse(recorder.recording)

    def test_quitting_discards_audio_without_saving_or_transcribing(self):
        recorder = voice_toggle.Recorder()
        stream = Mock()
        with patch("sounddevice.InputStream", return_value=stream), \
             patch("soundfile.write") as write, \
             patch.object(voice_toggle.readiness, "transcribe") as stt:
            recorder.start()
            recorder._capture(np.ones((8000, 1), dtype=np.int16), 8000, None, None)
            recorder.cancel()
        self.assertFalse(recorder.recording)
        self.assertEqual(recorder.chunks, [])
        stream.close.assert_called_once()
        write.assert_not_called()
        stt.assert_not_called()

    def test_failed_transcription_returns_to_ready(self):
        recorder = Mock(recording=True)
        recorder.stop.return_value = Path("mock.wav")
        session = voice_toggle.Session(recorder)
        with patch.object(voice_toggle.readiness, "transcribe", side_effect=RuntimeError("test failure")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            session.toggle()
            session.worker.join(timeout=2)
            recorder.recording = False
            session.poll()
        self.assertFalse(session.busy)
        self.assertIn("Transcription failed", output.getvalue())
        self.assertIn("READY", output.getvalue())

    def test_short_recording_does_not_reach_whisper(self):
        recorder = voice_toggle.Recorder()
        recorder.stream = Mock()
        with self.assertRaisesRegex(ValueError, "too short"):
            recorder.stop()
        self.assertFalse(recorder.recording)


if __name__ == "__main__":
    unittest.main()
