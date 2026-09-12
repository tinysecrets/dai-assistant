# Operations

The runbook: how to start the spine, read what it is telling you, approve a
privileged action, and recover when something goes wrong.

Everything here is a command you can run. Nothing requires editing code.

---

## Everyday commands

`bin/dai` is a thin dispatcher over the individual scripts — use whichever you
prefer. The scripts stay fully usable on their own.

| Command | What it does |
| --- | --- |
| `./bin/dai doctor` | Check files, config, keys, services, optional GUI deps |
| `./bin/dai up` | Start router + worker (and Xvfb when installed) |
| `./bin/dai down` | Stop them |
| `./bin/dai status` | Are they up? |
| `./bin/dai logs [n]` | Tail service logs |
| `./bin/dai smoke` | End-to-end self-test |
| `./bin/dai test` | Unit + integration suite |
| `./bin/dai keys` | Import keys from other local installs into `.env` |
| `./bin/dai models` | What the router can serve right now |
| `./bin/dai plan [id]` | What it would try, without calling anything |
| `./bin/dai config` | Resolved configuration, secrets removed |
| `./bin/dai chat "..."` | One-shot chat through the rotator |
| `./bin/dai approve <action> <scope>` | Issue an approval token |
| `./bin/dai tokens` | List tokens (masked) |
| `./bin/dai skills` | Install skills into the Vellum workspace |
| `./bin/dai hatch` | Hatch the Vellum assistant |
| `./bin/dai version` | Component versions |

---

## Fresh clone to first answer

```bash
git clone <repo> && cd dai-assistant

./bin/doctor.sh          # what is present, what is missing. Never fails here.
./bin/import-keys.sh     # or: cp .env.example .env && nano .env
./bin/start-spine.sh     # starts router (:11435) + worker (:8765)
./bin/smoke.sh           # verify end to end
./bin/dai chat "Say hi"  # once ready_for_chat is true
```

No dependency install, no build step. The Python is stdlib-only and runs on a
bare Debian `python3` (3.9+).

**A fresh clone is not broken.** With no keys it reports
`ready_for_chat: false` and chat answers `503 no_providers_ready` with a
message telling you what to add. Dry-run GUI tasks work with no keys at all.

`./bin/start-spine.sh` creates `.env` from `.env.example` if it is missing, so
the first run never fails on an absent file.

---

## Reading readiness

Two different questions, two different fields. Conflating them is the most
common source of confusion.

### Router: can anything answer a prompt?

```bash
curl -s localhost:11435/health | python3 -m json.tool
```

| Field | Meaning |
| --- | --- |
| `ok` | The service is up |
| `ready_for_chat` | At least one candidate is reachable |
| `candidates_available` | How many models the planner would try now |
| `providers_configured` | Which providers have a key |
| `cooldowns_active` | How many candidates are currently sitting out |
| `config_errors` | Config files that failed to parse |

`GET /ready` returns the same payload but answers **503** when
`ready_for_chat` is false — use that one in anything that should fail loudly.

### Worker: can it do GUI work?

```bash
curl -s localhost:8765/health | python3 -m json.tool
```

| Field | Meaning |
| --- | --- |
| `ready` | It can accept *something* (dry-run needs nothing) |
| `live_capable` | A **live** run could succeed right now |
| `dry_run_default` | What a task does when it does not say |
| `agent_s_installed` | `gui-agents` binary found |
| `display_ready` | The agent display is answering |
| `require_approval_token` | Live runs need a token |
| `queue_depth` / `queue_max` | Backlog and its bound |

`ready: true` with `live_capable: false` is the normal fresh-clone state: dry
runs work, live runs are refused with a specific reason.

---

## Diagnosing inference

### What would it try?

```bash
./bin/dai plan
./bin/dai plan dai/vision-auto
curl -s 'localhost:11435/v1/status/plan?model=groq/llama-3.3-70b-versatile'
```

This calls nothing. It shows the candidate order, each one's provider, base
URL, origin (`pool`, `explicit`, `vision`) and cooldown key. When rotation
behaves surprisingly, start here.

### Which keys are present?

```bash
./bin/dai config            # resolved config for both services
curl -s localhost:11435/v1/status/keys | python3 -m json.tool
```

`keys_needed` lists exactly what to add to `.env` to widen the rotation. Values
are never returned — booleans only.

### Why is a model failing?

```bash
curl -s localhost:11435/v1/status/cooldowns | python3 -m json.tool
curl -s localhost:11435/v1/status/stats | python3 -m json.tool
```

Cooldown values are absolute timestamps: the moment the candidate becomes
eligible again. Keys are `provider:model`, or just `provider` when a whole
provider is cooled after an auth or quota failure.

A failed request also explains itself:

```json
{
  "error": "all_candidates_failed",
  "attempts": [
    {"candidate": "openrouter:google/gemma-4-31b-it:free", "status": 429,
     "kind": "rate_limit", "error": "Rate limit exceeded", "cooldown_applied": 90.0}
  ]
}
```

