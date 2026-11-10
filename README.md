# SO101 voice control

Speak an English command in the terminal. Local Whisper transcribes it, Grok turns the transcript into structured intent, and the local client resolves the exact targets. In execution mode, Cyberwave sends the targets to a paired SO101 follower. Grok receives the transcript. Audio is processed on the Mac.

## Set up the client on macOS

Install Homebrew and Git, then run these commands from a terminal. The private repository requires GitHub access.

```sh
git clone https://github.com/ivanyalov/so101-voice-control.git
cd so101-voice-control
brew install python@3.12 whisper-cpp portaudio libsndfile ffmpeg
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

Save the privately supplied environment file as `.env` in the project root and run `chmod 600 .env`. If using your own accounts, copy [`.env.example`](.env.example) to `.env` and fill in your xAI settings. For Cyberwave control, also set the key, workspace ID and paired twin UUID. Existing Cyberwave Edge installations can supply the key and workspace from their local configuration instead.

Download the pinned English Whisper model and verify its checksum:

```sh
.venv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path
from urllib.request import urlretrieve

model = json.loads(Path("config/whisper-model.json").read_text())
output = Path(model["path"])
output.parent.mkdir(parents=True, exist_ok=True)
if not output.exists():
    urlretrieve(model["url"], output)
if output.stat().st_size != model["size_bytes"] or hashlib.sha256(output.read_bytes()).hexdigest() != model["sha256"]:
    raise SystemExit(f"Model verification failed. Remove {output} and download it again.")
print("Whisper model verified")
PY
.venv/bin/python build/dev.py doctor
```

## Try it without a robot

```sh
.venv/bin/python build/dev.py voice-toggle --terminal
./robot say "move the elbow plus 10 degrees while moving the wrist roll minus 5 degrees" --preview
```

In the voice terminal, press Enter to record and Enter again to transcribe and interpret. Type `quit` to exit. Allow Terminal to use the microphone when macOS prompts. The first command prints the transcript and Grok intent. The second also shows the locally resolved targets without connecting to Cyberwave. Both Grok paths need an xAI key and internet access. `./control --demo` opens an optional local browser demo without using the microphone, Grok or Cyberwave.

## Use an existing paired robot

```sh
.venv/bin/python -m pip install -r requirements-cyberwave.in
./robot status
```

The client can run on a different Mac from the robot host. The credentials must authorize the intended workspace and twin. The host needs the matching patched Cyberwave driver and fresh six-joint feedback. A generic Cyberwave installation does not have the managed motion protocol. Check the twin with the operator and use one command-producing session at a time.

The live voice entry point is `./robot voice`. Opening it leaves the microphone off and sends no movement. Enter starts recording and Enter again submits the phrase for execution. `./robot say "…"` runs the same workflow for typed commands. Use the preview above to inspect targets first. The operator should be at the robot for the initial connected checks and subsequent motion tests. `./robot stop` requests a driver stop with holding torque. It is not an emergency power cut.

The repository includes the native Mac host code and driver patches. Starting a host from a fresh clone requires separate provisioning: the reviewed Cyberwave SO101 source snapshot and native Python environment, pairing and workspace configuration, the follower's own calibration and URDF, and its USB device path. These are local to the robot host. The existing configured host can use `./robot native status` for local feedback health.

## What works and what is still open

| Feature | Status |
| --- | --- |
| Local English speech recognition and Grok interpretation | Voice capture and interpretation are available without a robot. The normal recording format goes directly to whisper.cpp in CPU mode. Recognition accuracy across speakers and six-joint commands is unbenchmarked. |
| Exact targets from voice or typed text | The local converter supports explicit signed arm angles and gripper opening percentages. Taught base labels such as “all the way left” and “halfway right” require the matching twin's local direction profile. Physical direction words for the other joints need verified mappings. |
| Coordinated motion | “While” or “together” puts up to six joints in one segment, and “then” starts another. The patched driver schedules continuous 30 Hz motion locally and checks completion using fresh feedback. Verified in offline tests. Physical timing, accuracy, stop behavior and stationary six-servo feedback still need acceptance on the connected arm. |
| Native Cyberwave transport | The configured Mac host has started and answered read-only requests through MQTT with the arm disconnected. Sustained MQTT and USB feedback reliability are still open. |
| Saved poses | Coordinates can be inspected with `./robot pose NAME --preview`. Powered replay is disabled pending travel-path validation. |

The taught direction profile and supported folded pose are specific to each robot. Keep `.cache/robot/directions.json` and `.cache/robot/poses.json` local and use verified data for the intended twin. These files are not created by cloning the repository. The folded pose is used to decide when the patched driver may release torque after successful motion. Otherwise it retains holding torque.

## Offline checks

```sh
.venv/bin/python -m unittest discover -s tests
```

A fresh clone skips the tests that require the reviewed upstream driver source snapshot. See [third-party sources](THIRD_PARTY.md) for patch provenance and the [browser interface guide](docs/2026-11-04_control-interface.md) for its controls.
