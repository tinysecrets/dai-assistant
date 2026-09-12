---
name: agent-s-delegate
description: "Delegate a bounded GUI/browser/desktop task to the local Agent S worker service. Use when Vellum's native tools/browser skill cannot complete visual app interaction and a dedicated agent desktop is required."
compatibility: "Designed for Vellum personal assistants on the Debian AI integration spine"
metadata:
  emoji: "🖥️"
  vellum:
    category: "automation"
    display-name: "Agent S Delegate"
    activation-hints:
      - "Load when a task needs GUI mouse/keyboard control beyond assistant browser CLI"
      - "Prefer native Vellum browser/tools first; only delegate visual desktop work"
---

# Agent S Delegate

Vellum remains the personal assistant (identity, memory, planning, approvals).
Agent S is only the **bounded GUI worker**.

## Rules

1. Prefer native Vellum skills/tools (`assistant browser`, APIs, bash) first.
2. Delegate only a **single bounded instruction** — not open-ended autonomy.
3. Default to **dry-run** unless the owner supplies an approval token for live GUI work.
4. Never target the owner's everyday live desktop. Worker uses dedicated `DISPLAY` from policy.
5. Never paste API keys into chat. Never send private files to cloud models for GUI tasks.
6. After completion, summarize status, actions, artifacts, and ask before any follow-up that spends money or changes protected state.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/delegate_task.py` | Submit a task to `agent-s-worker` and poll for result |
| `scripts/worker_health.py` | Check worker + policy health |

## Typical flow

```bash
# 1. Health
python3 scripts/worker_health.py

# 2. Dry-run (safe default)
python3 scripts/delegate_task.py --instruction "Open Chromium and go to example.com"

# 3. Live GUI only with owner approval token bound to this exact instruction
python3 scripts/delegate_task.py \
  --instruction "Open Chromium and go to example.com" \
  --live \
  --approval-token "<owner-issued-token>"
```

## Result handling

Interpret JSON fields:

- `status`: `dry_run_complete` | `queued` | `running` | `failed` | `blocked` | …
- `result.summary`: human-readable outcome
- `result.actions` / `result.artifacts`: what happened (empty in dry-run)
- On failure, report `error` and propose the next owner approval (package install, grounding model, enable worker, etc.)

## Out of scope

- Phone pairing (use Vellum native pair/connect)
- Replacing Vellum memory/identity
- Controlling the owner's main desktop session
- Paid OpenRouter / cloud grounding without explicit owner approval