Read `attempts`. `kind` tells you whether to wait (`rate_limit`), fix a key
(`auth`, `quota`), or look upstream (`server`, `network`).

### After fixing a key

Cooldowns persist across restarts, so clear them:

```bash
curl -X POST localhost:11435/v1/status/reset
```

### Logs

```bash
./bin/dai logs            # last 40 lines of each
./bin/dai logs 200
tail -f logs/model-router.log
```

Secrets are redacted in every log line and error body — the router registers
each secret-looking environment value at startup, so a key that matches no
known pattern is still scrubbed. If you ever see a real key in a log, treat it
as a bug and rotate the key.

---

## Diagnosing GUI tasks

### Always start with a dry run

A dry run validates the request and records the exact command a live run would
execute, without touching an input device:

```bash
curl -s -X POST localhost:8765/v1/tasks -H 'Content-Type: application/json' \
  -d '{"instruction":"Open Chromium and go to example.com","dry_run":true}'
```

Then read the result:

```bash
curl -s localhost:8765/v1/tasks/<id> | python3 -m json.tool
```

Two fields matter:

* **`would_run`** — the full `agent_s` argv, with secrets shown as
  `[REDACTED]`. This is the audit trail. Check the model, the grounding
  dimensions and the display before going live.
* **`live_blockers`** — what is missing before a live run could work.

Or use the skill script, which polls to a terminal status for you:

```bash
python3 skills/agent-s-delegate/scripts/delegate_task.py \
  --instruction "Open Chromium and go to example.com"
```

### Making a live run possible

`live_capable` needs all of:

1. `gui-agents` installed into the venv
2. the agent display up
3. the router reachable and `ready_for_chat`
4. policy allowing it (`dry_run_default: false`, or an explicit
   `"dry_run": false` in the request)
5. a valid approval token for that exact instruction

```bash
# 1. install gui-agents in its own venv
python3 -m venv ~/.local/agent-s-venv
~/.local/agent-s-venv/bin/pip install gui-agents

# 2. the display: start-spine.sh runs Xvfb when it is installed
sudo apt install xvfb x11-utils
./bin/dai down && ./bin/dai up

# 3. verify
./bin/doctor.sh
curl -s localhost:8765/health | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print("live_capable:", d["live_capable"])'
```

`./bin/doctor.sh` reports each of these with the fix next to it.

**The agent display is never your live desktop.** `start-spine.sh` runs a
dedicated Xvfb on `:99`. `bind_owner_live_desktop` is `false` in the shipped
policy and is a safety key, so no environment variable can flip it.

---

## Approval tokens

A token authorises **one** privileged action, for **one** exact scope, for a
**limited time**. This is the mechanism that keeps "the agent can drive a GUI"
and "the agent can spend money" from becoming ambient capabilities.

### Issue

```bash
TOKEN=$(./bin/issue-approval.sh agent_s_gui_task "Open Chromium and go to example.com")
./bin/issue-approval.sh openrouter_paid "anthropic/claude-sonnet-4.5"
./bin/issue-approval.sh agent_s_gui_task "..." --ttl 300 --max-uses 1 --note "demo"
```

stdout is the token and nothing else, so `$(...)` capture works. Everything
explanatory goes to stderr.

### Use

```bash
# live GUI task: token in the body
curl -X POST localhost:8765/v1/tasks -H 'Content-Type: application/json' \
  -d "{\"instruction\":\"Open Chromium and go to example.com\",
       \"dry_run\":false,\"approval_token\":\"$TOKEN\"}"

# paid model: body field or header
curl -X POST localhost:11435/v1/chat/completions -H 'Content-Type: application/json' \
  -H "X-DAI-Approval-Token: $TOKEN" \
  -d '{"model":"openrouter/anthropic/claude-sonnet-4.5",
       "messages":[{"role":"user","content":"hi"}]}'
```

### Manage

```bash
./bin/issue-approval.sh --list            # masked: E6W0…poHv
./bin/issue-approval.sh --revoke <token>
./bin/issue-approval.sh --prune           # drop expired/exhausted
```

### The rules, and why

| Rule | Why |
| --- | --- |
| Empty scope is rejected | A token that authorises anything must say `"*"` out loud |
| Scope match is exact | A token for one instruction will not authorise a reworded one |
| Spent at admission, atomically | Two concurrent requests cannot both use a single-use token |
| Spent even if the task then fails | A token authorises one *attempt*, not one success |
| TTL default 3600s | A forgotten token stops working on its own |
| `policy/approvals.json` is mode `0600` and git-ignored | Tokens are credentials |

That fourth rule surprises people. If a live task fails after the token was
accepted, issue a new token — do not look for a refund.

---

## Recovery

