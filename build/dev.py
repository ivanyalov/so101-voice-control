"""Voice-system launcher: local Whisper, Grok command JSON, speech and camera checks."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_environment():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)


def doctor():
    print(f'Python: {sys.version.split()[0]}')
    failures = []
    for package, module in {'httpx': 'httpx', 'numpy': 'numpy', 'sounddevice': 'sounddevice',
                            'soundfile': 'soundfile', 'python-dotenv': 'dotenv',
                            'opencv-python-headless': 'cv2'}.items():
        try:
            importlib.import_module(module)
            print(f'OK {package} {importlib.metadata.version(package)}')
        except Exception as exc:
            print(f'FAIL {package}: {type(exc).__name__}')
            failures.append(package)
    if importlib.util.find_spec('pynput') is None:
        failures.append('pynput')
        print('FAIL pynput: package missing')
    else:
        print('OK pynput installed (keyboard hook not started)')
    for name in ('whisper-cli', 'ffmpeg', 'say'):
        if shutil.which(name):
            print(f'OK local speech tool: {name}')
        else:
            print(f'FAIL local speech tool missing: {name}')
            failures.append(name)
    import readiness
    try:
        print(f'OK Whisper model: {readiness.resolve_model().name}')
    except RuntimeError as exc:
        print(str(exc))
        failures.append('Whisper model')
    for name in ('XAI_API_KEY', 'XAI_MODEL'):
        print(f"{name}: {'configured' if os.getenv(name) else 'not configured'}")
    print('No API, microphone or camera was opened.')
    return int(bool(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('doctor', help='Check speech dependencies without opening devices or services')
    toggle = sub.add_parser('voice-toggle', help='Record -> local Whisper -> Grok command JSON')
    toggle.add_argument('--terminal', action='store_true')
    toggle.add_argument('--max-seconds', type=float, default=30)
    toggle.add_argument('--planner', choices=('grok', 'basic'), default='grok')
    toggle.add_argument('--paired', action='store_true', help='Include arm names in semantic output')
    toggle.add_argument('--execute', action='store_true', help='Execute explicit-degree follower commands through Cyberwave')
    toggle.add_argument('--default-arm', choices=('leader', 'follower', 'both'), default='both')
    grok = sub.add_parser('grok-preview', help='Interpret typed text with Grok (API call)')
    grok.add_argument('text')
    grok.add_argument('--paired', action='store_true', help='Include leader/follower/both in semantic output')
    grok.add_argument('--default-arm', choices=('leader', 'follower', 'both'), default='both')
    basic = sub.add_parser('command-preview', help='Offline basic command parser')
    basic.add_argument('text')
    sub.add_parser('speech-smoke', help='Synthetic speech transcription check, without microphone')
    speak = sub.add_parser('speak', help='Speak text locally with macOS say')
    speak.add_argument('text')
    transcript = sub.add_parser('transcribe', help='Transcribe an audio file locally')
    transcript.add_argument('audio', type=Path)
    transcript.add_argument('--model', type=Path)
    listen = sub.add_parser('listen', help='Record and transcribe a short phrase')
    listen.add_argument('--seconds', type=float, default=4)
    listen.add_argument('--speak', action='store_true')
    camera = sub.add_parser('camera-check', help='Save a local camera snapshot')
    camera.add_argument('--index', type=int, default=0)
    args = parser.parse_args()
    load_environment()
    try:
        if args.command in (None, 'doctor'):
            return doctor()
        if args.command == 'voice-toggle':
            import voice_toggle
            return voice_toggle.run(terminal=args.terminal, max_seconds=args.max_seconds, planner=args.planner, paired=args.paired, default_arm=args.default_arm, execute=args.execute)
        if args.command == 'grok-preview':
            import grok_planner
            result = grok_planner.preview(args.text, paired=args.paired, default_arm=args.default_arm)
        elif args.command == 'command-preview':
            import command_preview
            result = command_preview.preview(args.text)
        else:
            import readiness
            if args.command == 'speech-smoke':
                result = readiness.speech_smoke()
                print(json.dumps(result, indent=2))
                return int(not result['passed'])
            if args.command == 'speak':
                readiness.synthesize(args.text)
                return 0
            if args.command == 'transcribe':
                result = readiness.transcribe(args.audio, args.model)
            elif args.command == 'listen':
                result = readiness.transcribe(readiness.record(args.seconds))
                if args.speak:
                    readiness.synthesize(result['text'])
            elif args.command == 'camera-check':
                result = readiness.camera_snapshot(args.index)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
