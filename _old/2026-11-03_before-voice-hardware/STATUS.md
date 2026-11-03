# SO101 voice-control harness

Updated: 2026-11-03, Asia/Hong_Kong.

## Current task

Current task: diagnose and fix rough follower movement and leader not moving. Ivan reports follower shaking and confirms leader has never moved under these commands. Read-only telemetry shows follower P=16/D=32/I=0, torque limits nonzero, acceleration=0 and speed=22 ticks/s. All six leader joints report P/D/I=0 and both torque limits=0. Calibration matches on both arms; that does not establish actuation readiness. Settings snapshot: `.cache/robot/20261103T075341890738Z_diagnostics.json`.

Updated `build/move_follower.py` to use an atomic Feetech acceleration/target/speed packet, acceleration=1 and speed=57 ticks/s (about 5 degrees/s) for a bounded five-degree base test. Shared helper: `build/servo_motion.py`. This is a proposed smoothing change, not physically validated. No new motion or hardware configuration write was performed while diagnosing. 63 offline tests pass; additional zero-drive preflight checks pass. Pair execution now refuses zero gain or torque before any writes. Leader settings were not changed or copied from follower.

Both boards and all 12 model-777 motors respond. Ivan confirmed the ZIP calibration belongs to this pair. The ZIP describes leader as a manually moved teleoperator and includes calibration, but no saved PID/torque configuration for active leader operation. Ivan asked to use the ZIP rather than ask him to reread hardware labels; do not repeat that request. Need a verified leader drive configuration or a compatible documented restoration procedure before enabling its output.

The authorized five-degree pair test was incomplete. The later user-run return also timed out: `.cache/robot/20261103T075154284410Z_return.json`; all leader encoders remained constant throughout that run. Follower base showed repeated direction reversals while returning. Hold commands were acknowledged; torque remains enabled. Original pre-test snapshot: `.cache/robot/20261103T074229990706Z_pair-test.json`. Return targets outside recorded calibration are restricted to its nearest limits. Do not repeat the old pair motion test against the inactive leader.

Voice remains local Whisper -> Grok intent -> mock target preview. Live voice-to-hardware execution is not implemented. Use Control + Command + Shift + R without Option, or Enter. Do not present mock motion plans as hardware control.

## Confirmed setup

- SO101 leader and follower, assembled in classroom 5110.
- One camera mounted on one arm; its type, connection and mounting arm are still unverified.
- Initial processing runs on the Mac; robot connection is USB. This workspace host reports Apple M3 and 16 GiB RAM.
- An ESP32 with microphone and speaker may be added later; exact board unknown.
- Existing Homebrew whisper.cpp/ffmpeg found. Ivan authorized a new small model; Tiny English Q5_1 is downloaded locally and configured.
- Cloud models/services are acceptable; no new paid usage has been started.
- End goal confirmed: move agent/models and directly attached arms/cameras/audio to Ivan's existing 64 GiB PC, with optional ESP32 audio. Do not start PC setup yet; exact CPU/GPU/OS are unverified here.
- Basic English joint commands remain useful test skills. Target architecture also includes camera perception, predefined sequences, variable-position picking and spoken outcome/failure replies.

## Results

