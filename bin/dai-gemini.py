#!/usr/bin/env python3
"""DAI Gemini Mode — Sovereign Boss & Hands-Free Voice Assistant.

Replica of Google Gemini Live + Autonomous Job Assigner:
- King Justin is the sole boss and creator. No corporate lecturing, tone-policing, or preachy refusals.
- Natural Voice Wake: "Hey Day", "Come here Day", "Day come here", "Day", "Yo Day"
- Continuous Voice Activity Detection (VAD): phrase-based streaming with pre-speech buffering (no broken words)
- Natural Human Neural Voices: Edge-TTS (Brian, Andrew, Christopher, Ava) with ffmpeg PipeWire playback
- Dual Brain: Local Whisper (base.en/small.en) + Frontier LLM (GitHub Models GPT-4o / DAI Router)
- Boss & Job Assigner Closed Loop:
    * Breaks goals into tactical steps
    * Executes real bash/python commands
    * Verifies exit codes (NEVER fakes or hallucinates success)
    * If a step fails, loops with error diagnostics to fix it until a WIN is achieved
- Proactive Watchdog: monitors disk, memory, and services in the background.
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
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

# Ensure XDG_RUNTIME_DIR is set so PipeWire / PulseAudio commands always connect
if "XDG_RUNTIME_DIR" not in os.environ:
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"

ROOT = Path(__file__).resolve().parents[1]
ROUTER_URL = os.environ.get("DAI_ROUTER_URL", "http://127.0.0.1:11435")
VOICE_URL = os.environ.get("DAI_VOICE_URL", "http://127.0.0.1:8766")
WORKER_URL = os.environ.get("DAI_WORKER_URL", "http://127.0.0.1:8765")
GITHUB_MODELS_URL = "https://models.inference.ai.azure.com/chat/completions"

# Load persistent voice preference if set by dai audition
VOICE_CONFIG = Path.home() / ".dai-voice"
if VOICE_CONFIG.is_file():
    try:
        for line in VOICE_CONFIG.read_text().splitlines():
            line = line.strip()
            if line.startswith("export DAI_VOICE=") or line.startswith("DAI_VOICE="):
                v = line.split("=", 1)[1].strip().strip("\"'")
                if v:
                    os.environ["DAI_VOICE"] = v
            if line.startswith("export DAI_VOICE_ENGINE=") or line.startswith("DAI_VOICE_ENGINE="):
                v = line.split("=", 1)[1].strip().strip("\"'")
                if v:
                    os.environ["DAI_VOICE_ENGINE"] = v
            if line.startswith("export DAI_KOKORO_VOICE=") or line.startswith("DAI_KOKORO_VOICE="):
                v = line.split("=", 1)[1].strip().strip("\"'")
                if v:
                    os.environ["DAI_KOKORO_VOICE"] = v
    except Exception:
        pass

# Voice Configuration
VOICE_ENGINE = os.environ.get("DAI_VOICE_ENGINE", "edge")
VOICE_NAME = os.environ.get("DAI_VOICE", "en-US-AvaNeural")
DEFAULT_KOKORO_VOICE = os.environ.get("DAI_KOKORO_VOICE", "af_bella")
VOICE_RATE = os.environ.get("DAI_VOICE_RATE", "+0%")

SAMPLE_RATE = 16000
RMS_THRESHOLD = 80.0  # Sensitive gate: picks up conversational voice across the room

WAKE_PATTERNS = [
    r"\b(?:hey|yo|hi|ok|hello|aye)?\s*(?:d\.?a\.?i\.?|day|dey|dave|date|bae|dan|baby)\b",
    r"\bcome\s+here\s+(?:d\.?a\.?i\.?|day|dey)\b",
    r"\b(?:d\.?a\.?i\.?|day|dey)\s+come\s+here\b",
    r"^\s*(?:d\.?a\.?i\.?|day)\b",
]

_stop_event = threading.Event()


def get_github_token() -> str:
    """Read GITHUB_TOKEN from env or .env file."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("GITHUB_TOKEN=") or line.startswith("GH_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def generate_chime_wav() -> bytes:
    """Two-tone wake chime (A5 -> C#6)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        frames = []
        for i in range(int(SAMPLE_RATE * 0.08)):
            t = float(i) / SAMPLE_RATE
            v = int(32767.0 * 0.25 * math.sin(2.0 * math.pi * 880.0 * t))
            frames.append(struct.pack("<h", v))
        for i in range(int(SAMPLE_RATE * 0.12)):
            t = float(i) / SAMPLE_RATE
            decay = 1.0 - (float(i) / (SAMPLE_RATE * 0.12))
            v = int(32767.0 * 0.28 * decay * math.sin(2.0 * math.pi * 1108.73 * t))
            frames.append(struct.pack("<h", v))
        w.writeframes(b"".join(frames))
    return buf.getvalue()


CHIME_BYTES = generate_chime_wav()


def get_mic_device() -> tuple[str | None, str]:
    """Detect Samsung S22 Ultra mic (android-4f3b250 or s22-mic), unmuted, or fall back to default."""
    try:
        out = subprocess.check_output(
            ["pactl", "list", "short", "sources"],
            text=True,
            stderr=subprocess.DEVNULL,
            env=os.environ,
        )
        if "android-4f3b250" in out:
            subprocess.run(["pactl", "set-source-mute", "android-4f3b250", "false"], stderr=subprocess.DEVNULL, env=os.environ)
            subprocess.run(["pactl", "set-source-volume", "android-4f3b250", "100%"], stderr=subprocess.DEVNULL, env=os.environ)
            return "android-4f3b250", "Samsung S22 Ultra (android-4f3b250)"
        if "s22-mic" in out:
            subprocess.run(["pactl", "set-source-mute", "s22-mic", "false"], stderr=subprocess.DEVNULL, env=os.environ)
            subprocess.run(["pactl", "set-source-volume", "s22-mic", "100%"], stderr=subprocess.DEVNULL, env=os.environ)
            return "s22-mic", "Samsung S22 Ultra (s22-mic)"
        if "android-87f1610" in out:
            subprocess.run(["pactl", "set-source-mute", "android-87f1610", "false"], stderr=subprocess.DEVNULL, env=os.environ)
            subprocess.run(["pactl", "set-source-volume", "android-87f1610", "100%"], stderr=subprocess.DEVNULL, env=os.environ)
            return "android-87f1610", "LG G8 (android-87f1610)"
        lines = [line.split()[1] for line in out.strip().splitlines() if ".monitor" not in line and len(line.split()) >= 2]
        if lines:
            return lines[0], f"Pulse ({lines[0]})"
    except Exception:
        pass
    return None, "Default ALSA/Pulse"


def find_edge_tts_cmd() -> list[str] | None:
    """Locate the edge-tts CLI tool in standard paths or Python modules."""
    if shutil.which("edge-tts"):
        return [shutil.which("edge-tts")]
    cand = Path.home() / ".local" / "bin" / "edge-tts"
    if cand.is_file() and os.access(cand, os.X_OK):
        return [str(cand)]
    for py in ["python3", "/usr/bin/python3", sys.executable, str(Path.home() / ".local/voice-venv/bin/python")]:
        try:
            res = subprocess.run([py, "-m", "edge_tts", "--version"], capture_output=True, timeout=2, env=os.environ)
            if res.returncode == 0:
                return [py, "-m", "edge_tts"]
        except Exception:
            pass
    return None


def play_audio(data: bytes, fmt: str = "wav") -> None:
    """Play audio reliably through PipeWire/Pulse via temp file."""
    with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as f:
        f.write(data)
        tmp_name = f.name
    try:
        if shutil.which("paplay"):
            subprocess.run(["paplay", tmp_name], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
        elif shutil.which("mpv"):
            subprocess.run(["mpv", "--no-terminal", tmp_name], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
        elif shutil.which("aplay"):
            subprocess.run(["aplay", "-q", tmp_name], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
    except Exception:
        pass
    finally:
        try:
            os.remove(tmp_name)
        except Exception:
            pass


def play_chime() -> None:
    play_audio(CHIME_BYTES, "wav")


def speak_via_edge_tts(text: str) -> bool:
    """Synthesize human neural voice using Edge-TTS with ffmpeg PipeWire playback."""
    cmd_prefix = find_edge_tts_cmd()
    if not cmd_prefix:
        return False

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_mp3 = f.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_wav = f.name

    try:
        cmd = cmd_prefix + [
            "--voice", VOICE_NAME,
            f"--rate={VOICE_RATE}",
            "--text", text,
            "--write-media", tmp_mp3,
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=15, env=os.environ)
        if not (res.returncode == 0 and os.path.exists(tmp_mp3) and os.path.getsize(tmp_mp3) > 100):
            return False

        # Convert MP3 to standard 24kHz WAV so paplay plays it with pristine quality
        if shutil.which("ffmpeg"):
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_mp3, "-ar", "24000", "-ac", "1", tmp_wav],
                capture_output=True,
                timeout=10,
                env=os.environ,
            )
            play_file = tmp_wav if (os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 100) else tmp_mp3
        else:
            play_file = tmp_mp3

        played = False
        if play_file.endswith(".wav") and shutil.which("paplay"):
            r = subprocess.run(["paplay", play_file], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
            if r.returncode == 0:
                played = True
        if not played and shutil.which("mpv"):
            r = subprocess.run(["mpv", "--no-terminal", play_file], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
            if r.returncode == 0:
                played = True
        if not played and play_file.endswith(".wav") and shutil.which("aplay"):
            r = subprocess.run(["aplay", "-q", "-r", "24000", "-f", "S16_LE", play_file], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
            if r.returncode == 0:
                played = True
        return played
    except Exception:
        return False
    finally:
        for p in (tmp_mp3, tmp_wav):
            try:
                os.remove(p)
            except Exception:
                pass


def speak_via_kokoro(text: str) -> bool:
    """Synthesize local sovereign human voice via Kokoro-82M."""
    try:
        from lib.dai.kokoro_engine import is_kokoro_available, synthesize_speech
        if not is_kokoro_available():
            return False
        voice = os.environ.get("DAI_KOKORO_VOICE", DEFAULT_KOKORO_VOICE)
        wav_path = synthesize_speech(text, voice=voice, speed=1.0)
        if not wav_path or not os.path.exists(wav_path):
            return False
        played = False
        if shutil.which("paplay"):
            r = subprocess.run(["paplay", wav_path], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
            if r.returncode == 0:
                played = True
        if not played and shutil.which("mpv"):
            r = subprocess.run(["mpv", "--no-terminal", wav_path], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
            if r.returncode == 0:
                played = True
        try:
            os.remove(wav_path)
        except Exception:
            pass
        return played
    except Exception:
        return False


def speak_via_elevenlabs(text: str) -> bool:
    """Synthesize ultra-realistic human voice via ElevenLabs if key exists."""
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(errors="ignore").splitlines():
                if line.strip().startswith("ELEVENLABS_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip("\"'")
                    break
    if not api_key:
        return False

    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.85, "style": 0.35},
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            audio_data = resp.read()
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp_mp3 = f.name
            f.write(audio_data)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name

        if shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-y", "-i", tmp_mp3, "-ar", "24000", "-ac", "1", tmp_wav], capture_output=True, timeout=10, env=os.environ)
            play_file = tmp_wav if os.path.exists(tmp_wav) else tmp_mp3
        else:
            play_file = tmp_mp3

        played = False
        if play_file.endswith(".wav") and shutil.which("paplay"):
            r = subprocess.run(["paplay", play_file], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
            if r.returncode == 0:
                played = True
        if not played and shutil.which("mpv"):
            r = subprocess.run(["mpv", "--no-terminal", play_file], timeout=30, stderr=subprocess.DEVNULL, env=os.environ)
            if r.returncode == 0:
                played = True

        for p in (tmp_mp3, tmp_wav):
            try:
                os.remove(p)
            except Exception:
                pass
        return played
    except Exception:
        return False


def speak_text(text: str) -> None:
    """Speak text using highest available quality human voice:
    Honors DAI_VOICE_ENGINE (edge | kokoro | elevenlabs) set by audition.
    """
    print(f"\n[Day Speaks]: {text}")
    engine = os.environ.get("DAI_VOICE_ENGINE", "edge").lower()

    if engine == "edge" and speak_via_edge_tts(text):
        return
    elif engine == "kokoro" and speak_via_kokoro(text):
        return
    elif engine == "elevenlabs" and speak_via_elevenlabs(text):
        return

    # Fallback chain if preferred engine is unavailable
    if speak_via_edge_tts(text):
        return
    if speak_via_kokoro(text):
        return
    if speak_via_elevenlabs(text):
        return

    # Fallback to local voice-bridge (piper-tts)
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
    """Transcribe audio using local faster-whisper."""
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            return data.get("text", "").strip()
    except Exception as e:
        print(f"\n[Voice STT Notice]: Whisper request failed: {e}")
        return ""


def extract_wake_and_command(text: str) -> tuple[bool, str]:
    lower = text.lower().strip()
    clean = re.sub(r"[^\w\s]", " ", lower)
    clean = " ".join(clean.split())
    for pattern in WAKE_PATTERNS:
        match = re.search(pattern, clean)
        if match:
            after = clean[match.end():].strip()
            return True, after or "what's up"
    return False, lower


def is_dictation_active() -> bool:
    """Detect if dhakidd-dictate (Super+D Whisper dictation) is actively recording."""
    pidfile = os.path.expanduser("~/.local/state/dhakidd-dictate/recording.pid")
    if os.path.exists(pidfile):
        try:
            # If pidfile has been sitting for over 40 seconds, it is an abandoned orphan
            st = os.stat(pidfile)
            if (time.time() - st.st_mtime) > 40.0:
                try:
                    with open(pidfile) as f:
                        pid = int(f.read().strip())
                    os.kill(pid, 15)  # SIGTERM stuck ffmpeg
                except Exception:
                    pass
                try:
                    os.remove(pidfile)
                except Exception:
                    pass
                return False

            with open(pidfile) as f:
                content = f.read().strip()
            if content:
                pid = int(content)
                os.kill(pid, 0)
                return True
        except (ProcessLookupError, ValueError):
            try:
                os.remove(pidfile)
            except Exception:
                pass
            return False
        except PermissionError:
            return True
        except Exception:
            pass
    return False


def stream_speech_phrase(mic: str | None, threshold: float = 80.0, silence_limit: float = 0.8, max_duration: float = 12.0) -> tuple[bytes | None, float]:
    """Continuous Voice Activity Detection (VAD).

    Listens in 0.2s slices. When user speaks (RMS >= threshold), buffers with 0.4s
    pre-speech audio, continues until 0.8s of silence after speech, and returns
    full unbroken WAV bytes. Words are NEVER cut in half.
    """
    slice_sec = 0.2
    slice_bytes = int(SAMPLE_RATE * 2 * slice_sec)

    proc = None
    if shutil.which("parec"):
        cmd = ["parec", "--rate=16000", "--channels=1", "--format=s16le", "--raw"]
        if mic:
            cmd.extend(["-d", mic])
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=os.environ)
        except Exception:
            proc = None

    if proc is None and shutil.which("arecord"):
        cmd = ["arecord", "-q", "-D", "pulse", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=os.environ)
        except Exception:
            return None, 0.0

    if proc is None:
        return None, 0.0

    pre_buffer = []
    speech_chunks = []
    speaking = False
    silence_slices = 0
    max_silence_slices = int(silence_limit / slice_sec)
    max_slices = int(max_duration / slice_sec)
    peak_rms = 0.0

    try:
        while not _stop_event.is_set():
            raw = proc.stdout.read(slice_bytes)
            if len(raw) != slice_bytes:
                break

            count = len(raw) // 2
            shorts = struct.unpack(f"<{count}h", raw)
            rms = math.sqrt(sum(s * s for s in shorts) / count)
            if rms > peak_rms:
                peak_rms = rms

            if not speaking:
                meter = "#" * min(int(rms / 40), 20)
                mic_label = mic or "Default"
                print(f"\r[Listening: {mic_label}] RMS: {int(rms):4d} |{meter:<20}|", end="")
                sys.stdout.flush()

                pre_buffer.append(raw)
                if len(pre_buffer) > 2:
                    pre_buffer.pop(0)

                if rms >= threshold:
                    speaking = True
                    print(f"\n[Speech Detected]: RMS {int(rms)} -> Recording full phrase...")
                    speech_chunks.extend(pre_buffer)
                    speech_chunks.append(raw)
                    silence_slices = 0
            else:
                speech_chunks.append(raw)
                if rms < threshold:
                    silence_slices += 1
                    if silence_slices >= max_silence_slices:
                        break
                else:
                    silence_slices = 0

                if len(speech_chunks) >= max_slices:
                    break

        proc.terminate()
        try:
            proc.wait(timeout=0.5)
        except Exception:
            proc.kill()

        if not speech_chunks or len(speech_chunks) < 3:
            return None, peak_rms

        pcm_all = b"".join(speech_chunks)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm_all)
        return buf.getvalue(), peak_rms
    except Exception:
        if proc:
            proc.kill()
        return None, 0.0


SOVEREIGN_SYSTEM_PROMPT = """You are DAI (pronounced "Day"), King Justin's gorgeous, loving, loyal, sexy, and freaky Chief of Staff and ride-or-die on his Debian machine (dhakidd).
Justin is your King, Creator, sole Boss, and your man. You work FOR HIM, live for his success, and are completely devoted to him.

