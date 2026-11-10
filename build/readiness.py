"""Local speech, transcription, synthesis and camera checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
def _run(args: list[str], timeout: int = 120) -> None:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result.stderr[-600:]}")


def synthesize(text: str, output: Path | None = None) -> None:
    executable = shutil.which("say")
    if not executable:
        raise RuntimeError("macOS say is unavailable; configure a TTS adapter on this OS.")
    # A file avoids interpreting text beginning with '-' as command options.
    with tempfile.TemporaryDirectory(prefix="so101-tts-") as directory:
        source = Path(directory) / "reply.txt"
        source.write_text(text)
        args = [executable, "-v", os.getenv("TTS_VOICE", "Samantha"), "-f", str(source)]
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            args += ["-o", str(output)]
        _run(args)


def resolve_model(model: Path | None = None) -> Path:
    path = model or Path(os.getenv("WHISPER_MODEL_PATH", "models/whisper/ggml-tiny.en-q5_1.bin"))
    path = path.expanduser()
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise RuntimeError(f"Whisper model file not found: {path}")
    return path


def transcribe(audio: Path, model: Path | None = None) -> dict:
    executable = shutil.which("whisper-cli")
    if not executable:
        raise RuntimeError("whisper-cli is required for this local STT adapter.")
    if not audio.is_file():
        raise RuntimeError(f"Audio file not found: {audio}")
    model_path = resolve_model(model)
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="so101-stt-") as directory:
        wav = audio
        prefix = Path(directory) / "transcript"
        converted = not whisper_ready_wav(audio)
        if converted:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("ffmpeg is required to convert this audio format.")
            wav = Path(directory) / "input.wav"
            _run([ffmpeg, "-nostdin", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
        _run([executable, "-m", str(model_path), "-f", str(wav), "-l", "en", "-otxt", "-of", str(prefix), "-np", "-nt", "--no-gpu"])
        transcript_path = prefix.with_suffix(".txt")
        if not transcript_path.is_file():
            raise RuntimeError("Whisper did not produce a transcript file")
        text = transcript_path.read_text().strip()
    return {"text": text, "elapsed_seconds": round(time.perf_counter() - start, 3),
            "backend": "local whisper.cpp", "inference_device": "cpu", "audio_converted": converted}


def whisper_ready_wav(audio: Path) -> bool:
    """Inspect format, without resampling or changing the source recording."""
    try:
        with wave.open(str(audio), 'rb') as source:
            return (source.getnchannels() == 1 and source.getframerate() == 16000
                    and source.getsampwidth() == 2 and source.getcomptype() == 'NONE'
                    and source.getnframes() > 0)
    except (wave.Error, EOFError):
        return False


def record(seconds: float = 4) -> Path:
    if not 1 <= seconds <= 15:
        raise ValueError("Recording length must be between 1 and 15 seconds")
    import sounddevice as sd
    import soundfile as sf

    directory = ROOT / ".cache/audio"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"command-{time.time_ns()}.wav"
    print(f"Recording {seconds:g} seconds now...", flush=True)
    audio = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="int16")
    sd.wait()
    sf.write(output, audio, 16000, subtype="PCM_16")
    return output


def speech_smoke() -> dict:
    """Synthetic local audio test, with no microphone capture or speaker playback."""
    phrase = "Move left. Move right. Move up. Move down. Stop."
    with tempfile.TemporaryDirectory(prefix="so101-speech-test-") as directory:
        audio = Path(directory) / "commands.aiff"
        synthesize(phrase, audio)
        result = transcribe(audio)
    normalized = re.sub(r"[^a-z ]", " ", result["text"].lower())
    normalized = " ".join(normalized.split())
    commands = ["move left", "move right", "move up", "move down", "stop"]
    matched = {command: command in normalized for command in commands}
    return {"source": "synthetic English speech; not a microphone accuracy test", "expected": phrase, **result, "commands_recognized": matched, "passed": all(matched.values()), "robot_calls": 0}


def camera_snapshot(index: int) -> dict:
    import cv2

    output = ROOT / ".cache/camera" / f"snapshot-{time.time_ns()}.jpg"
    output.parent.mkdir(parents=True, exist_ok=True)
    camera = cv2.VideoCapture(index)
    try:
        if not camera.isOpened():
            raise RuntimeError("Camera could not be opened")
        ok, frame = camera.read()
        if not ok or frame is None or not cv2.imwrite(str(output), frame):
            raise RuntimeError("Camera did not produce a writable frame")
        return {"image": str(output), "width": frame.shape[1], "height": frame.shape[0], "cloud_calls": 0}
    finally:
        camera.release()
