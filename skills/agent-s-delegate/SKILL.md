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

# 2. Dry-run (safe default) — always do this first
python3 scripts/delegate_task.py --instruction "Open Chromium and go to example.com"

# 3. Live GUI only with an owner approval token bound to this exact instruction.
#    The owner issues it:  ./bin/issue-approval.sh agent_s_gui_task "<instruction>"
#    Pass it through the environment, not argv: argv is visible in `ps`.
export DAI_APPROVAL_TOKEN="<owner-issued-token>"
python3 scripts/delegate_task.py \
  --instruction "Open Chromium and go to example.com" \
  --live
```

Useful flags: `--quiet` (compact one-line result), `--max-steps N`,
`--timeout N` (default 600s), `--cancel-on-timeout`, `--worker URL`.

## Exit codes

Branch on these, not on prose. They distinguish "never became a task" from
"became a task and did not succeed".

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | `succeeded` or `dry_run_complete` | Report the outcome |
| 1 | Worker refused the request or was unreachable | Read `error`; if `connection_failed`, ask the owner to run `./bin/dai up` |
| 2 | Task ran and did not succeed (`failed`, `blocked`, `timeout`, `cancelled`, `interrupted`) | Read `result.error` and propose the specific next step |
| 3 | `--timeout` elapsed while still `queued`/`running` | Tell the owner it may finish later; give the task id |
| 4 | Usage or configuration error | Fix the invocation — do not retry as-is |

Exit 1 with `missing_approval_token`, `approval_expired` or
`approval_scope_mismatch` means the owner must issue a **new** token for this
**exact** instruction. Tokens are single-use and are spent even when the task
later fails, so never reuse one.

## Result handling

Interpret JSON fields:

- `status`: `queued` | `running` | `dry_run_complete` | `succeeded` | `failed` |
  `blocked` | `timeout` | `cancelled` | `interrupted`
- `result.summary`: human-readable outcome
- `result.would_run`: the exact `agent_s` command a live run would execute,
  secrets shown as `[REDACTED]`. Show this to the owner before asking for a
  live approval — it is the audit trail
- `result.live_blockers`: what is missing before a live run could work
  (`gui-agents` not installed, display down, router not ready). Report these
  verbatim; each one has a concrete fix
- `result.actions` / `result.artifacts`: what happened (always empty in dry-run)
- On failure, `result.error` is a stable code and `result.detail` explains it.
  Report both, then propose the next owner approval (package install, grounding
  model, enable worker, etc.)

## Out of scope

- Phone pairing (use Vellum native pair/connect)
- Replacing Vellum memory/identity
- Controlling the owner's main desktop session
- Paid OpenRouter / cloud grounding without explicit owner approval
