# Third-party sources

Python packages used by the voice layer are listed in `requirements.in` and pinned with transitive dependencies in `requirements.lock`. No vendored robot controller is included.

## Local Whisper model

- Model: Whisper Tiny English, Q5_1 GGML conversion for whisper.cpp.
- Source: https://huggingface.co/ggerganov/whisper.cpp
- Repository revision: `5359861c739e955e79d9a303bcbc70fb988958b1`.
- License: MIT, per the model repository.
- Weights live in ignored `models/`; size, SHA-256 and pinned download URL are recorded in `config/whisper-model.json`.
- whisper.cpp, ffmpeg and macOS say are invoked as external processes.