| Symptom | Cause | Fix |
| --- | --- | --- |
| `start-spine.sh` says a port is in use | A foreign process holds it | `ss -ltnp \| grep 11435`. The script refuses to kill something it did not start |
| Service will not stop | Pid file lost | `./bin/dai down` falls back to a path-anchored match; then check `state/*.pid` |
| `stop-spine.sh` says "nothing was running" but a port is busy | Not ours | Find it with `ss`; this repo will not kill it |
| Task stuck in `running` | Worker died mid-task | On restart the record becomes `interrupted`; it is never left claiming to run |
| Task `failed` with `model_router_not_ready` | Router down or not ready | `./bin/dai status`, then `curl -s localhost:11435/health` |
| Task `failed` with `display_unavailable` | Xvfb not running | `./bin/dai down && ./bin/dai up`; `./bin/dai logs` shows `xvfb.log` |
| Task `timeout` | Exceeded `AGENT_S_TIMEOUT` | The process was killed. Raise the timeout or narrow the instruction |
| `429 queue_full` | Backlog at `DAI_AGENT_S_MAX_QUEUE` | Wait; tasks are serialised by design (one GUI agent per display) |
| Same model fails every time | Cooled | `curl -s localhost:11435/v1/status/cooldowns` |
| Whole provider failing | Auth/quota → provider cooldown | Fix the key, then `curl -X POST localhost:11435/v1/status/reset` |
| `501 not_implemented` | You asked for embeddings/audio/images | Deliberate; the `detail` says why |
| Policy file corrupt | Bad edit | The worker falls back to safe defaults and reports `config_errors`; it does not crash |
| `.env` has a value the service ignores | A real env var overrides it, or it names a safety key | `env \| grep DAI_`; `docs/CONFIG.md` |

### Reset everything

```bash
./bin/dai down
rm -rf state/ logs/                 # pid files, cooldowns, task records
curl -X POST localhost:11435/v1/status/reset   # if the router is up
./bin/dai up
```

`state/` and `logs/` are git-ignored and fully regenerable. Removing them costs
you task history and active cooldowns, nothing else. Your keys (`.env`) and
tokens (`policy/approvals.json`) are untouched.

### Restarting cleanly

`start-spine.sh` is idempotent: it stops anything it already owns before
starting, so re-running it is a restart. Pid files are verified against the
recorded process's command line before use, so a recycled pid can never cause
the script to kill something unrelated.

---

## Verifying a change

Before committing anything that touches the spine:

```bash
./bin/dai test       # unit, HTTP integration, shell, and repo consistency
./bin/dai smoke      # the suite plus live checks against a running spine
./bin/doctor.sh      # config, keys, services, optional deps
```

`smoke.sh` only attempts real inference when the router reports
`ready_for_chat`; otherwise it says so and skips, so it is safe to run without
keys. Its GUI task is always a dry run.

The consistency tests are what keep the repo honest: they fail if `.env.example`
documents a variable nothing reads, if a doc references a file that does not
exist, if `docs/API.md` documents an error code no service can emit, if the
rotation pool contains a non-`:free` model, or if the shipped policy stops
being safe.

CI runs the same three commands. See `.github/workflows/ci.yml`.

---

## Vellum

```bash
./bin/doctor.sh            # confirms vendor/vellum-assistant and the CLI
./bin/hatch-vellum.sh      # refuses unless an inference path is ready
./bin/install-vellum-skill.sh
```

`hatch-vellum.sh` will not hatch into a broken assistant: it checks that the
router is up and `ready_for_chat`, and exits non-zero otherwise. Pass `--force`
to hatch anyway. It exports only the keys Vellum needs, only for the single
command that uses them — `.env` is parsed, never sourced.

`install-vellum-skill.sh` validates each `SKILL.md`'s frontmatter first (a skill
Vellum cannot load is worse than no skill), installs into the assistant
workspace when it can find one, and otherwise stages into `skills-ready/` and
prints the exact copy command.

Missing `vendor/` is a **warning**, not a failure — a fresh clone has no
vendored checkouts and `doctor.sh` must not look broken.

---

## Security posture

The short version; `SECURITY.md` has the reporting path.

* Both services bind `127.0.0.1`. Nothing is reachable from the network by
  default.
* No CORS headers, and none will be added: a browser page must not be able to
  drive GUI automation or spend inference budget.
* Secrets live only in `.env` (mode `0600`, git-ignored) and are redacted from
  every log line, error body and task record.
* Live GUI control needs a policy change **and** a one-shot token scoped to the
  exact instruction.
* The agent display is a dedicated Xvfb, never the owner's live desktop.
* `state/` artifacts are mode `0600`; the artifact endpoint serves only
  basenames already recorded in a task, and refuses traversal.
* The worker serialises tasks (`DAI_AGENT_S_CONCURRENCY=1`) and bounds its
  queue, so a burst cannot spawn unbounded agent processes.

Optional bearer auth on both services: set `DAI_ROUTER_TOKEN` and
`DAI_AGENT_S_TOKEN`.

---

## Related

* `docs/API.md` — endpoints, payloads, error codes
* `docs/CONFIG.md` — every setting and the precedence rules
* `docs/ARCHITECTURE.md` — why the spine is shaped this way
* `README.md` — what this is
* `KEYS.md` — obtaining provider keys
