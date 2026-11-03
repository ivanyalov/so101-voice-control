# SO101 voice system

Updated: 2026-11-03, Asia/Hong_Kong.

## Current task

Ivan requested that GitHub contain only the voice-system software. The current tree retains microphone capture, local Whisper transcription, Grok structured intent (including optional semantic arm labels), optional local speech output, session logs, a standalone camera check and offline tests.

Robot profiles, calibration, direct motor control, test movements, numeric mock states, hardware packages/reports and legacy vendored controller examples are archived locally under ignored `_old/2026-11-03_before-voice-only/`. The previous Git tree is also preserved there as `repository.tar`. No hardware operation was performed during this cleanup.

Private GitHub repository: https://github.com/ivanyalov/so101-voice-control . The separate credential file remains at ignored `out/2026-11-03_so101.env`; `.env` is unchanged.

## Verification

33 offline tests pass, including the clean-copy suite. Voice dependency checks pass. Startup/quit of paired voice mode passed from a temporary clean copy without opening the microphone or making a network request. Source secret scan passed and the exported credential file is unchanged. Live microphone, Grok requests and camera capture are not exercised by this repository cleanup.

## Next step

Use `README.md` to start the voice system. Integrating its command JSON with a physical controller is separate work and requires external hardware configuration.
