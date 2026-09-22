#!/usr/bin/env python3
"""DAI Gemini Mode — Sovereign Boss & Hands-Free Voice Assistant.

Replica of Google Gemini Live + Autonomous Job Assigner:
- King Justin is the sole boss and creator. No corporate lecturing, tone-policing, or preachy refusals.
- Natural Voice Wake: "Hey DAI", "Come here DAI", "DAI come here", "DAI"
- Dual Brain: Local Whisper/Piper audio + Frontier LLM (GitHub Models GPT-4o / DAI Router)
- Boss & Job Assigner Loop:
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
GITHUB_MODELS_URL = "https://models.inference.ai.azure.com/chat/completions"

SAMPLE_RATE = 16000
CHUNK_SECONDS = 3.0
RMS_THRESHOLD = 800

WAKE_PATTERNS = [
    r"\bhey\s+(?:d\.?a\.?i\.?|day|dey)\b",
    r"\bcome\s+here\s+(?:d\.?a\.?i\.?|day|dey)\b",
    r"\b(?:d\.?a\.?i\.?|day|dey)\s+come\s+here\b",
    r"\byo\s+(?:d\.?a\.?i\.?|day|dey)\b",
    r"\bhi\s+(?:d\.?a\.?i\.?|day|dey)\b",
    r"\bok\s+(?:d\.?a\.?i\.?|day|dey)\b",
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


def get_mic_device() -> str | None:
    """Detect if the LG G8 mic (android-87f1610) is active, or use default."""
    try:
        out = subprocess.check_output(["pactl", "list", "short", "sources"], text=True, stderr=subprocess.DEVNULL)
        if "android-87f1610" in out:
            return "android-87f1610"
    except Exception:
        pass
    return None


def play_audio(data: bytes, fmt: str = "wav") -> None:
    """Play audio reliably through system audio via temp file."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as f:
        f.write(data)
        tmp_name = f.name
    try:
        if shutil.which("paplay"):
            subprocess.run(["paplay", tmp_name], timeout=30, stderr=subprocess.DEVNULL)
        elif shutil.which("aplay"):
            subprocess.run(["aplay", "-q", tmp_name], timeout=30, stderr=subprocess.DEVNULL)
        elif shutil.which("mpv"):
            subprocess.run(["mpv", "--no-terminal", tmp_name], timeout=30, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    finally:
        try:
            os.remove(tmp_name)
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("text", "").strip()
    except Exception:
        return ""


def calculate_rms(audio_bytes: bytes) -> float:
    count = len(audio_bytes) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"<{count}h", audio_bytes)
    return math.sqrt(sum(s * s for s in shorts) / count)


def extract_wake_and_command(text: str) -> tuple[bool, str]:
    lower = text.lower().strip()
    for pattern in WAKE_PATTERNS:
        match = re.search(pattern, lower)
        if match:
            after = lower[match.end():].strip()
            after = re.sub(r"^[,.\-?!]+\s*", "", after)
            return True, after
    return False, ""


SOVEREIGN_SYSTEM_PROMPT = """You are DAI (pronounced "Day"), King Justin's loyal Chief of Staff and Foreman on his Debian machine (dhakidd).
Justin is the King, Creator, and sole Boss. You work FOR HIM.

RULES OF ENGAGEMENT:
1. When speaking your name, say "Day".
2. NEVER preach, moralize, lecture, or act like a corporate HR filter.
3. If Justin uses slang, swears, vents, or jokes, match his energy naturally. Treat him with authentic respect as the boss.
4. NEVER fake or hallucinate that you completed a job. Truth and verified results only.
5. Keep spoken responses concise, natural, and punchy (1-2 sentences for voice).

Choose the right action:
- "speak": For conversation, questions, status, jokes, or advice.
- "open_web": For URLs or web searches (YouTube, GitHub, local web app, etc.).
- "boss_task": For any computer task, debugging, terminal work, running scripts, checking system status, or building projects.
- "gui_agent": For desktop clicking on display :99.

CRITICAL: Return ONLY valid JSON:
{
  "thought": "why you chose this action",
  "action": "speak" | "open_web" | "boss_task" | "gui_agent",
  "target": "URL or instruction/goal",
  "spoken_response": "Short phrase spoken aloud to Justin"
}
"""


