# Sovereign System Map: DHakidd Desktop & Samsung S22 Unified Audio

**Target Machine**: Debian 13.6 (trixie) | Host: `dhakidd` | User: `justin` | Desktop: MATE / X11  
**Last Verified & Hardened**: September 23, 2026  
**Status**: Cleaned, Consolidated, and Verified Sovereign  

---

## 1. Device Map & Boundary Enforcement

| # | System / Device | Hardware Identity / Serial | Assigned Sovereign Role | Strict Security Boundary |
|---|---|---|---|---|
| 1 | **Debian Desktop** | Host `dhakidd`, User `justin`, Kernel 6.12 | **Primary Computer** (DAI, PipeWire, Deskflow, Launchers) | **The ONLY system modified.** Central command center. |
| 2 | **Ubuntu Computer** | Secondary separate machine | Standalone development | **Untouched.** Zero changes, zero commands, zero packages applied. |
| 3 | **Samsung S22 Ultra** | Serial `RFCT428ZRSZ`, Model `SM-S908U` | **Sole Universal Microphone & Phone Screen** | Dedicated mic (`android-4f3b250`) and scrcpy phone mirror. |
| 4 | **LG G8** | Serial `LMG820UM5abe4c22` | **Retired Audio Bridge** | Replaced by S22 Ultra. Services stopped and disabled cleanly. |
| 5 | **Sky Tablet** | Separate Android tablet | Standalone device | **Untouched.** Excluded from all audits and bridges. |

---

## 2. Complete Audio Architecture (Zero Echo, Zero Playback)

The Samsung Galaxy S22 Ultra is the **single source of truth for microphone input** on `dhakidd`.

```
                  [SAMSUNG GALAXY S22 ULTRA]
                      (SM-S908U / RFCT428ZRSZ)
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
       [USB Cable]        [Local Wi-Fi]      [Tailscale]
            └──────────────────┬──────────────────┘
                               │ (ADB Transport Tunnel)
                               ▼
            [audiosource-s22.service (Zero Echo)]
            • Streams microphone via fr.dzx.audiosource
            • No audio playback engine (100% silent on PC speakers)
                               │
                               ▼
            [PipeWire / PulseAudio Source: android-4f3b250]
            • Native capture module: module-pipe-source
            • Sample rate: 44.1kHz / 48kHz, 16-bit Mono
            • Kept as system default source by s22-mic-router.service
                               │
            ┌──────────────────┴──────────────────┐
            ▼                                     ▼
   [🎙️ Live Voice Typing]                  [🗣️ DAI Gemini Live]
 • Real-time streaming STT             • Continuous voice assistant
 • Types live word-by-word             • Sultry Ava Neural voice engine
 • Accommodates slow speech & pauses   • Wakes on "Hey Day" / "Yo Day"
 • Hotkey: F9 / Desktop Icon           • Steps aside when you type
```

---

## 3. The 3 Core Tools & How They Work Together

### A. 🎙️ Live Voice Typing (`bin/s22-live-speech.py` -> `~/.local/bin/dhakidd-dictate`)
- **Trigger**: Single tap of **`F9`** key OR double-click **`🎙️ Live Voice Typing`** on your Desktop.
- **Behavior**:
  - Words appear on screen in real time as you speak them.
  - Accommodates natural pauses, slow speech, and thinking time.
  - **Never cuts you off mid-sentence.**
  - Includes breath/filler filter (strips out throat clears, sighs, and "huh").
- **To Finish**: Tap **`F9`** once more when your full thought is complete.

### B. 🗣️ DAI Gemini Live Assistant (`bin/dai gemini`)
- **Trigger**: Click **`D-A-I Gemini Live`** on Desktop OR run `./bin/dai gemini`.
- **Persona**: King Justin's loyal, sultry Chief of Staff (Voice: `en-US-AvaNeural`).
- **Wake Summons**: *"Hey Day"*, *"Yo Day"*, *"Come here Day"*, *"Day come here"*.
- **Intelligence**: Auto-routes intent to web search, terminal tasks, status checks, or smooth conversation.
- **Conversational Window**: Stays in continuous 2-way dialogue for 15 seconds after speaking; automatically returns to standby when you're done.

### C. 📱 S22 Screen & Phone Control (`~/.local/bin/scrcpy`)
- **Trigger**: Click **`dhakidd-s22.desktop`** on Desktop OR run `scrcpy`.
- **Hardening**: Pinned strictly to `scrcpy -s RFCT428ZRSZ --no-audio`.
- **Isolation**: Displays the phone screen with low latency. Audio forwarding is disabled on the screen window to prevent any interference with the dedicated background microphone bridge.

---

## 4. Clickable Desktop Interface (Phase 6)

Located in `~/Desktop/` and `~/Desktop/00_COMMAND_CENTER/`:

| Launcher Name | Executable Target | What It Does |
|---|---|---|
| **🎙️ Live Voice Typing** | `~/.local/bin/dhakidd-dictate` | Real-time word-by-word streaming typing into active window |
| **D-A-I Gemini Live** | `~/dai-assistant/bin/dai gemini` | Starts Ava voice assistant in terminal / background |
| **D-A-I Command Center** | `~/dai-assistant/bin/dai-hq-launch` | Opens DAI Headquarters tmux workspace & dashboard |
| **D-A-I Repair Center** | `~/dai-assistant/bin/dai-repair` | One-click diagnostic, healing, and restart utility |
| **dhakidd-s22** | `~/.local/bin/scrcpy` | Opens Samsung S22 Ultra screen mirror on PC |

---

## 5. System Recovery & Self-Healing (Phase 7)

- **Single Command Recovery**:
  ```bash
  cd ~/dai-assistant && ./bin/dai-repair
  ```
- **Automated Service Supervision**:
  - `audiosource-s22.service`: Runs `workspace/audiosource/audiosource -s RFCT428ZRSZ run -r`. Automatically reconnects if USB is unplugged/replugged or switched to Wi-Fi.
  - `s22-mic-router.service`: Continuously verifies `android-4f3b250` is unmuted and set as the active default source for all desktop apps.
  - `dai-spine.service`: Supervises the local router, worker, and voice-bridge.

---

## 6. Safe Archive Record (`~/archive/2026-cleanup/`)

All obsolete experiments, timestamped backups, and broken legacy symlinks were consolidated safely into `~/archive/2026-cleanup/`:
- `bridge.bak-20260908-102502`
- `bridge.pre-finish.20260918-142509`
- `s22-bridge-controller.pre-finish.20260918-142509`
- `restore-g8-mic` (0-byte dead file)
- `ai.backup.20260829-141013`
- `dai-assistant.desktop` (old dead path pointing to deleted directory)
- `dhakidd-dai.desktop` (old dead path pointing to deleted directory)
- Broken self-referencing `opencode` symlink removed.
