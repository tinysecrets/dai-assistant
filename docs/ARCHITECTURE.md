# Architecture

What the spine is, why it is shaped this way, and which parts are load-bearing.

For endpoint detail see `docs/API.md`; for settings see `docs/CONFIG.md`; for the
threat model see `SECURITY.md`.

---

## The shape

```
Owner (desktop, later phone pairing)
   │
   ▼
Vellum Assistant ── identity, memory, planning, approvals
   │  skills: agent-s-delegate, english-to-code
   │
   ├──► model-router  :11435  ──► OpenRouter   [primary, free tier, rotates]
   │     OpenAI-compatible      ─► Groq        [separate rate-limit bucket]
   │     /v1                    ─► Cerebras    [fast fallback]
   │                            ─► Ollama Cloud
   │                            ─► Ollama local :11434  [last resort]
   │
   └──► agent-s-worker :8765
          │  policy gate: enabled → dry-run? → approval token
          │  queue (bounded, serialised)
          ▼
        dedicated display :99 (Xvfb)
          │
          ▼
        Agent S / gui-agents ──inference──► back through model-router
```

Three separations do most of the work:

1. **Only the router talks to a provider.** Cooldowns, redaction, the paid-model
   gate and rotation all live in exactly one place, so they cannot be bypassed by
   accident.
2. **The worker talks to the router, not to a model.** `AGENT_S_PROVIDER=openai`
   means "OpenAI-compatible, i.e. the router", and `AGENT_S_BASE_URL` points at
   `:11435`. Setting `AGENT_S_PROVIDER` to anything else is a deliberate act
   that loses all four protections.
3. **The agent gets its own display.** `:99` under Xvfb, never the owner's
   session. `bind_owner_live_desktop` is a safety key that no environment
   variable can flip.

---

## Why the spine is separate from the assistant

Vellum holds identity and memory. The spine holds inference and actuation.
Keeping them apart means:

* The assistant can be replaced, re-hatched or reconfigured without losing the
  inference path — and the inference path can be restarted without disturbing
  the assistant's state.
* Skills call a **stable local API**, not a model directly. A skill that says
  "delegate this GUI task" does not need to know which provider answered.
* Policy lives in one file that both services read, so a rule change applies
  everywhere at once.
* Nothing here overwrites another assistant's working tree.
  `policy/sovereign.json` lists the paths that must never be touched.

The cost is an extra hop. On localhost that is microseconds, which is a good
trade for one enforcement point.

---

## Why rotation is per group, not per model

A `429` from one OpenRouter model says nothing about another — they have
independent limits. An auth failure says everything about the provider: the same
key would fail on every model there.

So the router cools at two different scopes:

| Failure | Cooled | Default |
| --- | --- | --- |
| `429` rate limit | that model | `DAI_COOLDOWN_SECONDS` = 90 |
| `401`/`403`, quota, billing | the whole provider | `DAI_PROVIDER_COOLDOWN_SECONDS` = 600 |
| Network error, non-JSON | that model briefly | `DAI_SHORT_COOLDOWN_SECONDS` = 30 |

Provider cooldowns take effect **mid-request**. The candidate snapshot is
re-checked at the top of every attempt, so a model whose provider failed auth
two attempts ago is skipped rather than tried. Skipped candidates are annotated
`"skipped": "cooled_during_request"` so the attempt list still explains itself.

Rotation happens **within a group**, so an explicitly requested model stays
first and the vision boost survives round-robin. A naive global rotation moved
the requested model out of slot one, which is not rotation — it is substitution.

`config/rotation-pool.json` holds the model lists; `lib/dai/routing.py` holds
the provider specs. A test fails if an OpenRouter entry is not `:free`, so the
pool cannot quietly start spending money.

---

## Why streaming rotates only before the first byte

Rotation is invisible while the client has received nothing. Once a byte has
been delivered, a retry would mean either silently restarting an answer mid-
sentence or emitting two answers.

So: rotate freely until the first byte, then commit. On a mid-stream failure the
router forwards what arrived and ends the stream cleanly. A client that asked for
`stream: true` gets a real SSE stream — previously the router forced it to
`false`, which made streaming support a lie.

The usage trailer is emitted **before** `[DONE]`, because most SSE clients stop
reading at `[DONE]`. The router intercepts the upstream terminator rather than
passing it through.

---

## Why dry-run is the default, and live needs two acts

A GUI agent that can click anything is the most dangerous component here, and it
is also the one whose failures are hardest to notice — a task that silently did
nothing looks identical to one that worked.

So live execution requires **both**:

1. `agent_s.dry_run_default: false` in the policy file (a standing change), or
   `"dry_run": false` in the request (a per-task decision), **and**
2. A single-use approval token scoped to the **exact instruction**.

The token is bound to the instruction string, expires after an hour, is spent
atomically at admission, and is spent even if the task then fails — a token
authorises one *attempt*, so a failure cannot be retried by replaying it.

