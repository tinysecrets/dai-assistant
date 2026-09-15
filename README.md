# Debian AI Assistant

A local-first inference spine for a Debian workstation: an OpenAI-compatible
**model router** that rotates across free provider tiers, and a **GUI-agent
worker** that runs bounded desktop tasks behind an approval gate.

Three roles, kept deliberately separate:

| Role | What it is | Where |
| --- | --- | --- |
| Personal assistant | **Vellum** — identity, memory, planning, phone pairing | `vendor/vellum-assistant` (linked by you) |
| Inference | **model-router** — cloud-first free-tier rotation, local last | `services/model-router` on `:11435` |
| GUI hands | **agent-s-worker** — bounded Agent S tasks, dry-run by default | `services/agent-s-worker` on `:8765` |
| Voice | **voice-bridge** — free/offline STT + TTS, relayed through the router | `services/voice-bridge` on `:8766` (optional) |

The router is the only thing that talks to a provider. The worker talks to the
router. Vellum talks to both. Nothing here overwrites your other assistant
projects — `policy/sovereign.json` lists the paths it must never touch.

**No dependencies and no build step.** Stdlib Python ≥ 3.9 plus bash. A fresh
clone starts, answers `/health`, and runs dry-run GUI tasks with no keys at all.
The optional voice-bridge needs its own venv (`~/.local/voice-venv`, with
`faster-whisper` and `piper`); without it the spine still runs and only
`/v1/audio/*` is unavailable.

---

## Quick start

```bash
git clone https://github.com/tinysecrets/dai-assistant.git
cd dai-assistant

./bin/doctor.sh          # what is present, what is missing — never fails here
./bin/import-keys.sh     # or: cp .env.example .env && nano .env
./bin/start-spine.sh     # router (:11435) + worker (:8765) + voice bridge (:8766)
./bin/smoke.sh           # end-to-end self-test
./bin/dai chat "Say hi"  # once ready_for_chat is true
```

Without a key, chat answers `503` with a message telling you exactly what to
add. That is the intended behaviour, not a broken install. See `KEYS.md`.

`./bin/start-spine.sh` creates `.env` from `.env.example` if it is missing.

---

## What you get

### Inference with rotation

```bash
curl -s localhost:11435/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"dai/auto","messages":[{"role":"user","content":"Say hi"}]}'
```

OpenAI-compatible, so anything that speaks the OpenAI API can point at
`http://127.0.0.1:11435/v1`. Two virtual model ids:

* `dai/auto` — rotate across every enabled provider, text-optimised
* `dai/vision-auto` — rotate across vision-capable free models (GUI grounding)

Any provider model can also be named explicitly, e.g.
`groq/llama-3.3-70b-versatile` or `ollama_local/hermes3:8b`.

On rotation:

* A `429` cools that **model** and moves to the next candidate.
* An auth or quota failure cools the **whole provider** — the same key would
  fail on every model there — and takes effect immediately, even mid-request.
* Streaming rotates **before the first byte**; it never silently restarts a
  half-delivered answer.
* Every response carries `dai_routed` telling you which provider and model
  actually answered, and how many attempts it took.

Paid OpenRouter models are refused unless you opt in via policy or supply a
one-shot approval token.

### GUI automation you have to approve

```bash
# Dry run: validates and records the exact command, touches no input device
python3 skills/agent-s-delegate/scripts/delegate_task.py \
  --instruction "Open Chromium and go to example.com"

# Live: needs a token bound to that exact instruction
TOKEN=$(./bin/issue-approval.sh agent_s_gui_task "Open Chromium and go to example.com")
export DAI_APPROVAL_TOKEN="$TOKEN"
python3 skills/agent-s-delegate/scripts/delegate_task.py \
  --instruction "Open Chromium and go to example.com" --live
```

A dry run returns `would_run` — the full `agent_s` argv with secrets redacted —
plus `live_blockers`, the list of what is missing before a live run could work.
Show that to the owner before asking for approval.

Live runs need a policy change **and** a single-use token scoped to the exact
instruction. Tokens expire, are spent atomically at admission, and are spent
even if the task then fails. The agent runs on a dedicated Xvfb display, never
on the desktop you are sitting at.

### Vellum

```bash
./bin/hatch-vellum.sh           # refuses without a ready inference path
./bin/install-vellum-skill.sh   # validates SKILL.md, then installs
```

Two skills ship in `skills/`: `agent-s-delegate` (delegate a bounded GUI task)
and `english-to-code` (turn a plain-language request into a runnable command).

---

## The `dai` command

One entry point over the individual scripts, which stay fully usable directly.

```
dai doctor      check files, keys, services, GUI deps
dai up / down   start / stop the spine
dai status      are they running?
dai logs [n]    tail service logs
dai smoke       end-to-end self-test
dai test        unit + integration suite
dai keys        import keys into .env
dai models      what the router can serve right now
dai plan [id]   what it would try, without calling anything
dai config      resolved configuration, secrets removed
dai chat "..."  one-shot chat through the rotator
dai approve     issue an approval token
dai tokens      list / revoke / prune tokens
dai skills      install skills into the Vellum workspace
dai hatch       hatch the Vellum assistant
dai version     component versions
```

