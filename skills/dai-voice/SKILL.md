---
name: dai-voice
description: "Give the assistant a voice. Use when the owner wants something said out loud, asks the assistant to announce or check in, wants a status reminder ('are you still there?'), or when a natural spoken confirmation beats a wall of text. Talks through the spine's voice-bridge (dai say / dai heartbeat), which is offline and free."
compatibility: "Designed for Vellum personal assistants on the Debian AI integration spine"
metadata:
  emoji: "🗣️"
  vellum:
    category: "communication"
    display-name: "DAI Voice"
    activation-hints:
      - "Load when the owner wants speech output or spoken status, not just text"
      - "Load when the owner asks whether the assistant/spine is still active"
---

# DAI Voice

The assistant has a voice: the spine's voice-bridge synthesises speech fully
locally (piper TTS) and the CLI hands you two tools.

## Commands

```bash
# Speak any text. Saves an mp3 under state/speech/ and plays it if a local
# audio player exists (headless boxes just save the file and say so).
./bin/dai say "Kettle's boiled, by the way."

# Say — and speak — a free-form status line built from live service health.
./bin/dai heartbeat --speak

# Keep reminding the owner the assistant is active: one spoken blip every N
# seconds, until killed.  For a standing schedule, run it in the background:
./bin/dai heartbeat --speak --every 900
nohup ./bin/dai heartbeat --speak --every 900 >> logs/heartbeat.log 2>&1 &
```

Every result is a JSON document on stdout: `{"ok": true, "line", "audio",
"bytes", "played"}` or `{"ok": false, "error", "detail"}`. Branch on `ok` /
`error`, never on prose.

## Speaking well (the "freely" part)

1. **First person, plain words.** You are D-A-I. Say "I'm up and ready", not
   "service status: nominal". The owner hears a person, not a dashboard.
2. **Short by default.** One or two sentences for a blip; the `heartbeat`
   line is already sized for this. Reserve longer speech for when the owner
   asked for it.
3. **Honest state.** Speak what the services actually say: if the router is
   not ready for chat, say there's no key yet — never announce readiness you
   didn't check. `dai heartbeat` builds the line from live `/health`
   payloads, so it is accurate by construction.
4. **No secrets, ever, out loud.** Nothing from `.env`, no tokens, no paths
   the owner didn't ask for. Spoken words are the most exposed text you
   produce.
5. **Don't spam.** Speak on request, on state change, or on a schedule the
   owner set. If the owner never asked for scheduled blips, offer one rather
   than starting one.

## When to use heartbeat vs say

* "Are you still there?" / "give me a status" → `dai heartbeat --speak`
  (accurate, consistent phrasing, includes what's down and where to look).
* Anything else you want to say (confirmation, reminder, news) → `dai say`.
* A recurring "I'm active" reminder → `dai heartbeat --speak --every N` in
  the background; mention the PID file it writes to `logs/heartbeat.log`
  tells the owner how to stop it (`pkill -f 'speak.py heartbeat'`).

## Pre-flight (and honest limits)

* The voice stack lives in `~/.local/voice-venv` (`faster-whisper`,
  `piper-tts`); models sit in `~/.local/voice-models`. If `dai say` returns
  `voice_bridge_down`, the spine needs a restart (`dai up`) or the venv is
  missing — say so, with the install command, instead of faking speech.
* Non-wav formats need `ffmpeg` on PATH; without it, use `--format wav`.
* Spoken output is one-way: the owner's microphone is a separate matter
  (`/v1/audio/transcriptions`, needs the faster-whisper model). Don't claim
  you can hear back until STT is confirmed working.
