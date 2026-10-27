# Local voice engine

The DAI voice bridge supports local/offline text-to-speech with either Piper or Kokoro ONNX.

## Preferred Kokoro configuration

The live workstation uses:

- Engine: `kokoro`
- Voice: `af_heart`
- Speed: `0.97`
- Model and voice bundle: `~/.venice-core/state/voice/kokoro/`
- Fallback: Piper `en_US-lessac-medium` when Kokoro fails

Configuration is read from `.env` by the voice bridge. The runtime Python environment is `~/.local/voice-venv`; install voice packages there, not into system Python.

Relevant settings:

- `DAI_TTS_ENGINE=piper|kokoro`
- `DAI_KOKORO_VOICE=af_heart`
- `DAI_KOKORO_SPEED=0.97`
- `DAI_KOKORO_MODEL_PATH=/absolute/path/to/kokoro-v1.0.int8.onnx`
- `DAI_KOKORO_VOICES_PATH=/absolute/path/to/voices-v1.0.bin`
- `DAI_TTS_FALLBACK_PIPER=1`

## Verify

1. Run `bin/start-spine.sh --no-xvfb` to apply voice configuration and restart the spine services.
2. Check `curl -fsS http://127.0.0.1:8766/health`; it should report `tts_engine=kokoro`, `voice=af_heart`, and `tts_speed=0.97`.
3. Send a short JSON request to `POST /v1/audio/speech` with `input`, `voice: default`, and `response_format: wav`.
4. Validate the returned WAV and test the actual microphone-to-response path separately. A successful synthetic WAV does not by itself prove live microphone capture or speaker playback.

The bridge falls back to Piper only when Kokoro synthesis fails and `DAI_TTS_FALLBACK_PIPER` is enabled. The bridge remains localhost-bound by default; do not expose it to a network without authentication.
