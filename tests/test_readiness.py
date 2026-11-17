"""Exercise real WAV inspection with stubbed Whisper/ffmpeg processes."""
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import readiness


class TranscriptionFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.model = self.root/'model.bin'
        self.model.write_bytes(b'test model')

    def wav(self, name='speech.wav', rate=16000, channels=1, width=2):
        path = self.root/name
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(width)
            audio.setframerate(rate)
            audio.writeframes(b'\0' * 400 * channels * width)
        return path

    def run_tool(self, args):
        if args[0] == 'whisper-cli':
            Path(args[args.index('-of')+1]).with_suffix('.txt').write_text('halfway right')

    def test_ready_recording_passes_original_bytes_directly_to_whisper(self):
        source = self.wav()
        before = source.read_bytes()
        with patch.object(readiness, 'which', side_effect=lambda name: 'whisper-cli' if name=='whisper-cli' else None) as which, \
             patch.object(readiness, '_run', side_effect=self.run_tool) as run:
            result = readiness.transcribe(source, self.model)
        self.assertFalse(result['audio_converted'])
        self.assertEqual(result['text'], 'halfway right')
        self.assertEqual(run.call_count, 1)
        self.assertIn('--no-gpu', run.call_args.args[0])
        self.assertEqual(result['inference_device'], 'cpu')
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index('-f')+1], str(source))
        which.assert_called_once_with('whisper-cli')
        self.assertEqual(source.read_bytes(), before)

    def test_nonmatching_audio_keeps_conversion_fallback(self):
        for options in ({'rate':48000}, {'channels':2}, {'width':3}):
            with self.subTest(options=options):
                source=self.wav(**options)
                with patch.object(readiness,'which',side_effect=lambda name:name), \
                     patch.object(readiness,'_run',side_effect=self.run_tool) as run:
                    result=readiness.transcribe(source,self.model)
                self.assertTrue(result['audio_converted'])
                self.assertEqual([c.args[0][0] for c in run.call_args_list],['ffmpeg','whisper-cli'])
                self.assertNotEqual(run.call_args.args[0][run.call_args.args[0].index('-f')+1],str(source))

    def test_unsupported_audio_explains_conversion_dependency(self):
        source=self.wav(rate=48000)
        with patch.object(readiness,'which',side_effect=lambda name:'whisper-cli' if name=='whisper-cli' else None), \
             patch.object(readiness,'_run') as run, \
             self.assertRaisesRegex(RuntimeError,'ffmpeg is required to convert'):
            readiness.transcribe(source,self.model)
        run.assert_not_called()

    def test_malformed_and_empty_audio_are_not_treated_as_ready(self):
        source=self.root/'broken.wav'
        source.write_bytes(b'invalid audio')
        self.assertFalse(readiness.whisper_ready_wav(source))
        empty=self.wav()
        with wave.open(str(empty),'wb') as audio:
            audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
        self.assertFalse(readiness.whisper_ready_wav(empty))


if __name__=='__main__':unittest.main()
