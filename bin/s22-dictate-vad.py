#!/usr/bin/env python3
"""
S22 Ultra Instant Dictator with Auto-Silence Detection (VAD).
Single-click or single-keypress trigger:
  1. Starts recording from S22 Ultra (android-4f3b250).
  2. Listens while you speak.
  3. Automatically stops the millisecond you pause (0.8s silence).
  4. Transcribes in 0.2s via local Whisper STT.
  5. Pastes directly into active chat box / window via Ctrl+V.
Zero second button press required. Zero repetition loops.
"""

import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import wave

SOURCE = "android-4f3b250"
RATE = 16000
CHANNELS = 1
SILENCE_TIMEOUT = 0.85  # Seconds of silence after speech to auto-finish
SPEECH_THRESHOLD = 85.0  # Sensitive speech threshold
MAX_RECORD_TIME = 20.0
VOICE_URL = "http://127.0.0.1:8766"


def get_active_source() -> str:
    try:
        out = subprocess.check_output(["pactl", "list", "short", "sources"], text=True, stderr=subprocess.DEVNULL)
        if "android-4f3b250" in out:
            return "android-4f3b250"
        if "s22-mic" in out:
            return "s22-mic"
    except Exception:
        pass
    return "default"


def record_phrase_vad() -> bytes | None:
    src = get_active_source()
    cmd = ["parec", "--rate=16000", "--channels=1", "--format=s16le", "--raw"]
    if src != "default":
        cmd.extend(["-d", src])

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        return None

    chunk_bytes = int(RATE * 0.08 * 2)  # 80ms chunks
    speech_started = False
    silence_start = None
    recorded_frames = []
    start_time = time.time()

    # Indicate listening
    subprocess.Popen(
        ["notify-send", "-t", "2500", "🎤 Listening...", "Speak now — stops automatically when you pause."],
        stderr=subprocess.DEVNULL
    )

    while True:
        raw = proc.stdout.read(chunk_bytes)
        if not raw:
            break

        count = len(raw) // 2
        shorts = struct.unpack(f"<{count}h", raw)
        sum_sq = sum(s * s for s in shorts)
        rms = math.sqrt(sum_sq / count) if count > 0 else 0.0

        if not speech_started:
            recorded_frames.append(raw)
            if len(recorded_frames) > 3:
                recorded_frames.pop(0)

            if rms >= SPEECH_THRESHOLD:
                speech_started = True
                silence_start = None

            # Cancel if user didn't speak within 5 seconds
            if time.time() - start_time > 5.0:
                proc.kill()
                subprocess.Popen(["notify-send", "-t", "1200", "🎤 Dictation", "Cancelled (no speech heard)"], stderr=subprocess.DEVNULL)
                return None
        else:
            recorded_frames.append(raw)
            if rms < SPEECH_THRESHOLD:
                if silence_start is None:
                    silence_start = time.time()
                elif time.time() - silence_start >= SILENCE_TIMEOUT:
                    # Silence detected — auto finish immediately!
                    break
            else:
                silence_start = None

            if time.time() - start_time >= MAX_RECORD_TIME:
                break

    proc.kill()
    if not speech_started or len(recorded_frames) < 5:
        return None

    # Package into 16kHz mono WAV in memory
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(recorded_frames))
    return buf.getvalue()


def transcribe_wav(wav_bytes: bytes) -> str:
    # 1. Try local Voice Bridge STT on 8766 (fastest int8 Whisper)
    boundary = "----DaiSTTBoundary"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="file"; filename="dictation.wav"\r\n')
    body.extend(b"Content-Type: audio/wav\r\n\r\n")
    body.extend(wav_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="model"\r\n\r\nbase\r\n')
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="language"\r\n\r\nen\r\n')
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{VOICE_URL}/v1/audio/transcriptions",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("text", "").strip()
    except Exception:
        pass

    # 2. Fallback to local whisper-cli if server was offline
    cli = os.path.expanduser("~/.local/share/dhakidd-whisper/whisper-cli")
    model = os.path.expanduser("~/.local/share/dhakidd-whisper/ggml-base.en.bin")
    if os.path.isfile(cli) and os.path.isfile(model):
        tmp = "/tmp/dai_dict_fb.wav"
        with open(tmp, "wb") as f:
            f.write(wav_bytes)
        try:
            out = subprocess.check_output([cli, "-m", model, "-t", "6", "-l", "en", "-nt", tmp], text=True, stderr=subprocess.DEVNULL)
            return out.strip()
        except Exception:
            pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    return ""


def paste_text(text: str) -> None:
    if not text:
        return

    # Clean hallucination repetition (e.g. "you you you" or repeated sentences)
    words = text.split()
    if len(words) > 6 and len(set(words[:10])) <= 2:
        text = " ".join(dict.fromkeys(words))

    # Save current clipboard
    old_clip = ""
    try:
        old_clip = subprocess.check_output(["xclip", "-selection", "clipboard", "-o"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    # Put transcribed text into clipboard
    try:
        p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p.communicate(text.encode("utf-8"))
        time.sleep(0.05)
        # Instant paste via Ctrl+V into active browser/window
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], stderr=subprocess.DEVNULL)
    except Exception:
        # Fallback to xdotool type
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "2", "--", text], stderr=subprocess.DEVNULL)

    # Restore old clipboard
    if old_clip:
        time.sleep(0.5)
        try:
            p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            p.communicate(old_clip.encode("utf-8"))
        except Exception:
            pass


def main() -> None:
    # Check lock / prevent duplicate runs
    pidfile = os.path.expanduser("~/.local/state/dhakidd-dictate/recording.pid")
    os.makedirs(os.path.dirname(pidfile), exist_ok=True)

    if os.path.exists(pidfile):
        try:
            with open(pidfile) as f:
                p = int(f.read().strip())
            os.kill(p, 0)
            # Already active, exit
            return
        except Exception:
            pass

    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

    try:
        wav = record_phrase_vad()
        if not wav:
            return

        text = transcribe_wav(wav)
        if text:
            paste_text(text)
    finally:
        try:
            if os.path.exists(pidfile):
                os.remove(pidfile)
        except Exception:
            pass


if __name__ == "__main__":
    main()
