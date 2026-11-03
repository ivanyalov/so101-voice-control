# Third-party sources

## Cyberwave Python SDK and NL arm example

- Source: https://github.com/cyberwave-os/cyberwave-python
- Revision: `85937673a4ef45bedffefb2af2047851d81ae5ed`
- License: MIT. Copy preserved at `vendor/cyberwave_nl_arm/LICENSE`.
- Selected files from `examples/nl_arm_controller/` are vendored unmodified; see `vendor/cyberwave_nl_arm/UPSTREAM.json`.
- The SDK is installed as a dependency from the same revision. Our development launcher is separate from the upstream example.

Phosphobot and Fouad's SO101 speech project were inspected as references. Their code has not been copied or installed in this project.

## Local Whisper model

- Model: Whisper Tiny English, Q5_1 GGML conversion for whisper.cpp.
- Source: https://huggingface.co/ggerganov/whisper.cpp
- Repository revision: `5359861c739e955e79d9a303bcbc70fb988958b1`.
- License: MIT, per the model repository.
- Weights live in ignored `models/`; size, SHA-256 and pinned download URL are recorded in `config/whisper-model.json`.
- The pre-existing Homebrew whisper.cpp/ffmpeg installations are used unchanged through subprocess calls.
