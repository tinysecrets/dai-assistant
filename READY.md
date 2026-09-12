# Readiness checklist

Work through this to go from a fresh clone to a working spine. Every item has
the command that proves it — do not take anyone's word for it, including this
file's.

The boxes are **unchecked on purpose**: this is a list for you to complete on
your machine, not a claim about the state of the repo.

```bash
./bin/doctor.sh    # runs every check below and tells you what is missing
```

---

## Stage 1 — the spine runs

No keys, no packages, no network needed. This is the floor, and a fresh clone
reaches it immediately.

- [ ] `python3` is 3.9 or newer — `python3 -V`
- [ ] `bash` and `curl` are present — `./bin/doctor.sh` checks both
- [ ] Both services pass configuration validation — `make check`
- [ ] The test suite passes — `make test`
- [ ] The spine starts — `./bin/start-spine.sh`
- [ ] The router answers — `curl -s localhost:11435/health`
- [ ] The worker answers — `curl -s localhost:8765/health`
- [ ] `./bin/smoke.sh` reports `failed=0`
- [ ] `./bin/stop-spine.sh` releases both ports

At this point `ready_for_chat` is `false` unless you have local Ollama. That is
correct, not broken.

## Stage 2 — inference works

Needs one key, or a local Ollama.

- [ ] `.env` exists with at least one key — `./bin/import-keys.sh`, or
      `cp .env.example .env && nano .env`
- [ ] `.env` is mode `0600` — `stat -c '%a' .env`
- [ ] At least one provider is configured —
      `curl -s localhost:11435/v1/status/keys`
- [ ] `ready_for_chat` is true — `curl -s localhost:11435/health`
- [ ] Candidates are available — `./bin/dai plan` lists at least one
- [ ] A chat round-trip succeeds — `./bin/dai chat "Say hi"`
- [ ] The response says which model answered — look for `dai_routed`
- [ ] Streaming works — `./bin/smoke.sh` covers it when `ready_for_chat` is true

Local-only alternative, no key at all:

```bash
ollama serve &
ollama pull hermes3:8b
./bin/dai down && ./bin/dai up
```

## Stage 3 — dry-run GUI delegation

Needs nothing beyond Stage 1. This is the safe way to exercise the worker.

- [ ] The worker reports `ready` — `curl -s localhost:8765/health`
- [ ] A dry-run task completes —
      `python3 skills/agent-s-delegate/scripts/delegate_task.py --instruction "Open Chromium and go to example.com"`
- [ ] The result records the exact command — `result.would_run` in the output
- [ ] Secrets in that command are redacted — you should see `[REDACTED]`, never
      a key
- [ ] `result.live_blockers` lists what a live run would need
- [ ] `worker_health.py` reports the same picture —
      `python3 skills/agent-s-delegate/scripts/worker_health.py`

## Stage 4 — live GUI automation

Optional. Every item here is a deliberate step up in what the machine can do.

- [ ] `gui-agents` installed **in its own venv** —
      `python3 -m venv ~/.local/agent-s-venv && ~/.local/agent-s-venv/bin/pip install gui-agents`
- [ ] `xvfb` and `x11-utils` installed — `sudo apt install xvfb x11-utils`
- [ ] `tesseract-ocr` installed — `sudo apt install tesseract-ocr`
- [ ] The spine was restarted so Xvfb comes up — `./bin/dai down && ./bin/dai up`
- [ ] The agent display answers — `xset -display :99 q`
- [ ] `live_capable` is true — `curl -s localhost:8765/health`
- [ ] A **vision-capable** model is reachable (see the note below)
- [ ] You understand what you are changing: set
      `agent_s.dry_run_default` to `false` in `policy/sovereign.json`, **or**
      keep it `true` and pass `"dry_run": false` per task
- [ ] You can issue a token — `./bin/issue-approval.sh agent_s_gui_task "<exact instruction>"`
- [ ] A live task runs end to end on display `:99`
- [ ] `bind_owner_live_desktop` is still `false` —
      `curl -s localhost:8765/v1/settings`

### The grounding problem, stated plainly

Live GUI tasks need a **vision** model for grounding — deciding where on screen
to click. Text-only models cannot do this, and this is the step that most often
fails:

* OpenRouter's free vision tier rate-limits aggressively, so grounding calls
  `429` and the task times out.
* Ollama Cloud's vision models may need credits.
* Most small local models are text-only.

Practical options, in rough order of reliability:

1. A local VLM through Ollama (for example a vision-capable `llava` or
   `qwen2.5vl` build), pointed at with `AGENT_S_GROUND_MODEL`. No rate limits,
   no credits, and it stays on your machine.
2. A paid OpenRouter vision model, authorised per request with
   `./bin/issue-approval.sh openrouter_paid "<model>"`.
3. Retry the free vision pool with a longer `AGENT_S_TIMEOUT` and accept that it
   will fail often.

Set the grounding model separately from the reasoning model — they do not have
to be the same provider:

```
AGENT_S_GROUND_MODEL=<a vision-capable id>
```

If a live task ends in `timeout` with no actions, this is almost certainly why.
Check `logs/agent-s-worker.log` and the task's artifacts for the grounding call.

## Stage 5 — Vellum

Optional, and only once Stage 2 is done.

- [ ] `vendor/vellum-assistant` linked —
      `mkdir -p vendor && ln -s <path-to-vellum-assistant> vendor/vellum-assistant`
- [ ] The `vellum` CLI is installed — `cd vendor/vellum-assistant && ./setup.sh`
- [ ] `./bin/hatch-vellum.sh` succeeds (it refuses without a ready inference
      path; `--force` overrides)
- [ ] Skills installed — `./bin/install-vellum-skill.sh`
- [ ] Vellum points at the router — base URL `http://127.0.0.1:11435/v1`,
      model `dai/auto`
- [ ] Vellum can load a skill by name — ask it to use `agent-s-delegate`

If no assistant workspace exists yet, the installer stages the skills into
`skills-ready/` and prints the exact copy command.

---

## When something is not ready

```bash
./bin/doctor.sh              # every check, with the fix next to each failure
./bin/dai logs 200           # what the services actually said
./bin/dai config             # what actually resolved, secrets removed
./bin/dai plan               # what the router would try
curl -s localhost:11435/v1/status/cooldowns   # who is sitting out, and until when
curl -s localhost:8765/v1/settings            # includes safety_keys_policy_only
```

`docs/OPERATIONS.md` has a symptom → cause → fix table for the common failures.

Two things that look like failures and are not:

* **`ready_for_chat: false` with no keys.** Correct. Add one key, or start local
  Ollama.
* **`doctor.sh` warning about `vendor/`, `gui-agents`, `Xvfb` or `xset`.**
  Optional dependencies. Dry-run works without any of them, and a fresh clone
  has none.