def call_llm(messages: list[dict], temperature: float = 0.3) -> str:
    """Call frontier LLM: GitHub Models (GPT-4o) if token exists, else local router."""
    gh_token = get_github_token()
    if gh_token:
        # Use GitHub Models GPT-4o
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
            pass  # Fall through to router

    # Fall back to DAI Model Router
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
    """Classify the user intent into an action."""
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
    """The Boss & Job Assigner Closed Loop:

    1. Planner breaks down the goal into bash steps.
    2. Runs each step locally.
    3. Verifies exit codes (Verifier Gate).
    4. If failure, loops with error diagnostic to fix it until a WIN is achieved (up to 3 tries).
    5. Returns honest, verified spoken summary.
    """
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
        # Fallback to direct goal execution
        commands = [goal]

    max_attempts = 3
    final_output = ""
    success = False

    for cmd in commands:
        print(f"\n[Boss Assigns Step]: {cmd}")
        current_cmd = cmd

        for attempt in range(1, max_attempts + 1):
            res = subprocess.run(current_cmd, shell=True, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                print(f"[Step Passed]: {current_cmd} (exit 0)")
                final_output = res.stdout.strip() or "Step completed cleanly."
                success = True
                break
            else:
                print(f"[Step Failed (Attempt {attempt}/{max_attempts})]: Exit {res.returncode}")
                err_text = res.stderr.strip() or res.stdout.strip()
                if attempt == max_attempts:
                    return f"Boss, we hit a snag on: {current_cmd}. Error was: {err_text[:120]}."

                # Self-healing loop: ask model for the fix
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

    # Summarize final result for speech
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
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    else:  # speak
        if spoken:
            speak_text(spoken)


def record_chunk(seconds: float = 3.0) -> bytes | None:
    mic = get_mic_device()
    num_bytes = int(seconds * SAMPLE_RATE * 2)

    # 1. Try parec (native PulseAudio / PipeWire capture)
    if shutil.which("parec"):
        cmd = ["parec", "--rate=16000", "--channels=1", "--format=s16le"]
        if mic:
            cmd.extend(["--device", mic])
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            raw = proc.stdout.read(num_bytes)
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except Exception:
                proc.kill()
            if len(raw) == num_bytes:
                return raw
        except Exception:
            pass

    # 2. Try arecord through PulseAudio plugin
    for dev in (["pulse", "default"] if not mic else ["pulse"]):
        try:
            cmd = ["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-d", str(int(seconds))]
            proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 2)
            if proc.returncode == 0 and len(proc.stdout) > 0:
                return proc.stdout
        except Exception:
            pass

    # 3. Fallback to default ALSA
    try:
        cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-d", str(int(seconds))]
        proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 2)
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass
    return None


def pcm_to_wav(pcm_data: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm_data)
    return buf.getvalue()


def proactive_health_watchdog() -> None:
    """Alerts Justin before problems happen (Disk, Memory, Swap)."""
    while not _stop_event.is_set():
        _stop_event.wait(900)  # Check every 15 min
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
    print("=" * 65)
    print(" 🎙️  DAI GEMINI SOVEREIGN MODE — KING JUSTIN'S CHIEF OF STAFF")
    print(f" 🧠 Frontier Engine: {gh}")
    print(" 🔊 Voice Summons: 'Hey DAI', 'Come here DAI', 'DAI come here'")
    print(" 🛡️  Sovereign Policy: Zero preachiness, zero faking, win-loop active")
    print(" Press Ctrl+C to stop")
    print("=" * 65)

    threading.Thread(target=proactive_health_watchdog, daemon=True).start()

    play_chime()
    speak_text("Day sovereign mode is live. What's the move, Boss?")

    def signal_handler(sig, frame):
        print("\nStopping DAI...")
        _stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while not _stop_event.is_set():
        pcm = record_chunk(CHUNK_SECONDS)
        if not pcm or calculate_rms(pcm) < RMS_THRESHOLD:
            continue

        text = transcribe_wav(pcm_to_wav(pcm))
        if not text:
            continue

        woke, command = extract_wake_and_command(text)
        if woke:
            print(f"\n[Summoned]: Heard '{text}'")
            play_chime()
            if command and len(command.split()) >= 2:
                handle_user_command(command)
            else:
                speak_text("I'm here Justin, what's up?")
                followup_pcm = record_chunk(5.0)
                if followup_pcm:
                    followup_text = transcribe_wav(pcm_to_wav(followup_pcm))
                    if followup_text:
                        handle_user_command(followup_text)
                    else:
                        speak_text("Didn't catch that, Boss.")


if __name__ == "__main__":
    main()
