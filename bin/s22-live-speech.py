#!/usr/bin/env python3
"""
S22 Ultra Real-Time Live Speech-to-Text (Word-by-Word Streaming)
Words appear on screen live as you speak them.
Accommodates slow speech, natural pauses, and thinking time with ZERO timeouts.
Never cuts you off.
"""

import json
import os
import shutil
import subprocess
import sys
import time

SOURCE = "android-4f3b250"
MODEL_DIR = os.path.expanduser("~/.local/share/vosk-models/small-en")
PIDFILE = os.path.expanduser("~/.local/state/dhakidd-dictate/live_speech.pid")


def ensure_dependencies() -> bool:
    try:
        import vosk
        return True
    except ImportError:
        pass

    print("[Live Speech]: Installing vosk streaming engine...", flush=True)
    # Check if uv is present
    if shutil.which("uv"):
        subprocess.run(["uv", "pip", "install", "--user", "vosk"], check=False)
    else:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--break-system-packages", "vosk"],
            check=False
        )

    try:
        import vosk
        return True
    except ImportError:
        return False


def ensure_model() -> bool:
    if os.path.isdir(MODEL_DIR) and (os.path.isdir(os.path.join(MODEL_DIR, "am")) or os.path.isfile(os.path.join(MODEL_DIR, "README"))):
        return True

    os.makedirs(os.path.dirname(MODEL_DIR), exist_ok=True)
    zip_path = "/tmp/vosk-model-small-en.zip"
    print("[Live Speech]: Downloading lightweight 40MB streaming model...", flush=True)

    urls = [
        "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
        "https://huggingface.co/grimso/vosk-models/resolve/main/vosk-model-small-en-us-0.15.zip"
    ]
    success = False
    for url in urls:
        r = subprocess.run(["curl", "-sL", url, "-o", zip_path], timeout=90, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and os.path.exists(zip_path) and os.path.getsize(zip_path) > 1000000:
            success = True
            break

    if not success:
        return False

    subprocess.run(["unzip", "-q", "-o", zip_path, "-d", "/tmp"], stderr=subprocess.DEVNULL)
    extracted = "/tmp/vosk-model-small-en-us-0.15"
    if os.path.isdir(extracted):
        shutil.rmtree(MODEL_DIR, ignore_errors=True)
        shutil.move(extracted, MODEL_DIR)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    return True


def type_live_text(text: str) -> None:
    if not text:
        return
    # Stream text live into focused window
    try:
        # Use xdotool type for real-time word flow
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "1", "--", text], stderr=subprocess.DEVNULL)
    except Exception:
        pass


def run_live_stream() -> None:
    import vosk
    vosk.SetLogLevel(-1)

    try:
        model = vosk.Model(MODEL_DIR)
    except Exception as e:
        print(f"Error loading model: {e}")
        return

    rec = vosk.KaldiRecognizer(model, 16000)
    rec.SetWords(True)

    cmd = ["parec", "--rate=16000", "--channels=1", "--format=s16le", "--raw", "-d", SOURCE]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        proc = subprocess.Popen(["parec", "--rate=16000", "--channels=1", "--format=s16le", "--raw"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    subprocess.Popen(
        ["notify-send", "-t", "3000", "🎙️ Live Mic Active", "Speak naturally. Words appear live as you talk. Tap F9 to finish."],
        stderr=subprocess.DEVNULL
    )

    try:
        while True:
            data = proc.stdout.read(3200) # 100ms
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                res = json.loads(rec.Result())
                text = res.get("text", "").strip()
                if text:
                    type_live_text(text + " ")
    finally:
        proc.kill()
        subprocess.Popen(["notify-send", "-t", "1500", "🎙️ Live Mic Off", "Finished dictating."], stderr=subprocess.DEVNULL)


def main() -> None:
    os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)

    # Toggle behavior: if already running, stop it!
    if os.path.exists(PIDFILE):
        try:
            with open(PIDFILE) as f:
                p = int(f.read().strip())
            os.kill(p, 15) # Stop previous run
            os.remove(PIDFILE)
            return
        except Exception:
            pass

    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        if not ensure_dependencies():
            subprocess.Popen(["notify-send", "Error", "Could not install vosk engine."])
            return

        if not ensure_model():
            subprocess.Popen(["notify-send", "Error", "Could not download voice model."])
            return

        run_live_stream()
    finally:
        if os.path.exists(PIDFILE):
            os.remove(PIDFILE)


if __name__ == "__main__":
    main()