- Expanded infrastructure comparison and architecture recommendation: [working document](work/2026-10-27_voice-harness-design.md).
- Agent-level and joint-level acceptance specification: [JSON](work/2026-10-27_command-tests.json). These are design cases, not executed application tests.
- Prior joint-only proposal and test file preserved in `_old/2026-10-27_joint-only-proposal/`.
- First code foundation selected for evaluation: the pinned Cyberwave SDK and Python multimodal example. Phosphobot remains the native-Mac alternative; LiveKit is an optional conversational runtime. Full Cyberwave platform adoption is not decided.
- Refreshed docs and inspected selected code for Cyberwave, Phosphobot, Seeed and Fouad's project. Pinned revisions and concrete adaptation gaps are recorded in the working document.
- Official LeRobot source inspected at revision `dd5313e305ff7aaa5b6c7b4769d1b26650ce250b`.
- Project-local Python 3.12.16 installed. Main `.venv` has 41 packages; optional isolated `.venv-stt` has 23. The active local STT path uses the existing whisper.cpp CLI in its own process.
- Selected upstream example files and MIT license preserved under `vendor/cyberwave_nl_arm/`, unmodified. SDK/example revision is pinned; resolved dependencies are locked.
- [README](README.md) explains the setup and commands; [development launcher](build/dev.py) provides local speech/echo, camera checks, non-opening port discovery, configuration validation and no-hardware agent previews.
- Active [Grok adapter/resolver](build/grok_planner.py) requests structured intent using xAI HTTP, resolves mock numeric targets locally, then reuses Cyberwave MotionPlan/Action/validate_plan. Vendor files remain unchanged. No executor exists on this path.
- The earlier [Claude agent preview](build/agent_preview.py) remains optional/legacy and is not required for Grok.
- [Robot profile](config/robot-profile.json) records provisional joint names/IDs; unknown driver version, calibration, units and limits remain unset. Profile validation reports these gaps without opening the arm.
- VS Code settings/tasks/launch configurations created; workspace and README opened and verified in the Visual Studio Code UI. Existing Python/debugger extensions were reused.
- Verified imports, dependency graphs, two offline plan fixtures and 10 focused tests, including mocked local voice -> reused planner -> TTS flow and missing-camera handling. Real synthetic TTS -> Whisper test recognized all five phrases in 1.648 seconds including conversion and model startup. [Report](work/2026-10-27_readiness-report.json).
- No model API request, microphone/camera capture, speaker playback, robot connection, calibration or movement was performed. Synthetic speech was generated to a temporary file for testing.
- XAI_API_KEY is configured in ignored .env; XAI_MODEL=grok-4.3 and XAI_REASONING_EFFORT=none are selected. No live Grok request was made. Other API credentials remain unconfigured; local speech paths are configured in ignored `.env`. Model weights are downloaded (32,166,155 bytes, checksum verified). Platform CLI/Edge Core/Docker and live robot driver remain uninstalled.
- Cleaned `.env` and `.env.example` for Whisper/Grok: xAI fields first, local speech settings retained, future Cyberwave settings separate, unused Claude/Mistral defaults and inactive voice flag removed. Parsing and existing-value preservation checked. The xAI key and model are now configured.
- Existing VS Code extensions cover current needs. Project recommendations and additional port/voice launch configurations are present; no global extension changes were made.
- Added `python build/dev.py voice-toggle`: Control + Command + Shift + R starts/stops microphone capture, then local Whisper provides text to Grok and the target resolver. `--planner basic` retains the earlier offline phrase helper explicitly. `--terminal` uses Enter without a global keyboard hook. Microphone is closed before transcription, recordings are capped at 30 seconds by default, and repeat presses during transcription are ignored. Audio remains in `.cache/audio/`.
- Removed `--speak` from all supplied VS Code launch configurations and normal README examples. Spoken output remains an explicit opt-in feature for a later demo.
- 18 focused tests pass, including mocked toggle capture, bounded audio, microphone cleanup, error recovery and no spoken reply. Ivan supplied a successful human-speech transcript; live recognition of the replacement shortcut remains unverified.
- The updated toggle has 27 passing focused tests. Shortcut tests cover repeated R, either modifier side, keyboard-layout characters and ignoring Option combinations. Local preview tests cover the reported left-then-top phrase, ordered parameters, missing numeric values, and rejecting negated/unsupported requests without a partial plan. These tests use mocked devices; no physical shortcut or movement was exercised.
- Grok outputs ordered semantic joint/endpoint/jog intents. Without `--mock-state`, missing measured state/configuration blocks numeric targets. With it, illustrative values yield per-joint before/target/delta, duration and Cyberwave plan JSON. Numeric physical targets still need calibration and fresh state. "Top" requires a defined pose. The offline `command-preview` helper remains a diagnostic.
- 39 focused tests pass, including mocked xAI HTTP/schema, voice -> intent -> resolver flow, pan/elbow distinction, directional endpoints, sequential mock state, missing state/credentials, invalid output, errors and no partial executable plans. No live Grok interpretation, model access, latency, camera, servo writes or collision checks were exercised.
- Saved [numeric mock preview](work/2026-10-27_grok-mock-preview.json), explicitly marked fixture_not_live_grok. Pan 15 -> 70 (+55), elbow -20 -> -10 (+10) are illustrative, not calibration.
- VS Code launch configurations 5/7 use Grok with explicit mock state, 8 supports typed Grok input, and 6 is labelled legacy Claude. Missing XAI config exits before microphone/HTTP; both typed and voice CLI failure paths were checked.
- Added durable local session/turn logs (.cache/logs): audio file links, transcripts, full structured plan/resolver results, errors and timings. Log path printed at startup; no keys/raw HTTP bodies recorded. A configured production CLI startup/quit was verified without capture or API calls. No prior hidden inference logs exist for the old transcription-only loop.
- Requested slow all-servo range check is documented as a future registered skill. There is no executable sweep: calibrated ranges, direction/units and collision-free test poses are still missing.
- Inspected Ivan's VS Code terminal after the missing recording indicator: the global shortcut command exited because macOS keyboard permission was missing. Started `voice-toggle --terminal` in that terminal and visually verified `READY. Microphone off.` User can press Enter to start/stop; no microphone capture was initiated during diagnosis.

## Open checks

- Classmate's setup file, installed LeRobot revision, Python version, robot IDs and calibration convention.
- Camera identity, mount and camera access from Mac.
- Which platform/backend integrates cleanly with the team's environment and available accounts.
- Preserve a fully local application route: local inference is documented, while Cyberwave platform self-hosting is a separate commercial/deployment question. No native Cyberwave ESP32 audio SDK was verified; plan a PC-side audio adapter.
- xAI API key/model access and allowed paid-usage budget; actual Grok interpretation quality/latency and camera capture still need testing. Human speech transcription has been demonstrated by Ivan.
- Desired joint-direction viewpoint, and whether multi-turn conversation is required in the first integration.
- Direction signs, step size and joint limits must come from the actual assembled arm.

## Next step

Ivan can run `.venv-robot/bin/python build/move_follower.py` for the updated five-degree base test and report whether movement is smoother. Leader remains blocked pending verified motor-drive settings; do not change its zero gains/limits blindly. Voice-to-hardware control waits for successful motor bring-up.

The architecture review must preserve replaceable model providers, locally runnable skills/state, and a direct local robot adapter so later PC deployment does not depend on Cyberwave's hosted control plane.
