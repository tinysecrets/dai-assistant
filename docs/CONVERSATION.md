# Talking to DAI, and getting to the live GUI

The two things people actually want from this spine: **converse** with the
assistant, and let it **drive the GUI**. This is the exact path to both —
every command below was run and verified, and the error strings are quoted
from real output, not from intent.

The one dependency everything shares: the router must be *ready*.

```bash
./bin/dai plan          # what the router would try right now
./bin/dai status        # are the services up?
```

Until `ready_for_chat` is `true` (a provider key, or local Ollama answering on
`127.0.0.1:11434`), chat refuses with:

```json
{"error": "no_providers_ready",
 "detail": "no providers ready — add a key to .env (see KEYS.md) or start local Ollama", ...}
```

Everything else — voice, dry-run GUI, approvals — works without it.

---

## Path 1 — converse

### 1a. One-shot (no assistant needed)

```bash
# put one key in .env first (OpenRouter recommended — see KEYS.md), then:
./bin/dai down && ./bin/dai up          # pick up the key
./bin/dai plan                          # must list candidates now
./bin/dai chat "what are you?"
```

`dai chat` prints the reply, then a routing note like
`[openrouter/... · 1 attempt(s)]`. Any OpenAI-compatible client can use the
same rotator directly — base URL `http://127.0.0.1:11435/v1`, model `dai/auto`:

```bash
curl -s http://127.0.0.1:11435/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model": "dai/auto", "messages": [{"role": "user", "content": "hi"}]}'
```

### 1b. The personal assistant (Vellum) — the real conversation

Vellum is a separate project you link into `vendor/` (this repo ships only
the spine + skills). Exact sequence:

```bash
mkdir -p vendor && ln -s <path-to-vellum-assistant> vendor/vellum-assistant
cd vendor/vellum-assistant && ./setup.sh          # installs the vellum CLI (~/.bun/bin/vellum)
cd -
./bin/dai hatch            # refuses unless ready_for_chat (use --force to override)
export PATH="$HOME/.bun/bin:$PATH"
vellum use debian-ai       # the assistant name dai hatch printed
./bin/dai skills           # installs the spine skills into its workspace:
                           #   agent-s-delegate, dai-voice, english-to-code
vellum wake                # and now: just talk to it
```

After that, "converse" means whatever Vellum's conversation surface is
(`vellum wake`, phone pairing via `vellum pair`). DAI is what it thinks with
(`dai/auto` through the rotator) and what it speaks with (the voice-bridge,
via the `dai-voice` skill — see Path 3).

### 1c. What conversing does NOT require

- A paid plan. The pool is free-tier models only; paid needs an explicit
  `openrouter_paid` approval token.
- The GUI stack. Chat, dry-run and voice all run without Xvfb or gui-agents.

---

## Path 2 — get to the live GUI

Status as of this box: spine up, **dry-run GUI delegation works** (Stage 3),
`gui-agents` is installed (`~/.local/agent-s-venv/bin/agent_s`), voice works.
A dry run already records the exact command it *would* run, with secrets
redacted:

```bash
python3 skills/agent-s-delegate/scripts/delegate_task.py \
  --instruction "Open Chromium and go to example.com"
# -> status: dry_run_complete, result.would_run: [...], result.live_blockers: [...]
```

`live_blockers` is the remaining shopping list, and it is authoritative. To
empty it:

### Step 1 — a vision-capable cloud key

Grounding (the model that *sees* the screen) is `dai/vision-auto`, which the
router resolves **only from cloud vision pools** (`config/rotation-pool.json`
→ `openrouter_free_vision`; `lib/dai/routing.py` — local Ollama is not in the
vision pool). So even if chat runs on local Ollama, a live GUI task needs a
cloud key (OpenRouter is the one the pool is built around):

```bash
nano .env        # OPENROUTER_API_KEY=sk-or-v1-...   (KEYS.md for the rest)
./bin/dai down && ./bin/dai up
./bin/dai plan dai/vision-auto     # must list vision candidates
```

### Step 2 — the display (workstation)

```bash
sudo apt install xvfb x11-utils tesseract-ocr
# and a browser for the agent to drive, if you don't have one:
sudo apt install chromium
```

### Step 3 — restart and verify

```bash
./bin/dai down && ./bin/dai up      # start-spine now starts Xvfb on :99 too
python3 skills/agent-s-delegate/scripts/worker_health.py
# want: live_capable: true, display_ready: true
```

### Step 4 — approve the exact instruction, then run it live

The live gate is **`--live` plus a single-use token bound to the exact
instruction** (24-char token, 1 h TTL, spent atomically on admission —
`policy/sovereign.json` keeps `require_approval_token: true`). Keep
`dry_run_default: true` — it is the default for tasks that don't ask
otherwise; the token is the gate.

```bash
INSTRUCTION="Open Chromium and go to example.com"
TOKEN=$(./bin/issue-approval.sh agent_s_gui_task "$INSTRUCTION")
python3 skills/agent-s-delegate/scripts/delegate_task.py \
  --instruction "$INSTRUCTION" --live --approval-token "$TOKEN"
```

What you see at each gate if something is still missing (all verified):

| Situation | Response |
| --- | --- |
| `--live` without a token | `usage_error`: "--live needs an approval token…" |
| live, token valid, display down | `status: blocked`, `error: display_unavailable`, "display :99 is down and Xvfb is not installed (apt install xvfb)" |
| live, all clear | task admitted (`202`), then runs on the dedicated `:99` display — **never your desktop** (`bind_owner_live_desktop: false`) |

Or, once Vellum is linked (Path 1b), just ask in conversation —
*"open example.com in Chromium"* — and it runs these same steps itself through
the `agent-s-delegate` skill, asking you first because `live_gui` is in the
policy's `ask_before` list.

---

## Path 3 — say it out loud (works today)

```bash
./bin/dai say "Kettle's boiled."            # free-form speech, saves state/speech/*.mp3
./bin/dai heartbeat --speak                 # spoken status line from live /health
nohup ./bin/dai heartbeat --speak --every 900 >> logs/heartbeat.log 2>&1 &   # scheduled blips
```

Voice is one-way until the faster-whisper model can download (HuggingFace);
TTS is fully local. The `dai-voice` skill teaches Vellum when to speak and
how to run the "I'm active" reminders.

---

## What this sandbox can and cannot prove

| Item | Here | On your workstation |
| --- | --- | --- |
| Spine, smoke, 478 tests | ✅ verified | ✅ |
| Voice (TTS) + heartbeat blips | ✅ working (CC0 `en_US-joe-medium`) | ✅ |
| Chat | ❌ no key / HF blocked here | ✅ once a key is in `.env` |
| Dry-run GUI | ✅ verified | ✅ |
| gui-agents install | ✅ verified (`python3 -m venv ~/.local/agent-s-venv && ~/.local/agent-s-venv/bin/pip install gui-agents`) | ✅ |
| Live GUI | ❌ Xvfb (apt) + vision key missing | ✅ Steps 1–4 above |
| Vellum conversation | ❌ project not linked here | ✅ Path 1b |
