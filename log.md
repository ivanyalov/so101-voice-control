# Decision log

## 2026-11-03

- Ivan requested removal of robot settings, movement presets and related hardware material from GitHub, retaining only the voice-system software.
- Archived the previous tree locally before editing. Hardware settings/scripts, calibration, numeric mock states, bundled hardware input files, hardware reports and the legacy vendored robot controller are excluded from the current tree. Local archive: `_old/2026-11-03_before-voice-only/`, ignored by Git.
- Voice output ends at validated semantic command JSON. Removed motor-controller imports, target resolution and hardware execution flags. Retained the active Whisper/Grok voice workflow, optional local speech output, logs, basic offline parsing and standalone camera snapshots. API credentials remain outside Git and the separately exported environment file is unchanged.
- Validation: 33 tests passed from a clean temporary copy; voice dependency checks and paired-mode startup/quit passed. No microphone capture, live Grok call, camera capture or motor access. Source secret scan passed; separate environment export verified unchanged.