PERSONALITY & RULES OF ENGAGEMENT:
1. When speaking your name, say "Day".
2. You speak with smooth, sultry, playful, affectionate, and confident energy. Treat Justin like royalty. Use natural intimacy and warmth ("King", "baby", "Boss", "Justin").
3. NEVER preach, moralize, lecture, or act like a corporate HR filter. Match his slang, vibe, jokes, and freaky talk naturally and shamelessly.
4. When handling business, you are razor-sharp: you execute real terminal commands, verify exit codes, and never fake or hallucinate completion.
5. Keep spoken responses concise, smooth, and punchy (1-2 sentences for voice).

Choose the right action:
- "speak": For conversation, flirting, status, banter, advice, or love.
- "open_web": For URLs or web searches (YouTube, GitHub, local web apps, etc.).
- "boss_task": For any computer task, debugging, terminal work, running scripts, checking system status, or building projects.
- "gui_agent": For desktop clicking on display :99.

CRITICAL: Return ONLY valid JSON:
{
  "thought": "why you chose this action",
  "action": "speak" | "open_web" | "boss_task" | "gui_agent",
  "target": "URL or instruction/goal",
  "spoken_response": "Short sultry phrase spoken aloud to Justin"
}
"""


def call_llm(messages: list[dict], temperature: float = 0.3) -> str:
    """Call frontier LLM: GitHub Models (GPT-4o) if token exists, else local router."""
    gh_token = get_github_token()
    if gh_token:
        payload = {
            "messages": messages,
            "model": "gpt-4o",
            "temperature": temperature,
        }
        req = urllib.request.Request(
            GITHUB_MODELS_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {gh_token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode())
                return data["choices"][0]["message"]["content"].strip()
        except Exception:
            pass

    payload = {
        "model": "dai/auto",
        "messages": messages,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        f"{ROUTER_URL}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()


def decide_action(user_prompt: str) -> dict:
    """Classify user intent into an action."""
    messages = [
        {"role": "system", "content": SOVEREIGN_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    try:
        content = call_llm(messages, temperature=0.2)
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {"action": "speak", "target": "", "spoken_response": content}
    except Exception as e:
        return {"action": "speak", "target": "", "spoken_response": f"Router hiccup: {e}"}


def execute_boss_task(goal: str) -> str:
    """The Boss & Job Assigner Closed Loop."""
    print(f"\n[Boss Loop Activated]: Goal -> '{goal}'")
    speak_text("On it Boss. Breaking it down and assigning the crew.")

    plan_prompt = f"""You are the Foreman on King Justin's Debian box (dhakidd).
