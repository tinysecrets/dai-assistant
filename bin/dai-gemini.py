#!/usr/bin/env python3
"""DAI Gemini Mode — Continuous hands-free voice assistant.

Replica of Google Gemini Live / Google Assistant:
- Responds to: "Hey DAI", "Come here DAI", "DAI come here", "DAI"
- Intelligent: automatically decides whether to answer via voice,
  open a web page/app, run a terminal command, or automate the screen.
- Proactive: monitors health (disk, memory, services) and alerts you.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER_URL = os.environ.get("DAI_ROUTER_URL", "http://127.0.0.1:11435")
VOICE_URL = os.environ.get("DAI_VOICE_URL", "http://127.0.0.1:8766")
WORKER_URL = os.environ.get("DAI_WORKER_URL", "http://127.0.0.1:8765")

SAMPLE_RATE = 16000
CHUNK_SECONDS = 3.0
RMS_THRESHOLD = 800  # Adjust based on mic sensitivity

WAKE_PATTERNS = [
    r"\bhey\s+d\.?a\.?i\.?\b",
    r"\bcome\s+here\s+d\.?a\.?i\.?\b",
    r"\bd\.?a\.?i\.?\s+come\s+here\b",
    r"\bhey\s+day\b",
    r"\bhey\s+die\b",
    r"\bhi\s+d\.?a\.?i\.?\b",
    r"\bok\s+d\.?a\.?i\.?\b",
    r"^\s*d\.?a\.?i\.?\b",
]

_stop_event = threading.Event()


def generate_chime_wav() -> bytes:
    """Generate a clean, pleasant two-tone chime (A5 -> C#6)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        frames = []
        # Tone 1: 880 Hz (0.08s)
        for i in range(int(SAMPLE_RATE * 0.08)):
            t = float(i) / SAMPLE_RATE
            v = int(32767.0 * 0.25 * math.sin(2.0 * math.pi * 880.0 * t))
            frames.append(struct.pack("<h", v))
        # Tone 2: 1108 Hz (0.12s)
        for i in range(int(SAMPLE_RATE * 0.12)):
            t = float(i) / SAMPLE_RATE
            # slight decay
            decay = 1.0 - (float(i) / (SAMPLE_RATE * 0.12))
            v = int(32767.0 * 0.28 * decay * math.sin(2.0 * math.pi * 1108.73 * t))
            frames.append(struct.pack("<h", v))
        w.writeframes(b"".join(frames))
    return buf.getvalue()


CHIME_BYTES = generate_chime_wav()


def play_audio(data: bytes, fmt: str = "wav") -> None:
    """Play audio bytes through aplay or paplay."""
    players = ["paplay", "aplay", "mpv"]
    player = None
    for p in players:
        if shutil.which(p):
            player = p
            break
    if not player:
        return

    try:
        proc = subprocess.Popen([player], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        proc.communicate(input=data, timeout=60)
    except Exception:
        pass


def play_chime() -> None:
    play_audio(CHIME_BYTES, "wav")


def speak_text(text: str) -> None:
    """Speak text using local piper-tts via voice-bridge."""
    print(f"\n[DAI Speaks]: {text}")
    try:
        req = urllib.request.Request(
            f"{VOICE_URL}/v1/audio/speech",
            data=json.dumps({"input": text, "response_format": "wav", "voice": "default"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_data = resp.read()
            play_audio(audio_data, "wav")
    except Exception as e:
        print(f"[Voice Error]: could not speak: {e}")


def transcribe_wav(wav_bytes: bytes) -> str:
    """Transcribe audio using faster-whisper on voice-bridge."""
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n')
    body.extend(b"Content-Type: audio/wav\r\n\r\n")
    body.extend(wav_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="model"\r\n\r\n')
    body.extend(b"whisper-1\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{VOICE_URL}/v1/audio/transcriptions",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("text", "").strip()
    except Exception:
        return ""


def calculate_rms(audio_bytes: bytes) -> float:
    """Calculate Root Mean Square (RMS) volume of 16-bit PCM audio."""
    count = len(audio_bytes) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"<{count}h", audio_bytes)
    sum_squares = sum(s * s for s in shorts)
    return math.sqrt(sum_squares / count)


def extract_wake_and_command(text: str) -> tuple[bool, str]:
    """Check if text contains wake word and extract the user's command."""
    lower = text.lower().strip()
    for pattern in WAKE_PATTERNS:
        match = re.search(pattern, lower)
        if match:
            # Extract everything after the wake word
            after = lower[match.end():].strip()
            # Clean up leading punctuation or filler words like "can you", "please"
            after = re.sub(r"^[,.\-?!]+\s*", "", after)
            return True, after
    return False, ""


SYSTEM_PROMPT = """You are DAI, Justin's highly intelligent sovereign AI assistant on Debian (dhakidd).
You act like Google Gemini Live: natural, fast, direct, and capable.
Justin communicates via voice. He should never have to remember command flags or technical names.

You have the power to:
1. "speak": Just answer conversationally. Keep spoken answers concise and natural (1-3 sentences max).
2. "open_web": Open a website, search query, or local web app in browser (e.g. YouTube, GitHub, localhost:8799, realwah-lah).
3. "run_terminal": Run a safe shell command on his Debian machine (e.g. check disk space, git status, docker status, battery, temperature, list files).
4. "gui_agent": Delegate complex desktop clicking or web tasks to Agent-S on display :99.

CRITICAL: Return ONLY valid JSON with this exact structure:
{
  "thought": "why you chose this action",
  "action": "speak" | "open_web" | "run_terminal" | "gui_agent",
  "target": "URL to open, or bash command to run, or Agent-S instruction (or empty for speak)",
  "spoken_response": "What you speak aloud to Justin"
}
"""


def decide_action(user_prompt: str) -> dict:
    """Ask the model router to determine action and spoken response."""
    payload = {
        "model": "dai/auto",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        f"{ROUTER_URL}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"].strip()
            # Parse json out of model response (even if wrapped in markdown blocks)
            clean_json = re.search(r"\{.*\}", content, re.DOTALL)
            if clean_json:
                return json.loads(clean_json.group(0))
            return {
                "action": "speak",
                "target": "",
                "spoken_response": content,
            }
    except Exception as e:
        return {
            "action": "speak",
            "target": "",
            "spoken_response": f"I had a momentary glitch connecting to my router: {e}",
        }


def handle_user_command(command: str) -> None:
    """Execute the user command based on AI intent."""
    print(f"\n[Command Received]: {command}")
    decision = decide_action(command)
    action = decision.get("action", "speak")
    target = decision.get("target", "").strip()
    spoken = decision.get("spoken_response", "")

    # Execute action
    if action == "open_web" and target:
        if not target.startswith("http://") and not target.startswith("https://"):
            target = f"https://{target}"
        print(f"[Opening Browser]: {target}")
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    elif action == "run_terminal" and target:
        print(f"[Running Command]: {target}")
        try:
            res = subprocess.run(target, shell=True, capture_output=True, text=True, timeout=15)
            output = res.stdout.strip() or res.stderr.strip() or "command completed with no output"
            # Summarize result for speech
            summary_req = {
                "model": "dai/auto",
                "messages": [
                    {"role": "system", "content": "Summarize this terminal output in 1 clear, spoken sentence for Justin."},
                    {"role": "user", "content": f"Command: {target}\nOutput:\n{output[:1500]}"},
                ],
            }
            req = urllib.request.Request(
                f"{ROUTER_URL}/v1/chat/completions",
                data=json.dumps(summary_req).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as sresp:
                sdata = json.loads(sresp.read().decode())
                spoken = sdata["choices"][0]["message"]["content"].strip()
        except Exception as e:
            spoken = f"I ran the command, but encountered an error: {e}"

    elif action == "gui_agent" and target:
        print(f"[Agent-S Task on :99]: {target}")
        try:
            task_req = urllib.request.Request(
                f"{WORKER_URL}/v1/tasks",
                data=json.dumps({"instruction": target, "dry_run": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(task_req, timeout=10)
        except Exception:
            pass

    # Speak back to Justin
    if spoken:
        speak_text(spoken)


def record_chunk(seconds: float = 3.0) -> bytes | None:
    """Record a chunk of raw 16kHz 16-bit mono PCM."""
    cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-d", str(int(seconds))]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 2)
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass
    return None


def pcm_to_wav(pcm_data: bytes) -> bytes:
    """Wrap PCM bytes into a standard WAV header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm_data)
    return buf.getvalue()


def proactive_health_watchdog() -> None:
    """Proactive background monitor: alerts Justin before problems happen."""
    while not _stop_event.is_set():
        # Sleep for 15 minutes between proactive checks
        _stop_event.wait(900)
        if _stop_event.is_set():
            break

        try:
            # 1. Check disk space on /
            st = os.statvfs("/")
            free_gb = (st.f_bavail * st.f_frsize) / (1024**3)
            if free_gb < 8.0:
                speak_text(f"Heads up Justin: your main drive is down to {free_gb:.1f} gigabytes free.")

            # 2. Check memory
            with open("/proc/meminfo") as f:
                meminfo = f.read()
            avail_match = re.search(r"MemAvailable:\s+(\d+)\s+kB", meminfo)
            if avail_match:
                avail_mb = int(avail_match.group(1)) / 1024
                if avail_mb < 800:
                    speak_text("Notice Justin: system memory is getting tight.")
        except Exception:
            pass


def main() -> None:
    print("=" * 60)
    print(" 🎙️  DAI GEMINI MODE — Hands-Free Voice Assistant Active")
    print(" Say: 'Hey DAI', 'Come here DAI', or 'DAI come here'")
    print(" Press Ctrl+C to stop")
    print("=" * 60)

    # Start proactive health monitor thread
    watchdog = threading.Thread(target=proactive_health_watchdog, daemon=True)
    watchdog.start()

    # Play startup chime
    play_chime()
    speak_text("D.A.I. Gemini mode is live. I'm listening.")

    def signal_handler(sig, frame):
        print("\nStopping DAI Gemini mode...")
        _stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while not _stop_event.is_set():
        pcm = record_chunk(CHUNK_SECONDS)
        if not pcm:
            time.sleep(0.2)
            continue

        rms = calculate_rms(pcm)
        # Skip silence
        if rms < RMS_THRESHOLD:
            continue

        # Speech detected: transcribe
        wav = pcm_to_wav(pcm)
        text = transcribe_wav(wav)
        if not text:
            continue

        # Check for wake word
        woke, command = extract_wake_and_command(text)
        if woke:
            print(f"\n[Wake Detected]: Heard '{text}'")
            play_chime()

            if command and len(command.split()) >= 2:
                # Command was spoken in the same sentence as wake word
                handle_user_command(command)
            else:
                # Wake word spoken alone: acknowledge and record the prompt
                speak_text("I'm here Justin, what's up?")
                followup_pcm = record_chunk(5.0)
                if followup_pcm:
                    followup_text = transcribe_wav(pcm_to_wav(followup_pcm))
                    if followup_text:
                        handle_user_command(followup_text)
                    else:
                        speak_text("I didn't catch that.")
        else:
            # Background chatter without wake word: ignore
            pass


if __name__ == "__main__":
    main()