Both are safety keys or token-gated, and neither can be set from the
environment. A dry run returns `would_run`: the full `agent_s` argv with secrets
redacted. Show that to the owner before asking for approval — it is the only way
to see what "yes" actually means.

`live_blockers` in the same response lists what a live run would need, so the
conversation is concrete rather than speculative.

---

## Why tasks are serialised

`DAI_AGENT_S_CONCURRENCY` defaults to `1`. Two agents driving the same display
interleave their clicks, and neither can reason about the screen it is looking
at. The queue is bounded (`DAI_AGENT_S_MAX_QUEUE`), so a burst is refused with
`429 queue_full` rather than accepted and dropped.

`maxsize=0` on `queue.Queue` means unbounded — an early version of this worker
had a "bounded" queue that was not.

---

## Why everything is stdlib

The spine has to run on a bare Debian `python3` with no install step and no
network access. A dependency is a failure mode: it needs resolving, it breaks on
a Python upgrade, and it arrives with its own supply chain.

The pieces people usually reach for a package for are small enough to own:

| Need | Owned by |
| --- | --- |
| `.env` without exporting secrets | `lib/dai/env.py` — parsed, never sourced |
| Secret redaction | `lib/dai/redact.py` |
| Atomic JSON with locking | `lib/dai/jsonio.py` — `RLock`, temp file + rename |
| HTTP client and SSE | `lib/dai/httpclient.py` |
| HTTP server, errors, bounded bodies | `lib/dai/httpserver.py` |
| Approval tokens | `lib/dai/approvals.py` |
| Provider specs and rotation | `lib/dai/routing.py` |
| Per-candidate counters | `lib/dai/stats.py` |

Targeting Python 3.9 means `from __future__ import annotations` for modern type
syntax, and no `match` statements or runtime `X | Y`.

---

## Why configuration has four layers

```
request body  >  environment  >  .env  >  policy/sovereign.json  >  built-in defaults
```

Two rules override it, both so an environment variable can never make the system
more dangerous:

1. **Safety keys resolve from the policy file only.** `enabled`,
   `dry_run_default`, `bind_owner_live_desktop`, `require_approval_token`,
   `max_steps_hard_cap`. The worker publishes the list at `/v1/settings` →
   `safety_keys_policy_only`, so a caller can see what it cannot change.
2. **A real environment variable beats `.env`**, so one key can be overridden for
   one command without editing a file.

`.env` is parsed rather than sourced: sourcing exports every secret into every
child process and breaks on values containing spaces or quotes.

A corrupt policy or pool file degrades to safe built-in defaults and reports
itself in `config_errors`. A configuration file should not be able to take a
service down.

---

## Why `--check` separates problems from warnings

`--check` is used two ways: as a CI gate, and by an operator asking "what is
wrong?". Those need different answers on a machine that has not installed the
optional GUI stack yet.

* **`problems`** are configuration or safety faults. Non-empty → exit 1. An
  unreadable policy, no `agent_s` object, `bind_owner_live_desktop` true,
  `dry_run_default` false, `config_errors`.
* **`warnings`** are missing optional dependencies. Exit 0. No `agent_s` binary,
  no answering display.

Conflating the two meant `make check` failed on a fresh clone, which taught
operators to ignore it — the worst outcome for a check. `doctor.sh` reads both
lists structurally rather than re-classifying message text.

---

## Runtime layout

| Path | Written by | Committed |
| --- | --- | --- |
| `.env` | you, or `bin/import-keys.sh` | never |
| `policy/approvals.json` | `bin/issue-approval.sh`, the worker | never |
| `state/agent-s-tasks/` | the worker: records, logs, screenshots | never |
| `logs/` | both services | never |
| `vendor/` | you, by hand | never |
| `skills-ready/` | `bin/install-vellum-skill.sh` | never |

All git-ignored, all regenerable. Deleting `state/` and `logs/` is safe.

---

## Non-goals

* No overwriting another assistant's working tree (Hermes, NOVA, LIYA, OpenClaw,
  Genie). The policy lists protected paths.
* No paid provider calls without an explicit policy change or a per-request
  token.
* No live control of the owner's everyday desktop session.
* No `sudo` or package installs without the operator running them.
* No CORS. Both services are localhost-only; a browser page must not be able to
  drive GUI automation or spend inference budget.
* No sandboxing of the applications the agent drives. Containment is the
  dedicated display, bounded steps and a timeout — not isolation.
* No rate limiting of local callers. The router forwards to providers and cools
  down on their `429`s; it does not police you.

---

## Where this is going

The design anticipates phone pairing and a second actuation path. Two things
would have to stay true:

* A new actuator gets its **own** worker with its own policy section, rather than
  growing flags inside `agent_s`. The safety keys are the contract.
* A new provider becomes a `ProviderSpec` in `lib/dai/routing.py` plus a pool,
  not a special case in the handler.

Both are additive. Neither changes what a client already sees.