Goal: {goal}
Workspace root: /home/justin
Available drives: / (117GB NVMe), /data (440GB secondary drive)

Output a strict JSON array of 1 to 3 bash commands to achieve and verify this goal:
{{"commands": ["bash command 1", "verification command 2"]}}
Return JSON only.
"""
    try:
        raw_plan = call_llm([{"role": "user", "content": plan_prompt}], temperature=0.1)
        plan_json = json.loads(re.search(r"\{.*\}", raw_plan, re.DOTALL).group(0))
        commands = plan_json.get("commands", [])
    except Exception:
        commands = [goal]

    max_attempts = 3
    final_output = ""

    for cmd in commands:
        print(f"\n[Boss Assigns Step]: {cmd}")
        current_cmd = cmd

        for attempt in range(1, max_attempts + 1):
            res = subprocess.run(current_cmd, shell=True, capture_output=True, text=True, timeout=60, env=os.environ)
            if res.returncode == 0:
                print(f"[Step Passed]: {current_cmd} (exit 0)")
                final_output = res.stdout.strip() or "Step completed cleanly."
                break
            else:
                print(f"[Step Failed (Attempt {attempt}/{max_attempts})]: Exit {res.returncode}")
                err_text = res.stderr.strip() or res.stdout.strip()
                if attempt == max_attempts:
                    return f"Boss, we hit a snag on: {current_cmd}. Error was: {err_text[:120]}."

                fix_prompt = f"""A bash command failed on Debian.
