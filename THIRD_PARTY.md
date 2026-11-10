# Third-party sources

Python packages used by the voice layer are listed in `requirements.in` and pinned with transitive dependencies in `requirements.lock`. No vendored robot controller is included.

## Local Whisper model

- Model: Whisper Tiny English, Q5_1 GGML conversion for whisper.cpp.
- Source: https://huggingface.co/ggerganov/whisper.cpp
- Repository revision: `5359861c739e955e79d9a303bcbc70fb988958b1`.
- License: MIT, per the model repository.
- Weights live in ignored `models/`; size, SHA-256 and pinned download URL are recorded in `config/whisper-model.json`.
- whisper.cpp, ffmpeg and macOS say are invoked as external processes.


## Cyberwave integration

- Cyberwave SDK 0.7.4 is an optional installed dependency in requirements-cyberwave.in. The cloud service, official CLI and Edge core are external to this repository.
- The reproducible SO101 driver patch targets the official cyberwaveos/so101-driver image pinned by digest in build/cyberwave_image.py. Upstream source: https://github.com/cyberwave-os/cyberwave-edge-so101 . Copyright (c) 2025 Cyberwave S.p.a., MIT license; complete notice retained in [driver license](docs/2026-11-03_cyberwave-driver-license.txt).
- This repository includes the patch, pose-preservation helper and rebuild script, not a copied complete driver tree. Calibration, pairing credentials and concrete physical direction profiles remain private local configuration.
