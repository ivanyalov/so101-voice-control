"""Development entry point for the pinned Cyberwave example.

Provides diagnostics, local speech/camera checks and explicit robot integration.
Hardware execution requires --robot plus --execute; mock mode cannot execute.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "vendor" / "cyberwave_nl_arm"
sys.path.insert(0, str(UPSTREAM))


def load_environment() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)


def doctor() -> int:
    """Check dependencies without credentials, device capture or network calls."""
    print(f"Python: {sys.version.split()[0]}")
    print(f"Environment: {sys.prefix}")
    modules = {
        "cyberwave": "cyberwave",
        "anthropic": "anthropic",
        "httpx": "httpx",
        "numpy": "numpy",
        "opencv-python-headless": "cv2",
        "sounddevice": "sounddevice",
        "soundfile": "soundfile",
        "python-dotenv": "dotenv",
        "pyserial": "serial",
    }
    failures = []
    for package, module in modules.items():
        try:
            importlib.import_module(module)
            version = importlib.metadata.version(package)
            print(f"OK {package} {version}")
        except Exception as exc:
            # Native loader errors belong in this diagnostic, not only ImportError.
            print(f"FAIL {package}: {type(exc).__name__}")
            failures.append(package)
    # Importing/starting pynput can involve macOS input-monitoring permissions.
    if importlib.util.find_spec("pynput") is None:
        print("FAIL pynput: package missing")
        failures.append("pynput")
    else:
        print("OK pynput installed (keyboard hook not started)")

    # Keep PyAV's FFmpeg outside the OpenCV process on macOS.
    stt_python = ROOT / ".venv-stt" / "bin" / "python"
    if stt_python.exists():
        result = subprocess.run(
            [str(stt_python), "-c", "import faster_whisper; from importlib.metadata import version; print(version('faster-whisper'))"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode:
            print("FAIL isolated faster-whisper import")
            failures.append("faster-whisper")
        else:
            print(f"OK faster-whisper {result.stdout.strip()} (isolated speech environment)")
    else:
        print("Local STT environment: not installed (optional)")

    load_environment()
    for name in ("whisper-cli", "ffmpeg", "say"):
        if shutil.which(name):
            print(f"OK local speech tool: {name}")
        else:
            print(f"FAIL local speech tool missing: {name}")
            failures.append(name)
    model_path = Path(os.getenv("WHISPER_MODEL_PATH", "models/whisper/ggml-tiny.en-q5_1.bin")).expanduser()
    if not model_path.is_absolute():
        model_path = ROOT / model_path
    if model_path.is_file():
        print(f"OK local Whisper model: {model_path.name}")
    else:
        print("FAIL local Whisper model file missing")
        failures.append("Whisper model")
    for name in ("XAI_API_KEY", "XAI_MODEL", "ANTHROPIC_API_KEY", "MISTRAL_API_KEY", "CYBERWAVE_API_KEY"):
        print(f"{name}: {'configured' if os.environ.get(name) else 'not configured'}")
    print("No API, microphone, camera or robot connection was opened.")
    return 1 if failures else 0


def offline_smoke() -> int:
    """Pass local fixtures through the actual upstream plan parser."""
    from planner import parse_plan_json

    fixtures = sorted((ROOT / "inputs" / "plans").glob("*.json"))
    if not fixtures:
        raise RuntimeError("No offline plan fixtures found")
    for path in fixtures:
        plan, error = parse_plan_json(path.read_text())
        if error or plan is None:
            print(f"FAIL {path.name}: {error}")
            return 1
        print(f"OK {path.name}: {len(plan.actions)} planned action(s)")
        print(json.dumps({"say": plan.say, "actions": [vars(a) for a in plan.actions]}, indent=2))
    print("Upstream parser verified. No model or executor was called.")
    return 0


def preview(*, voice: bool = False, vision: bool = False) -> None:
    """Explicitly requested cloud preview; always pass upstream --dry-run."""
    load_environment()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY in .env before using cloud preview.")
    if voice and not os.environ.get("MISTRAL_API_KEY"):
        raise SystemExit("Upstream --voice uses Mistral; set MISTRAL_API_KEY or use typed input.")
    os.environ["CW_MODE"] = "simulation"
    args = [str(UPSTREAM / "nl_arm_controller.py"), "--dry-run"]
    if voice:
        args.append("--voice")
    if vision:
        args.append("--vision")
    sys.argv = args
    print("Cloud preview: model requests may incur provider charges; robot execution is disabled.")
    runpy.run_path(args[0], run_name="__main__")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="Check installed dependencies; no devices or services")
    sub.add_parser("offline-smoke", help="Validate local example plans; no model calls")
    cloud = sub.add_parser("preview", help="Explicit cloud planner preview; no robot connection")
    cloud.add_argument("--voice", action="store_true", help="Use upstream Mistral push-to-talk")
    cloud.add_argument("--vision", action="store_true", help="Capture a local camera image")
    sub.add_parser("ports", help="List USB/serial metadata without opening ports")
    profile = sub.add_parser("profile", help="Show missing robot configuration; no connection")
    profile.add_argument("--file", type=Path, default=ROOT / "config/robot-profile.json")
    sub.add_parser("speech-smoke", help="Local synthetic speech test; no mic, playback or API")
    speak = sub.add_parser("speak", help="Speak a local reply using macOS TTS")
    speak.add_argument("text")
    transcript = sub.add_parser("transcribe", help="Transcribe an audio file using local Whisper")
    transcript.add_argument("audio", type=Path)
    transcript.add_argument("--model", type=Path)
    listen = sub.add_parser("listen", help="Record and transcribe a short command; no robot")
    listen.add_argument("--seconds", type=float, default=4)
    listen.add_argument("--speak", action="store_true", help="Speak back the recognized text")
    toggle = sub.add_parser("voice-toggle", help="Whisper -> Grok -> targets; hardware requires explicit --robot")
    toggle.add_argument("--terminal", action="store_true", help="Use Enter instead of a global keyboard hook")
    toggle.add_argument("--max-seconds", type=float, default=30, help="Auto-stop each recording (1 to 120 seconds)")
    toggle.add_argument("--planner", choices=("grok", "basic"), default="grok", help="Grok LLM (default), or offline phrase helper")
    toggle.add_argument("--mock-state", action="store_true", help="Resolve numbers using labelled illustrative state, never hardware")
    toggle.add_argument("--robot", choices=("pair", "leader", "follower"), help="Read real arms; pair defaults unspecified voice targets to both")
    toggle.add_argument("--execute", action="store_true", help="Execute validated hardware plans; requires --robot and verified directions/controller readiness")
    sub.add_parser("robot-diagnostics", help="Read both arms, calibration, controller readiness; no motor writes")
    robot = sub.add_parser("robot-preview", help="Typed Grok intent -> real measured two-arm plan; no writes by default")
    robot.add_argument("text")
    robot.add_argument("--arm", choices=("both", "leader", "follower"), default="both")
    robot.add_argument("--execute", action="store_true", help="Execute only a fully validated hardware plan")
    intent = sub.add_parser("command-preview", help="Offline basic intent/parameter preview; no hardware or model calls")
    intent.add_argument("text")
    grok = sub.add_parser("grok-preview", help="Text -> Grok intent -> Cyberwave plan preview; API call, no robot")
    grok.add_argument("text")
    grok.add_argument("--mock-state", action="store_true", help="Use illustrative positions/limits for numeric resolution")
    agent = sub.add_parser("agent-preview", help="Local voice/TTS + Cyberwave cloud planner; no robot")
    agent.add_argument("--voice", action="store_true", help="Record locally on Enter")
    agent.add_argument("--vision", action="store_true", help="Include the configured camera")
    agent.add_argument("--speak", action="store_true", help="Speak the model's reply locally")
    agent.add_argument("--seconds", type=float, default=4)
    camera = sub.add_parser("camera-check", help="Explicitly open a camera and save a local snapshot")
    camera.add_argument("--index", type=int, default=0)
    args = parser.parse_args()
    if args.command == "voice-toggle":
        if args.execute and not args.robot:
            parser.error("--execute requires --robot")
        if args.robot and (args.mock_state or args.planner != "grok"):
            parser.error("--robot requires Grok and cannot use --mock-state")
    if args.command in {"robot-diagnostics", "robot-preview"}:
        import robot_control
        load_environment()
        try:
            if args.command == "robot-diagnostics":
                result = robot_control.diagnostics()
            else:
                result = robot_control.Controller(execute=args.execute, default_arm=args.arm).process(args.text)
        except (ValueError, RuntimeError, OSError) as exc:
            print(json.dumps({"status": "failed", "error": str(exc), "executed_actions": 0}))
            return 1
        print(json.dumps(result, indent=2))
        return 1 if result.get("status") in {"hardware_failed", "blocked_hardware", "needs_configuration"} else 0
    if args.command in (None, "doctor"):
        return doctor()
    if args.command == "offline-smoke":
        return offline_smoke()
    if args.command == "command-preview":
        import command_preview

        print(json.dumps({"text": args.text, **command_preview.preview(args.text)}, indent=2))
        return 0
    if args.command == "grok-preview":
        import grok_planner

        load_environment()
        try:
            result = grok_planner.preview(args.text, mock_state=args.mock_state)
        except (ValueError, RuntimeError) as exc:
            print(str(exc))
            return 1
        print(json.dumps({"text": args.text, **result}, indent=2))
        return 0
    if args.command == "voice-toggle":
        import voice_toggle

        load_environment()
        return voice_toggle.run(terminal=args.terminal, max_seconds=args.max_seconds, planner=args.planner,
                                mock_state=args.mock_state, robot=args.robot, execute=args.execute)
    if args.command == "agent-preview":
        import agent_preview

        load_environment()
        return agent_preview.run(voice=args.voice, vision=args.vision, speak=args.speak, seconds=args.seconds)
    if args.command != "preview":
        import readiness

        load_environment()
        if args.command == "ports":
            print(json.dumps(readiness.list_ports(), indent=2))
        elif args.command == "profile":
            print(json.dumps(readiness.profile_report(args.file), indent=2))
        elif args.command == "speech-smoke":
            result = readiness.speech_smoke()
            print(json.dumps(result, indent=2))
            return 0 if result["passed"] else 1
        elif args.command == "speak":
            readiness.synthesize(args.text)
        elif args.command == "transcribe":
            print(json.dumps(readiness.transcribe(args.audio, args.model), indent=2))
        elif args.command == "listen":
            result = readiness.transcribe(readiness.record(args.seconds))
            print(json.dumps(result, indent=2))
            if args.speak:
                readiness.synthesize(f"I heard: {result['text']}. No movement was sent.")
        elif args.command == "camera-check":
            print(json.dumps(readiness.camera_snapshot(args.index), indent=2))
        return 0
    preview(voice=args.voice, vision=args.vision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
