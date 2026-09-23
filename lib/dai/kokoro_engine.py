"""Kokoro-82M Neural Voice Engine for Day.

100% sovereign, local, lightweight, and human-sounding open-weight TTS.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

MODEL_DIR = Path.home() / ".local" / "voice-models" / "kokoro"
MODEL_PATH = MODEL_DIR / "kokoro-v1.0.onnx"
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"

KOKORO_MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx"
KOKORO_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

_kokoro_instance = None


def is_kokoro_available() -> bool:
    """Check if model and voices files are downloaded."""
    return MODEL_PATH.is_file() and VOICES_PATH.is_file()


def ensure_kokoro_installed() -> bool:
    """Download Kokoro int8 model (88MB) and voices (28MB) if missing."""
    if is_kokoro_available():
        return True

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print("\n[Kokoro Setup]: Downloading lightweight neural voice weights (116MB total)...")
    try:
        if not MODEL_PATH.is_file():
            print("  Downloading model weights...")
            subprocess.run(["curl", "-L", "-sS", "-o", str(MODEL_PATH), KOKORO_MODEL_URL], check=True, timeout=120)
        if not VOICES_PATH.is_file():
            print("  Downloading neural voices...")
            subprocess.run(["curl", "-L", "-sS", "-o", str(VOICES_PATH), KOKORO_VOICES_URL], check=True, timeout=60)
        print("  ✅ Kokoro-82M ready!")
        return True
    except Exception as e:
        print(f"  ❌ Download failed: {e}")
        return False


def get_kokoro():
    """Lazy-load the Kokoro ONNX model instance."""
    global _kokoro_instance
    if _kokoro_instance is None:
        if not is_kokoro_available():
            if not ensure_kokoro_installed():
                return None
        try:
            from kokoro_onnx import Kokoro
            _kokoro_instance = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
        except Exception as e:
            print(f"[Kokoro Init Error]: {e}")
            return None
    return _kokoro_instance


def synthesize_speech(text: str, voice: str = "af_heart", speed: float = 1.05) -> str | None:
    """Synthesize text using Kokoro-82M to a temporary WAV file and return path."""
    k = get_kokoro()
    if k is None:
        return None

    try:
        import soundfile as sf
        samples, sample_rate = k.create(text, voice=voice, speed=speed, lang="en-us")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name
        sf.write(tmp_wav, samples, sample_rate)
        return tmp_wav
    except Exception as e:
        print(f"[Kokoro Synthesis Error]: {e}")
        return None
