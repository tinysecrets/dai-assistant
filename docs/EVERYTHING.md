# DAI for EVERYTHING (except phone control)

You proved live: STT → router (OpenRouter free) → Vellum hatched → TTS → STT back `"D.A.I. is live and ready."` + Agent-S `:99` live `succeeded` on `Click inside xterm`. Full stack up. This doc makes that 1 keypress.

## One-time setup (5 min)

```bash
# Auto-start spine on boot
mkdir -p ~/.config/systemd/user
cp ~/dai-assistant/config/systemd/dai-spine.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dai-spine.service

# Aliases for everything
echo 'source ~/dai-assistant/config/dai-aliases.sh' >> ~/.bashrc
source ~/dai-assistant/config/dai-aliases.sh

# Dashboard everywhere
# Set Chromium homepage to http://127.0.0.1:8799
# Optional remote: tailscale serve --bg http://127.0.0.1:8799
# Then https://<machine>.ts.net is headquarters on your laptop

# Hotkey: Debian Settings > Keyboard > Custom Shortcuts
# Name: DAI Listen | Command: ~/dai-assistant/bin/dai-ask --listen | Binding: Ctrl+Alt+Space
```

## The 3 commands for everything

### `da` / `dai-ask` - universal ask
```bash
da "make a python http server on 8000"          # text → chat → spoken
da --listen                                      # push-to-talk: mic → STT → LLM → TTS
da --code "fix the dashboard snapshot bug"       # english-to-code mode
da --do "open vs code in this project"           # delegates to Agent-S
```
Uses existing spine: `voice-bridge /v1/audio/*` proxied via `model-router AUDIO_PATHS`, `dai/auto` rotation, Vellum gateway if hatched.

### `dd` / `dai-do` - GUI automation on isolated :99
```bash
dd --list
dd xterm-focus          # preset from state/presets/xterm-focus.txt
dd "Open Chromium and go to example.com"
dd --dry-run "Press Ctrl+T"
```
Live path: `issue-approval.sh agent_s_gui_task "<exact instruction>"` → `DAI_APPROVAL_TOKEN` → `delegate_task.py --live`. You fixed the upstream 15-step hardcode in local Agent-S checkout - that fix stays outside dai-assistant (correct).

Presets in `state/presets/` (8 included): xterm-focus, browser-example, vscode-project, screenshot-describe, github-prs, terminal-ls, close-window, new-tab. Add your own `.txt` files.

### `dl` / `dai-listen` - hands-free loop
```bash
dl              # 4s record → STT → chat → TTS
dl --loop       # keep listening until Ctrl+C
```
Thin wrapper over `dai-ask --listen`. Requires `arecord` (alsa-utils) + voice venv (`~/.local/voice-venv` with faster-whisper + piper-tts). Fails cleanly with `stt_load_failed` / `voice_download_failed` JSON when models missing - expected.

## Everyday everything workflows

**Coding**
```bash
da --code "watch ~/Downloads and move pdfs to ~/Docs"
# Vellum renders minimal working code, runs it, verifies exit code
```

**Desktop**
```bash
dd github-prs
# Agent-S opens Chromium on :99, you watch via dashboard
```

**System / memory**
```bash
da "is Ollama running? what models?"
da "what did we do yesterday on dashboard fix?"  # Vellum+Qdrant recalls if Qdrant up
```

**Status**
```bash
dh          # status + heartbeat line: "D-A-I here, and I'm active..."
dlog 20     # tail logs
de2e        # full e2e-loop: STT proxy → router 503 honest → TTS proxy → Agent-S dry-run → dashboard aggregation
dmodels     # router models right now
dtasks      # worker tasks
```

## Memory for everything

- **Qdrant**: `docker run -d -p 6333:6333 -v ~/qdrant_storage:/qdrant/storage qdrant/qdrant` → `doctor.sh` flips to OK, Vellum uses it automatically for long-term.
- **Projects**: 14 projects in `config/integrations.json` - add `MEMORY.md` in each with 5 lines about what it is. Vellum recalls when you say "fix the dashboard".
- **Heartbeat autonomy**: `nohup ~/dai-assistant/bin/dai heartbeat --speak --every 900 >> logs/heartbeat.log &` → spoken presence every 15min.

## Why no phone control?

Registry has `android_control` via ADB+scrcpy but you said EVERYTHING *beside* phone. Leave it as INFO in doctor - no need to disable, just don't use `dai-voice` for phone. Focus is Tailscale+SSH+Deskflow for remote/desktop.

## Git clean

Live run you reported was at `f51da99` - finishing commit (dashboard fix, headquarters robust, device probes). `1546a7f` adds only `e2e-loop.sh` helper. Both are clean, no rebuild, no duplicate bridge.

No middleware rebuild needed - spine already forwards AUDIO_PATHS and shares router URL.
