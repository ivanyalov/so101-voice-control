# SO101 multimodal agent development

Mac development environment for the Cyberwave natural-language SO101 example. The active development path is local Whisper -> Grok structured intent -> local target resolution -> Cyberwave motion-plan preview. Camera understanding, spoken feedback and registered robot skills remain integration work. Physical robot operation is not enabled by the development launcher.

## Active Grok setup

Claude/Anthropic is not required for this path. Set these entries locally in the ignored `.env` file:

```dotenv
XAI_API_KEY=your_xai_api_key
XAI_MODEL=grok-4.3
XAI_REASONING_EFFORT=none
```

Use a model available to your xAI API key that supports structured JSON output. Grok 4.3 is selected with reasoning disabled for short command interpretation. Both settings are configurable; live model access remains unverified. Grok requests are billable according to your xAI account. No paid request was run during preparation. At the published short-context rates ($1.25/M input, $2.50/M output), an illustrative 1,000-input/300-output-token request costs $0.002. Rates checked on 2026-10-27 against the [official model page](https://docs.x.ai/developers/models/grok-4.3). The adapter uses the existing httpx dependency; no extra SDK is needed.

```sh
# First isolate the interpretation stage using typed input (one Grok request):
.venv/bin/python build/dev.py grok-preview "Move all the way to the right and then move your elbow up" --mock-state
# Then use the same planner/resolver with the working local voice path:
.venv/bin/python build/dev.py voice-toggle --mock-state
# If global keyboard monitoring is unavailable:
.venv/bin/python build/dev.py voice-toggle --terminal --mock-state
```

`--mock-state` uses explicitly illustrative positions, limits and signs from `inputs/mock/2026-10-27_joint-state.json`. It is not physical calibration. Omit the flag to see Grok's interpretation without invented numeric targets; measured state and verified hardware configuration are not connected yet.

Grok selects ordered semantic actions (`jog`, `endpoint`, explicit-degree `relative`, named `pose`, or `stop`). Local code resolves numeric targets and creates Cyberwave `MotionPlan`/`Action` objects, using its plan validator. The adapter is project code based on the reference's structured-planning approach; the vendor's Claude transport/prompt is not called or modified. No executor or robot connection exists on this path.

Terminal output includes provider/model, dry-run mode, source of state, ordered interpretation, each joint's before/target/delta, units, duration, the resolved Cyberwave plan and commented SDK-call examples. A plan with an unknown pose or a target outside its limits has no executable motion plan. No automatic home is added. Mock joint-limit checks do not validate physical collisions, trajectories or measured target completion.

For a mocked response to the reported right-then-elbow command, the resolver produces pan 15 -> 70 degrees (delta +55), then elbow -20 -> -10 (delta +10). That confirms resolver behavior, not that live Grok understood the sentence. Live interpretation quality still needs checking with your selected model/key. See the saved `work/2026-10-27_grok-mock-preview.json` fixture report.

## Open in VS Code

Open this folder in Visual Studio Code. The workspace selects `.venv/bin/python` (Python 3.12) and provides Run and Debug configurations:

1. **Check environment (offline)**: imports the installed libraries, reports whether credentials are configured without displaying their values, and does not open devices or services.
2. **Parse example plans (offline)**: checks local example JSON using the actual Cyberwave example parser. It neither generates a model answer nor executes a movement.
3. **Typed cloud preview**: explicitly calls the configured Anthropic model and prints its plan. This mode can incur provider charges. It always passes upstream `--dry-run`, so no Cyberwave/robot connection is created.
4. **USB ports**: enumerates serial-port metadata without opening the ports.
5. **Voice -> Grok -> MOCK targets**: Control + Command + Shift + R or Enter starts/stops recording. Whisper runs locally; Grok interprets the text; local code prints numeric mock targets. Requires XAI_API_KEY and XAI_MODEL. No robot or speech output.
6. **Legacy Claude agent preview**: optional earlier integration requiring Anthropic credentials. Not needed for Grok.
7. **Voice -> Grok -> MOCK targets, Enter fallback**: same planner with terminal start/stop.
8. **Typed Grok -> MOCK targets**: isolate the text-to-intent stage without recording audio.

The Python and debugger extensions were already installed in VS Code when this environment was prepared. If VS Code asks to select an interpreter, choose this folder's `.venv/bin/python`.

## Terminal checks

From this project folder:

```sh
source .venv/bin/activate
python build/dev.py doctor
python build/dev.py offline-smoke
python -m unittest discover -s tests -v
```

These checks require no API keys or robot. The diagnostic also checks the downloaded local speech model. They do not prove microphone/camera capture, model quality or physical execution.

## Ready-to-run local commands

```sh
# Synthetic TTS -> local Whisper test; no microphone or speaker playback:
python build/dev.py speech-smoke
# Your voice -> Grok intent -> mock numeric parameter/code preview (API request):
python build/dev.py voice-toggle --mock-state
# Fallback: Enter starts/stops recording in this terminal:
python build/dev.py voice-toggle --terminal --mock-state
# Offline voice/phrase diagnostic only, without Grok:
python build/dev.py voice-toggle --planner basic --terminal
# A single four-second recording, with text output:
python build/dev.py listen --seconds 4
# Local audio-file transcription:
python build/dev.py transcribe /absolute/path/to/recording.wav
# Test the command preview without microphone, API or hardware:
python build/dev.py command-preview "Move to the left then rise all the way to the top"
# USB port metadata, without opening a port:
python build/dev.py ports
# Show what still needs to come from the team's ZIP:
python build/dev.py profile
# Explicit camera test after choosing its index; saves an image locally:
python build/dev.py camera-check --index 0
```

`listen` is a speech test, not a motor command. The synthetic smoke test recognized all five supplied command phrases in 1.648 seconds for audio conversion plus transcription/model startup on this Mac. This is one clean generated-audio check, not measured classroom or human-speech accuracy.

## Start/stop voice shortcut

Run `python build/dev.py voice-toggle --mock-state` once after configuring xAI. Stop any older voice process with Ctrl+C before restarting.

1. Wait for **READY. Microphone off.** Press **Control + Command + Shift + R**, then release R, or press **Enter in its terminal**.
2. Wait for **RECORDING**, then speak.
3. Press the same combination again, or Enter. The microphone closes before Whisper transcribes and Grok interprets the text.
4. Read `proposed_actions`, `parameter_changes`, `motion_plan`, `code_preview`, `unresolved`, and `state_source`. Wait for **READY** before the next command.

The shortcut matches physical R on macOS and ignores held-key repeats. Option is excluded to avoid Typeless conflicts. Enter always remains available in the terminal. Missing keyboard permission or a stopped listener causes an explicit fallback to Enter. `--terminal` skips global monitoring. Ctrl+C or `quit` exits; unfinished recordings are discarded. Captures stop after 30 seconds and remain under `.cache/audio/`.

The active default is **Grok**, not the old phrase helper. Missing XAI_API_KEY/XAI_MODEL stops the command before opening the microphone. Provider failure preserves the transcript and reports `planner_failed`; there is no silent fallback to the phrase helper. `--planner basic` explicitly selects the offline diagnostic instead; it is not general language understanding.

If the terminal says **Local Whisper, text output only, no cloud or robot**, it is an old process. Python does not reload it when files change. Press Ctrl+C, wait for the shell prompt, then start the updated command. Its banner must say **Local Whisper -> Grok (grok-4.3) -> target resolution**, followed by **DRY RUN; state source: MOCK** and a **SESSION LOG** path. For long spoken commands, run `python build/dev.py voice-toggle --terminal --mock-state --max-seconds 90` and press Enter when finished.

New sessions append structured JSON events to `.cache/logs/*.jsonl`: session settings, recording start/stop, saved audio paths, transcripts, planner start/results/errors, target changes and processing timings. Events have UTC timestamps and turn IDs; the terminal prints the exact log filename. No API keys or raw HTTP response bodies are recorded. These logs are local and ignored by Git. Old transcription-only sessions saved WAV audio in `.cache/audio/`; they did not create planner logs or run Grok, and upgrading cannot recover results that were never generated.

macOS may request microphone access. Global keyboard monitoring needs OS permission for the launching application/Python; see [pynput's macOS permission notes](https://pynput.readthedocs.io/en/latest/limitations.html#macos). Enter mode needs no global keyboard monitoring.

Spoken replies are currently off in every supplied VS Code launch configuration and normal example command. The optional `--speak` switch and `speak` subcommand remain available for a later demonstration, when explicitly requested.

## Proposed slow servo-check command

"Run a slow servo check" can become a registered `range_check` skill. Its intended behavior is to test the six joints one at a time through verified test ranges, using slow interpolation between approved poses. The program must use the assembled arm's calibrated units, limits and collision-free test poses rather than inventing angles from speech. Individual motor limits alone do not establish that every arm pose is reachable without a collision.

The Grok preview currently reports this skill as unavailable; speaking it cannot execute a sweep. The skill and motor executor are not implemented. The team's setup/calibration ZIP and verification on the physical arm are needed before preparing an executable sweep. This check would exercise known ranges, not discover limits by pushing against mechanical stops.

The English Tiny Q5_1 model is downloaded under `models/whisper/` (32,166,155 bytes). Its SHA-256 was verified against the model repository; provenance is recorded in `config/whisper-model.json`. The existing Homebrew `whisper-cli` and `ffmpeg` are reused without changing their installation. They run in a separate process from the camera libraries.

## Legacy Claude agent preview (optional)

After adding the chosen Anthropic API key to `.env`:

```sh
# Type requests and read replies:
python build/dev.py agent-preview
# Press Enter to record locally; use the existing cloud planner:
python build/dev.py agent-preview --voice
# Add the configured camera when ready:
python build/dev.py agent-preview --voice --vision
```

This reuses Cyberwave's multimodal planner and camera module. It prints proposed actions and text replies, but has no robot executor. Type `quit` to exit. This older planner preview records a fixed duration on Enter; the `voice-toggle` command provides start/stop capture and a narrow offline command preview. Camera capture, microphone recording and real model calls have not been exercised during preparation. A missing camera frame is reported as unavailable rather than sent through a text-only visual guess.

## Team ZIP and hardware handoff

`config/robot-profile.json` contains the six reported names/IDs and explicitly missing configuration. Obtain the team's driver/package versions, robot IDs, per-arm calibration files, unit convention, USB-port identification and camera setup. Verify these against the ZIP before populating the profile.

The current LeRobot motor stack can differ from older setup files. Install the team's matching driver environment after inspecting its requirements. We have installed pyserial for non-invasive port discovery, not arbitrarily selected a live motor stack.

Calibration IDs and motor IDs are different. A completed profile is a configuration check, not proof of physical calibration or correct direction signs. The development launcher still provides no servo-write command.

## Cloud preview, when ready

An ignored `.env` file has been created with empty credentials. Enter `ANTHROPIC_API_KEY` there locally; `.env.example` documents the settings. Do not paste credentials into source files or commit `.env`.

```sh
python build/dev.py preview
# Optional camera capture, when explicitly desired:
python build/dev.py preview --vision
# Optional upstream cloud speech mode:
python build/dev.py preview --voice --vision
```

The upstream `preview --voice` mode uses Mistral Voxtral and needs `MISTRAL_API_KEY`. Our `agent-preview --voice` uses the downloaded local whisper.cpp model instead and needs no Mistral key. The separate faster-whisper environment remains an optional alternative; it is not the active local speech path.

The optional faster-whisper environment avoids loading conflicting OpenCV/PyAV FFmpeg bundles into one macOS process. Any future faster-whisper adapter should preserve that process boundary; the active whisper.cpp adapter already runs separately.

Cyberwave credentials and twin/environment IDs are reserved for a later platform connection. Preview only needs the chosen model provider credentials; offline checks need none. No live launcher, calibration action or automatic homing is provided here.

Use `build/dev.py`, rather than launching the vendor entry point directly: the unmodified upstream script defaults to live mode when its configuration file is absent. Upstream smoke tests can also call APIs or actuate hardware; they are not the offline checks above.

## What is installed

- Project-local Python 3.12 under `.tools/python/` and an isolated `.venv/`.
- Cyberwave SDK pinned to revision `85937673a4ef45bedffefb2af2047851d81ae5ed`.
- The example's cloud-planner, HTTP, audio, keyboard and OpenCV dependencies.
- Local Whisper Tiny English Q5_1 model, used through the existing whisper.cpp installation.
- faster-whisper in a separate `.venv-stt` as an optional alternative runtime.
- A project-local `uv` bootstrap environment for dependency management.

`requirements.in` declares the dependencies and pinned SDK source. `requirements.lock` records the resolved development environment. To synchronize it:

```sh
UV_CACHE_DIR="$PWD/.cache/uv" .bootstrap/bin/uv pip sync \
  --python .venv/bin/python requirements.lock
```

The isolated speech dependencies are declared in `requirements-stt.in` and pinned in `requirements-stt.lock`:

```sh
UV_CACHE_DIR="$PWD/.cache/uv" .bootstrap/bin/uv pip sync \
  --python .venv-stt/bin/python requirements-stt.lock
```

When migrating to the PC, resolve/install for its OS from `requirements.in`; do not copy a Mac virtual environment. Device paths and model providers are configuration, while skill contracts remain stable.

The Cyberwave CLI/Edge Core, Docker, Phosphobot server, VLA training stack and robot driver have not been installed. Those are separate integrations, not needed for voice/planner development. No global tool or shell setting was changed.

## Project map

- `build/dev.py`: diagnostics, offline parser smoke check and cloud preview wrapper.
- `build/readiness.py`: local speech, synthetic speech smoke test, port listing and profile validation.
- `build/agent_preview.py`: local speech/TTS integrated with the reused cloud planner; no robot executor.
- `build/voice_toggle.py`: start/stop capture -> local Whisper -> selected planner; Grok is the active default. No speech output or motor execution.
- `build/grok_planner.py`: xAI structured intent adapter and local resolver into Cyberwave MotionPlans; labelled mock state only for numeric resolution.
- `build/command_preview.py`: offline basic intent, parameter and symbolic code preview; no numeric target resolution or executor.
- `config/`: provisional hardware profile and downloaded-model provenance.
- `vendor/cyberwave_nl_arm/`: selected unmodified upstream example files, MIT license and source manifest.
- `inputs/plans/`: illustrative local plan fixtures. Joint values are not calibrated for the physical arm.
- `tests/`: focused launcher-boundary and upstream-parser checks.
- `work/2026-10-27_voice-harness-design.md`: infrastructure research, proposed architecture and local-PC migration requirements.
- `work/2026-10-27_command-tests.json`: broader acceptance specification; most scenarios are not implemented yet.
- `STATUS.md` and `log.md`: current state and decisions.

Next integration: configure XAI_API_KEY/XAI_MODEL, check actual Grok interpretation using typed requests, then the voice toggle. Inspect the team ZIP before connecting measured joint state or hardware execution. Physical execution and registered movement/grasp skills remain subsequent work.

API references: [xAI structured outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs), [Chat Completions](https://docs.x.ai/developers/model-capabilities/legacy/chat-completions). xAI also offers [speech to text](https://docs.x.ai/developers/model-capabilities/audio/speech-to-text), but Whisper remains the active STT.