Or `make help` for the same operations as make targets.

---

## Layout

| Path | Purpose |
| --- | --- |
| `bin/` | Operational scripts, plus `dai` and the shared `lib.sh` |
| `lib/dai/` | Shared stdlib library: env, redaction, JSON, HTTP, approvals, routing, stats |
| `services/model-router/` | OpenAI-compatible router with free-tier rotation |
| `services/agent-s-worker/` | Bounded GUI-agent worker |
| `skills/` | Vellum skills, installable into an assistant workspace |
| `config/rotation-pool.json` | Which models each provider rotates through |
| `config/free-models.json` | Reference catalog of free models |
| `policy/sovereign.json` | Intent and safety: what needs approval, what is forbidden |
| `policy/approvals.json` | Live tokens — created on first use, `0600`, git-ignored |
| `tests/` | 446 tests: unit, HTTP integration, scripts, drift detection |
| `docs/` | API, configuration, operations, architecture |
| `.env` | Your keys — `0600`, git-ignored |
| `logs/`, `state/` | Runtime artifacts, git-ignored, fully regenerable |
| `vendor/` | Upstream checkouts you link by hand |

---

## Documentation

| | |
| --- | --- |
| `docs/API.md` | Every endpoint, payload, and error code — both services advertise it from `/health` |
| `docs/CONFIG.md` | Every setting, its default, and which layer wins |
| `docs/OPERATIONS.md` | Runbook: start, diagnose, approve, recover |
| `docs/ARCHITECTURE.md` | Why the spine is shaped this way |
| `KEYS.md` | Getting provider keys |
| `SECURITY.md` | Threat model, design rules, known limitations |
| `CONTRIBUTING.md` | Ground rules and how the tests are organised |
| `CHANGELOG.md` | What changed, including everything fixed in this overhaul |
| `docs/INSPECTION.md`, `docs/SCRAP_MAP.md` | Field notes on Agent S and the local landscape |

---

## Configuration

Four layers, highest wins:

```
request body  >  environment  >  .env  >  policy/sovereign.json  >  built-in defaults
```

Two rules override that, and both exist so an environment variable can never
make the system more dangerous:

1. **Safety keys resolve from the policy file only** — `enabled`,
   `dry_run_default`, `bind_owner_live_desktop`, `require_approval_token`,
   `max_steps_hard_cap`. The worker publishes the list at
   `/v1/settings` → `safety_keys_policy_only`.
2. **A real environment variable beats `.env`**, so you can override one key for
   one command without editing a file.

`.env` is *parsed*, never sourced: sourcing would export every secret into
every child process. Secrets are redacted from every log line, error body and
task record.

Inspect what actually resolved — never guess:

```bash
./bin/dai config
./bin/dai plan
curl -s localhost:11435/v1/status/keys      # booleans only, never a value
curl -s localhost:8765/v1/settings
```

---

## Development

```bash
make help        # every target
make lint        # compile + bash -n + service --check
make test        # 446 tests
make all         # lint + test + smoke — the gate CI runs
```

The suite needs no keys, no network and no display. HTTP tests start real
servers on ephemeral ports against a stub provider; live GUI execution is
tested with a fake `agent_s` and a fake `xset`.

`tests/test_repo_consistency.py` is the one worth knowing about. It fails when
`.env.example` documents a variable nothing reads, when a doc references a file
that does not exist, when `docs/API.md` documents an error code no service can
emit, when the rotation pool contains a non-`:free` model, when a skill script
calls an API that does not exist, or when the shipped policy stops being safe.
It exists because this repo drifted badly once.

CI runs the same gate on Python 3.9, 3.11 and 3.12, plus shellcheck, ruff and
repo hygiene.

---

## Requirements

| | |
| --- | --- |
| Required | `python3` ≥ 3.9, `bash`, `curl` |
| For live GUI tasks | `gui-agents` in its own venv, `xvfb`, `x11-utils`, `tesseract-ocr` |
| For Vellum | the `vellum` CLI, linked into `vendor/vellum-assistant` |
| Optional, dev only | `ruff`, `shellcheck` (`make install-dev`) |

`./bin/doctor.sh` reports which of these are present and how to install what is
missing. Optional dependencies are never a hard failure — dry-run works without
any of them.

---

## Security

Both services bind `127.0.0.1`. No CORS headers, and none will be added: a
browser page must not be able to drive GUI automation or spend inference budget.
Secrets live only in `.env` and `policy/approvals.json`, both mode `0600` and
git-ignored, and are redacted from all output.

Read `SECURITY.md` for the threat model, the design rules that are load-bearing,
and the known limitations — stated plainly, because a limitation nobody wrote
down becomes an incident.

To report a vulnerability, please do so privately rather than in a public issue.

---

## License

MIT — see `LICENSE`.