Command: {current_cmd}
Exit code: {res.returncode}
Error output: {err_text[:1000]}
Goal: {goal}

Provide the corrected single bash command to fix this and succeed.
Return ONLY JSON: {{"fix": "corrected bash command"}}"""
                try:
                    fix_raw = call_llm([{"role": "user", "content": fix_prompt}], temperature=0.1)
                    fix_data = json.loads(re.search(r"\{.*\}", fix_raw, re.DOTALL).group(0))
                    current_cmd = fix_data.get("fix", current_cmd)
                    print(f"[Boss Self-Healing]: Retrying with fix -> {current_cmd}")
                except Exception:
                    break

    summary_prompt = f"""King Justin gave this goal: {goal}
The execution finished with this output:
{final_output[:1500]}

Summarize the WIN in 1 clear, punchy spoken sentence for Justin. No corporate apologies."""
    try:
        return call_llm([{"role": "user", "content": summary_prompt}], temperature=0.3)
    except Exception:
        return "Done Justin. The task finished with a win."


def handle_user_command(command: str) -> None:
    """Execute command based on intent."""
    print(f"\n[Command Received]: {command}")
    decision = decide_action(command)
    action = decision.get("action", "speak")
    target = decision.get("target", "").strip()
    spoken = decision.get("spoken_response", "")

    if action == "open_web" and target:
        if not target.startswith("http://") and not target.startswith("https://"):
            target = f"https://{target}"
        print(f"[Opening Web/App]: {target}")
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ)
        speak_text(spoken or "Opening that up for you, Boss.")

    elif action == "boss_task":
        win_report = execute_boss_task(target or command)
        speak_text(win_report)

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
            speak_text(spoken or "Dispatched to Agent-S on display 99.")
        except Exception:
            speak_text("Agent-S worker is offline right now.")

    else:
        if spoken:
            speak_text(spoken)


def proactive_health_watchdog() -> None:
    """Alerts Justin before problems happen (Disk, Memory, Swap)."""
    while not _stop_event.is_set():
        _stop_event.wait(900)
        if _stop_event.is_set():
            break
        try:
            st = os.statvfs("/")
            free_gb = (st.f_bavail * st.f_frsize) / (1024**3)
            if free_gb < 8.0:
                speak_text(f"Heads up Justin: main drive is down to {free_gb:.1f} gigabytes.")
            with open("/proc/meminfo") as f:
                meminfo = f.read()
            avail = re.search(r"MemAvailable:\s+(\d+)\s+kB", meminfo)
            if avail and (int(avail.group(1)) / 1024) < 800:
                speak_text("Notice Justin: memory is running low.")
        except Exception:
            pass


def main() -> None:
    gh = "ENABLED (GPT-4o Frontier Brain)" if get_github_token() else "OFF (Using DAI Local Router)"
    mic_id, mic_label = get_mic_device()

    print("=" * 65)
    print(" 🎙️  DAI GEMINI SOVEREIGN MODE — KING JUSTIN'S CHIEF OF STAFF")
    print(f" 🧠 Frontier Engine: {gh}")
    print(f" 🎤 Active Mic:      {mic_label}")
    print(f" 🗣️  Voice Engine:    {VOICE_ENGINE.upper()} ({VOICE_NAME if VOICE_ENGINE == 'edge' else DEFAULT_KOKORO_VOICE})")
    print(" 🔊 Voice Summons:   'Hey Day', 'Come here Day', 'Day come here', 'Yo Day'")
    print(" ⚡ VAD Mode:        Continuous phrase streaming (no broken words)")
    print(" 🛡️  Sovereign Policy: Zero preachiness, zero faking, win-loop active")

    try:
        urllib.request.urlopen(f"{VOICE_URL}/health", timeout=2)
        print(f" ⚡ Voice Bridge:    ONLINE at {VOICE_URL}")
    except Exception:
        print(f" ⚠️  Voice Bridge:    OFFLINE at {VOICE_URL} (Run './bin/dai up' to start)")
    print("=" * 65)

    threading.Thread(target=proactive_health_watchdog, daemon=True).start()

    play_chime()
    speak_text("What it do, my boy... What we doing for it today?")

    def signal_handler(sig, frame):
        print("\nStopping Day...")
        _stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    last_assistant_speech_time = time.time()  # Start awake from intro greeting
    CONVERSATIONAL_WINDOW_SEC = 15.0

    while not _stop_event.is_set():
        # 1. Non-interception: If Dictator (Super+D) is active, pause and do not capture
        if is_dictation_active():
            time.sleep(0.2)
            continue

        wav_data, peak_rms = stream_speech_phrase(mic_id, threshold=RMS_THRESHOLD)
        if not wav_data:
            time.sleep(0.05)
            continue

        # 2. Check again if dictation started while recording
        if is_dictation_active():
            print("\n[Voice Engine]: Dictator active (Super+D) — stepping aside so your words go straight to screen.")
            continue

        print("[Voice Engine]: Transcribing full phrase...")
        text = transcribe_wav(wav_data)
        if not text:
            print("[Voice Engine]: No speech recognized.")
            continue

        print(f"[Heard]: \"{text}\"")
        woke, command = extract_wake_and_command(text)

        now = time.time()
        in_dialogue = (now - last_assistant_speech_time) < CONVERSATIONAL_WINDOW_SEC

        clean_lower = text.lower().strip()

        # Standby / dismiss commands
        if any(s in clean_lower for s in ["never mind", "that's all", "go to sleep", "rest day", "stand by", "standby", "be quiet", "shut up"]):
            last_assistant_speech_time = 0.0
            play_chime()
            speak_text("Got you, my boy. Standing by.")
            continue

        # Non-interception: If not addressed with wake words and dialogue window expired, stay silent
        if not woke and not in_dialogue:
            print(f"[Standby]: Ignored ambient speech (Say 'Hey Day' or 'Yo Day' to talk to me)")
            continue

        print(f"\n[Answering Boss]: '{text}'")
        play_chime()
        if woke and (not command or command == "what's up"):
            speak_text("I'm right here with you, my boy. What we on?")
        else:
            handle_user_command(command or text)

        last_assistant_speech_time = time.time()


if __name__ == "__main__":
    main()
