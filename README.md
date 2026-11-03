# SO101 voice system

Local microphone capture → Whisper transcription → Grok structured command JSON. The repository contains the voice and interpretation layer, with optional local speech output and a standalone camera check.

## Setup on macOS

Use Python 3.12. Install `whisper-cpp` and `ffmpeg` through Homebrew, then create the Python environment:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cp .env.example .env
```

Set `XAI_API_KEY` and `XAI_MODEL` in `.env`, or copy the separately supplied credential file into place. The credential file is excluded from Git. `requirements.lock` pins the macOS environment; `requirements.in` lists direct dependencies. The optional `requirements-stt.in` / `.lock` describe the alternative faster-whisper environment; the active launcher uses whisper.cpp.

Download the model to the path in `.env`. The pinned URL, size and SHA-256 are in `config/whisper-model.json`:

```sh
.venv/bin/python - <<'PYTHON'
import hashlib, json, pathlib, urllib.request
config = json.loads(pathlib.Path('config/whisper-model.json').read_text())
output = pathlib.Path(config['path'])
output.parent.mkdir(parents=True, exist_ok=True)
if not output.exists():
    urllib.request.urlretrieve(config['url'], output)
assert output.stat().st_size == config['size_bytes']
assert hashlib.sha256(output.read_bytes()).hexdigest() == config['sha256']
print('Whisper model verified')
PYTHON
.venv/bin/python build/dev.py doctor
```

## Voice input

```sh
.venv/bin/python build/dev.py voice-toggle --terminal --max-seconds 90
```

Press Enter to start recording, speak, then press Enter to stop. The microphone closes before transcription and the Grok request. The terminal prints the transcript and validated JSON, then returns to `READY`. Type `quit` or press Ctrl+C to exit.

Without `--terminal`, Control + Command + Shift + R also toggles recording. Missing macOS keyboard permission falls back to Enter. Microphone permission is required for capture. Each recording is capped at 30 seconds by default, configurable from 1 to 120 seconds.

For leader/follower/both labels in the output:

```sh
.venv/bin/python build/dev.py voice-toggle --terminal --paired
.venv/bin/python build/dev.py grok-preview "Move both arms 5 degrees left then 30 degrees right" --paired
```

`--default-arm leader|follower|both` sets the label for commands that omit an arm when `--paired` is enabled. These are semantic labels, with no USB connection or motor configuration.

## Output contract

`grok_planner.interpret(text, paired=False)` returns `status`, `reply` and ordered `actions`. Each action includes `type`, `joint`, `direction`, `amount_degrees` and `pose`; paired mode adds `arm`. Types are `relative`, `jog`, `endpoint`, `pose` and `stop`.

An explicit degree amount is retained from the request. Jog and endpoint requests keep their amount null. A named pose remains a name without a stored joint position. Ambiguous or unsupported requests return an explanation with no actions. Replies describe the proposed command rather than claiming execution.

`grok_planner.preview(...)` wraps the intent as `mode=intent_only`, `proposed_actions`, and `executed_actions=0`. It leaves `motion_plan=null` and `parameter_changes=[]`. A downstream controller must supply its own state, physical viewpoint, limits, calibration and execution policy.

## Other commands

```sh
# Typed Grok interpretation, using the configured API account:
.venv/bin/python build/dev.py grok-preview "Bend the wrist down 30 degrees"
# Offline basic phrase helper:
.venv/bin/python build/dev.py command-preview "move left then move up"
.venv/bin/python build/dev.py voice-toggle --terminal --planner basic
# Local audio transcription and optional speech output:
.venv/bin/python build/dev.py transcribe recording.wav
.venv/bin/python build/dev.py listen --seconds 4
.venv/bin/python build/dev.py speak "Ready"
# Synthetic speech test, without microphone or speaker playback:
.venv/bin/python build/dev.py speech-smoke
# Standalone local camera snapshot:
.venv/bin/python build/dev.py camera-check --index 0
```

Audio and JSONL session logs are stored locally under ignored `.cache/audio/` and `.cache/logs/`. Logs include transcripts, intent results, errors and timings. Grok calls send the transcript and fixed schema/instructions to xAI. The normal voice loop does not speak replies; `speak` and `listen --speak` opt into local macOS speech output. Camera snapshots are a separate check and are not sent to Grok by this launcher.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python build/dev.py doctor
```

Tests cover recording lifecycle, shortcuts, structured intent validation, mocked HTTP responses, command ordering, error handling and session logging. They do not establish microphone quality, live model quality or physical robot behavior.

`build/` contains the voice implementation, `tests/` its offline checks, `config/` the Whisper model provenance, and `.vscode/` voice-only launch configurations. Hardware settings, calibration, motion presets, servo tools, mock motor states and hardware reports are excluded from the current repository tree. Previous local material is preserved in ignored `_old/`.
