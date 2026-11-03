"""Local speech and non-invasive hardware preparation. No servo writes."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")


def list_ports() -> list[dict]:
    """Enumerate OS port metadata only. Never open or probe a serial port."""
    from serial.tools import list_ports as serial_ports

    return [
        {"device": p.device, "description": p.description, "vid": p.vid, "pid": p.pid}
        for p in sorted(serial_ports.comports(), key=lambda item: item.device)
    ]


def profile_report(path: Path) -> dict:
    """Validate supplied configuration; this does not establish physical readiness."""
    data = json.loads(path.read_text())
    missing = []
    if data.get("motor_setup_confirmed") is not True:
        missing.append("motor_setup_confirmed")
    for field in ("port", "robot_id", "driver_version", "calibration_file"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            missing.append(field)
    calibration = data.get("calibration_file")
    if isinstance(calibration, str) and calibration:
        calibration_path = Path(calibration).expanduser()
        if not calibration_path.is_absolute():
            calibration_path = ROOT / calibration_path
        if not calibration_path.is_file():
            missing.append("calibration_file does not exist")
    joints = data.get("joints", {})
    if not isinstance(joints, dict):
        joints = {}
    ids = []
    for name in JOINTS:
        joint = joints.get(name, {})
        if not isinstance(joint, dict):
            missing.append(f"joints.{name}")
            continue
        motor_id = joint.get("id")
        if type(motor_id) is not int or not 1 <= motor_id <= 253:
            missing.append(f"joints.{name}.id")
        else:
            ids.append(motor_id)
        if joint.get("unit") not in ("degrees", "normalized_0_100", "normalized_m100_100"):
            missing.append(f"joints.{name}.unit")
        bounds = [joint.get("min"), joint.get("max")]
        if not all(type(v) in (int, float) and math.isfinite(v) for v in bounds) or bounds[0] >= bounds[1]:
            missing.append(f"joints.{name}.bounds")
    if len(ids) != len(set(ids)):
        missing.append("duplicate motor IDs")
    return {
        "configuration_complete": not missing,
        "physical_validation_performed": False,
        "missing_or_invalid": missing,
        "servo_connection_opened": False,
    }


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
    executable, ffmpeg = shutil.which("whisper-cli"), shutil.which("ffmpeg")
    if not executable or not ffmpeg:
        raise RuntimeError("whisper-cli and ffmpeg are required for this local STT adapter.")
    if not audio.is_file():
        raise RuntimeError(f"Audio file not found: {audio}")
    model_path = resolve_model(model)
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="so101-stt-") as directory:
        wav = Path(directory) / "input.wav"
        prefix = Path(directory) / "transcript"
        _run([ffmpeg, "-nostdin", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
        _run([executable, "-m", str(model_path), "-f", str(wav), "-l", "en", "-otxt", "-of", str(prefix), "-np", "-nt"])
        transcript_path = prefix.with_suffix(".txt")
        if not transcript_path.is_file():
            raise RuntimeError("Whisper did not produce a transcript file")
        text = transcript_path.read_text().strip()
    return {"text": text, "elapsed_seconds": round(time.perf_counter() - start, 3), "backend": "local whisper.cpp"}


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
    from vision import open_camera

    output = ROOT / ".cache/camera" / f"snapshot-{time.time_ns()}.jpg"
    output.parent.mkdir(parents=True, exist_ok=True)
    with open_camera(index) as camera:
        frame = camera.grab_frame()
        if frame is None or not cv2.imwrite(str(output), frame):
            raise RuntimeError("Camera did not produce a writable frame")
        return {"image": str(output), "width": frame.shape[1], "height": frame.shape[0], "cloud_calls": 0, "robot_calls": 0}
