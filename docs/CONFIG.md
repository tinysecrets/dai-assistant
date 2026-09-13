# Configuration

Everything the spine reads, where it reads it from, and which value wins.

There is no config file format invented here: one JSON policy file, one
`.env`, and a handful of JSON data files. Nothing is required — a fresh clone
starts and answers `/health` with zero configuration.

---

## The four layers

```
request body        highest   per-call overrides (dry_run, max_steps, model)
      ▲
environment         a real exported variable wins over .env
      ▲
.env                host-specific values, git-ignored
      ▲
policy/sovereign.json   intent and safety, committed
      ▲
built-in defaults   lowest    safe values in the code
```

Two rules override that stack, and both exist for the same reason — an
environment variable must never be able to make the system more dangerous:

1. **Safety keys resolve from the policy file only.** An environment variable
   named for one is ignored entirely. See below.
2. **`.env` never wins over a real environment variable.** That is what lets
   you override one key for one command without editing a file:
   `DAI_COOLDOWN_SECONDS=5 ./bin/dai smoke`.

`.env` is *parsed*, never sourced. Values may contain spaces, quotes or `#`,
and sourcing would export every secret into every child process. The parser is
`lib/dai/env.py` and its accepted syntax is documented in that module.

### Safety keys

These five come from `policy/sovereign.json` → `agent_s` and nowhere else:

| Key | Shipped value | What it guards |
| --- | --- | --- |
| `enabled` | `true` | Whether the worker accepts tasks at all |
| `dry_run_default` | `true` | Whether a task touches input devices by default |
| `bind_owner_live_desktop` | `false` | Never drive the desktop you are sitting at |
| `require_approval_token` | `true` | Whether a live run needs a one-shot token |
| `max_steps_hard_cap` | `20` | Upper bound on agent steps, whatever a caller asks |

The worker reports the list live, so you never have to trust this table:

```bash
curl -s localhost:8765/v1/settings | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["safety_keys_policy_only"])'
```

Changing a safety key means editing the policy file — a deliberate act that
shows up in `git diff`. That is the point.

Every other `agent_s` key is a **host knob**: `.env` first, then policy, then
the built-in default. Host knobs are things that legitimately differ per
machine (which display, which resolution, where the venv lives).

---

## Files

| Path | Committed | Purpose |
| --- | --- | --- |
| `.env.example` | yes | Template; every variable in it is read by something |
| `.env` | **no** | Your keys and host overrides. Mode `0600` |
| `policy/sovereign.json` | yes | Intent and safety |
| `policy/approvals.json` | **no** | Live approval tokens. Mode `0600`, created on first use |
| `policy/approvals.example.json` | yes | Documents the token record shape |
| `config/rotation-pool.json` | yes | Which models each provider rotates through |
| `config/free-models.json` | yes | Reference catalog of free models |
| `logs/` | no | Service and Xvfb logs |
| `state/` | no | Pid files, cooldowns, task records |

Relocate any of them with `DAI_ENV`, `DAI_POLICY`, `DAI_APPROVALS`,
`DAI_AGENT_S_STATE`, `DAI_STATE_DIR`, `DAI_LOG_DIR`.

---

## model-router

Environment variables, with the defaults the code actually uses:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DAI_ROUTER_HOST` | `127.0.0.1` | Bind address. Keep it loopback |
| `DAI_ROUTER_PORT` | `11435` | Listen port |
| `DAI_ROUTER_TOKEN` | *(unset)* | When set, every route but `/health` needs `Authorization: Bearer` |
| `DAI_COOLDOWN_SECONDS` | `90` | How long a rate-limited **model** sits out |
| `DAI_SHORT_COOLDOWN_SECONDS` | `30` | Cooldown for transient/network failures |
| `DAI_PROVIDER_COOLDOWN_SECONDS` | `600` | Cooldown for a whole **provider** after auth or quota failure |
| `DAI_REQUEST_TIMEOUT` | `120` | Per-attempt timeout, non-streaming |
| `DAI_STREAM_TIMEOUT` | `600` | Per-attempt timeout, streaming |
| `DAI_CLIENT_TIMEOUT` | `180` | The router's own outbound calls (status probes) |
| `DAI_STRICT_MODELS` | `false` | When `true`, an unavailable explicit model fails instead of falling back |
| `DAI_MAX_BODY_BYTES` | `8388608` | Request body limit (8 MiB) |
| `DAI_QUIET_LOGS` | `false` | Suppress per-request log lines |
| `DAI_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Provider base URL |
| `DAI_GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Provider base URL |
| `DAI_CEREBRAS_BASE_URL` | `https://api.cerebras.ai/v1` | Provider base URL |
| `DAI_OLLAMA_CLOUD_BASE_URL` | `https://ollama.com/v1` | Provider base URL |
| `DAI_OLLAMA_LOCAL_BASE_URL` | `http://127.0.0.1:11434/v1` | Provider base URL |

Provider keys — see `KEYS.md` for obtaining them:

| Variable | Provider | Notes |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | openrouter | Drives the free top-model rotation |
| `GROQ_API_KEY` | groq | Separate rate-limit bucket |
| `CEREBRAS_API_KEY` | cerebras | Fast fallback |
| `CEREBRAS_MODEL` | cerebras | Overrides the default Cerebras model id |
| `OLLAMA_API_KEY` | ollama_cloud | Ollama Cloud |
| `OPENROUTER_HTTP_REFERER` | openrouter | Sent as `HTTP-Referer` |
| `OPENROUTER_APP_TITLE` | openrouter | Sent as `X-Title` |

`ollama_local` needs no key. It is the last resort and is used only when
`http://127.0.0.1:11434` answers.

### Policy keys the router reads

From `policy/sovereign.json` → `inference`:

| Key | Effect |
| --- | --- |
| `default_model` | Model used when a request omits one (shipped: `dai/auto`) |
| `<provider>.enabled` | `false` removes that provider from every plan |
| `openrouter.paid_enabled` | `true` allows paid OpenRouter models without a per-request token |
| `openrouter.free_only_default` | Documents intent; the pool itself only lists `:free` ids |
| `secret_redaction` | Kept `true`; redaction is not actually optional |

A provider is **on unless the policy explicitly switches it off** — a missing
section is not a disable. That keeps a trimmed policy file from silently
narrowing your rotation.

### Cooldown behaviour worth knowing

* A `429` cools that **model** for `DAI_COOLDOWN_SECONDS`.
* A `401`/`403`/quota error cools the **whole provider** for
  `DAI_PROVIDER_COOLDOWN_SECONDS`, because the same key would fail on every
  model at that provider.
* Provider cooldowns take effect **immediately, even mid-request**. If a
  candidate already selected for the current request becomes cooled, it is
  skipped and the attempt is annotated `"skipped": "cooled_during_request"`.
* Cooldowns persist in `state/` across restarts. Clear them after fixing a
  key: `curl -X POST localhost:11435/v1/status/reset`.

---

## agent-s-worker

| Variable | Default | Meaning |
| --- | --- | --- |
| `DAI_AGENT_S_HOST` | `127.0.0.1` | Bind address |
| `DAI_AGENT_S_PORT` | `8765` | Listen port |
| `DAI_AGENT_S_TOKEN` | *(unset)* | When set, requires `Authorization: Bearer` |
| `DAI_MODEL_ROUTER` | `http://127.0.0.1:11435` | Where the worker sends inference |
| `DAI_AGENT_S_CONCURRENCY` | `1` | Simultaneous tasks. One GUI agent per display |
| `DAI_AGENT_S_MAX_QUEUE` | `32` | Queue bound; clamped to ≥ 1 |
| `DAI_AGENT_S_MAX_TASKS` | `200` | Task records kept before pruning |
| `DAI_AGENT_S_MAX_STEPS` | `15` | Default step budget, clamped to the policy hard cap |
| `DAI_AGENT_S_STATE` | `state/agent-s-tasks` | Task records and artifacts |
| `DAI_AGENT_S_VENV` | `~/.local/agent-s-venv` | Where to look for the `agent_s` binary |
| `DAI_CLIENT_TIMEOUT` | `180` | The worker's own outbound calls |
| `DAI_MAX_BODY_BYTES` | `1048576` | Request body limit (1 MiB) |

Note that `DAI_MAX_BODY_BYTES` and `DAI_CLIENT_TIMEOUT` are read by both
services, and `DAI_MAX_BODY_BYTES` has a **different default** in each: 8 MiB
for the router (prompts can be large), 1 MiB for the worker (a task body is
one instruction). Setting it in `.env` applies to both.

### Live-GUI knobs

Consulted only once policy allows a live run. All are host knobs, so `.env`
wins over policy.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_S_PROVIDER` | `openai` | `openai` means "through the router" |
| `AGENT_S_MODEL` | `dai/auto` | Inference model |
| `AGENT_S_MODEL_URL` | `$DAI_MODEL_ROUTER/v1` | Inference endpoint |
| `AGENT_S_API_KEY` | `$DAI_ROUTER_TOKEN`, else a placeholder | Sent to the model URL |
| `AGENT_S_GROUND_PROVIDER` | `openai` | Grounding provider |
| `AGENT_S_GROUND_MODEL` | `dai/vision-auto` | Vision model for GUI grounding |
| `AGENT_S_GROUND_URL` | `$DAI_MODEL_ROUTER/v1` | Grounding endpoint |
| `AGENT_S_GROUND_API_KEY` | same as `AGENT_S_API_KEY` | |
| `AGENT_S_GROUNDING_WIDTH` | `1280` | Coordinate space for grounding |
| `AGENT_S_GROUNDING_HEIGHT` | `800` | Coordinate space for grounding |
| `AGENT_S_DISPLAY` | `:99` | X display to drive |
| `AGENT_S_DISPLAY_GEOMETRY` | `1280x800x24` | Xvfb screen size |
| `AGENT_S_TIMEOUT` | `600` | Per-task wall clock, seconds |
| `AGENT_S_ALLOW_XVFB` | `1` | Permit a headless display |
| `AGENT_S_EXTRA_ARGS` | *(empty)* | Additional `agent_s` flags, shell-quoted |

Two traps here:

* **`AGENT_S_PROVIDER` must stay `openai`** unless you mean to bypass the
  router. Any other value talks to that provider directly, which loses
  cooldowns, redaction and the paid-model gate.
* **Grounding dimensions must match the display geometry.** The shipped values
  agree (`1280x800` for `1280x800x24`). If you change one, change both, or
  clicks land in the wrong place. This is why `.env.example` leaves them
  commented out.

## voice-bridge

Free, offline STT + TTS. The model-router relays `/v1/audio/*` to it, so
clients see the same OpenAI shapes they would from the router. Runs only if
`~/.local/voice-venv` is installed (with `faster-whisper` and `piper`);
`start-spine.sh` falls back to the system python3 otherwise, and `/v1/audio/*`
then answers `502 voice_bridge_unreachable` (or `500` from the bridge itself).

| Variable | Default | Meaning |
| --- | --- | --- |
| `DAI_VOICE_BRIDGE_HOST` | `127.0.0.1` | Bind address |
| `DAI_VOICE_BRIDGE_PORT` | `8766` | Listen port |
| `DAI_VOICE_BRIDGE_CPUS` | `0-2` | CPU affinity (`taskset -c`); blank disables pinning |
| `DAI_VOICE_BRIDGE_THREADS` | `4` | Thread hint for faster-whisper/ONNXRuntime (~1 per core) |
| `DAI_VOICE_BRIDGE_WORKERS` | `4` | Cap concurrent STT/TTS requests (each is CPU-bound) |
| `DAI_VOICE_BRIDGE_TOKEN` | *(unset)* | When set, requires `Authorization: Bearer` |
| `DAI_WHISPER_MODEL` | `tiny` | faster-whisper model to load (tiny/base/small) |
| `DAI_PIPER_VOICE` | `en_US-lessac-medium` | piper voice id, downloaded on first use |
| `DAI_VOICE_MODELS_DIR` | `~/.local/voice-models` | Where STT/TTS models are cached |
| `DAI_VOICE_BRIDGE_URL` | `http://127.0.0.1:8766` | What the router relays audio to. Derived from host/port unless overridden — set all three together |

The bridge also reads `DAI_QUIET_LOGS` and `DAI_MAX_BODY_BYTES`, which default
as documented for the router: the bridge normally grants 8 MiB via `DAI_MAX_BODY_BYTES`,
so a long recording can be transcribed without touching the 1 MiB worker value.

---

## policy/sovereign.json

Committed intent. Version 3.

| Section | Keys | Read by |
| --- | --- | --- |
| `mode` | `cloud_first_free_rotation` | informational |
| `ask_before` | list of action names | documents what needs approval |
| `inference` | `default_model`, `router_url`, per-provider `enabled`, `openrouter.paid_enabled`, `secret_redaction`, `never_send_to_cloud` | router |
| `agent_s` | the safety keys plus every host knob | worker |
| `approvals` | `file`, `default_ttl_seconds`, `default_max_uses`, `require_scope_match` | approval store |
| `vellum` | `cli`, `point_inference_at`, `skills_to_install` | `bin/hatch-vellum.sh` |
| `channels` | `phone_pairing`, `do_not_use` | informational |
| `projects_left_untouched` | list of paths | a standing instruction: never modify these |

`agent_s.notes` is an array of strings shipped inside the file so the rationale
travels with the setting rather than living only in a doc someone has to find.

The worker validates the policy at startup and reports problems rather than
failing:

```bash
python3 services/agent-s-worker/server.py --check
```

A corrupt or missing policy file is **not** a crash: the worker falls back to
built-in safe defaults (dry-run on, token required) and says so in
`config_errors`.

---

## config/rotation-pool.json

Model ids per provider. Top-level keys that hold lists are pools; the rest is
metadata.

| Key | Meaning |
| --- | --- |
| `version`, `strategy`, `quality_bar`, `notes` | metadata |
| `openrouter_free` | text rotation, OpenRouter `:free` models |
| `openrouter_free_vision` | vision-capable models, used for GUI grounding |
| `groq_models` | Groq rotation |
| `ollama_cloud_models` | Ollama Cloud rotation |
| `ollama_local_models` | local Ollama, last resort |

Rules the test suite enforces (`tests/test_repo_consistency.py`):

* every provider's `pool_key` must exist in this file
* the only extra pool allowed is `openrouter_free_vision`
* every `openrouter*` entry must end in `:free` — the whole strategy is free
  tier, and a paid id here would bill you
* no duplicates within a pool

`openrouter_free_vision` is **not** required to be a subset of
`openrouter_free`. The planner expands it into candidates on its own, and also
uses it to reorder the text pool when a request looks vision-shaped.

A request for `dai/vision-auto` is answered **only** by vision-capable models.
Text-only models are never substituted into it, and if the pool is empty or
OpenRouter has no key the plan comes back empty with the note
`no vision-capable candidates available (needs OPENROUTER_API_KEY)` rather than
quietly degrading to a text model. That matters for GUI grounding: a text model
asked "where is the search box" will answer confidently and wrongly, and the
agent will click somewhere plausible. A refusal is recoverable; a confident
wrong coordinate is not.

Edit it freely; the file is re-read on a short cache interval, so a running
router picks up changes without a restart.

## config/free-models.json

A reference catalog of free models, used to enrich `/v1/models` and to seed
vision detection. Large by nature. Not required for routing — the pool is.

---

## Inspecting what actually resolved

Never guess. Each service reports its resolved configuration with secrets
removed:

```bash
# router: config + readiness
python3 services/model-router/server.py --print-config
curl -s localhost:11435/v1/status/config | python3 -m json.tool

# router: which keys are present, and which are missing
curl -s localhost:11435/v1/status/keys | python3 -m json.tool

# router: what it would try, without calling anything
./bin/dai plan
./bin/dai plan ollama_local/hermes3:8b

# worker: resolved settings + the safety-key list
curl -s localhost:8765/v1/settings | python3 -m json.tool

# both, plus files, keys, services and optional GUI deps
./bin/doctor.sh
./bin/doctor.sh --json
```

`--check` on either service validates configuration and exits non-zero if
something is wrong, which is what CI uses.

---

## Common mistakes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Setting an env var does nothing | It names a safety key | Edit `policy/sovereign.json` instead |
| `.env` value ignored | A real environment variable is set | `env | grep DAI_` and unset it |
| `ready_for_chat` false | No cloud key and no local Ollama | `KEYS.md`, or start Ollama |
| Clicks land in the wrong place | Grounding size ≠ display geometry | Match `AGENT_S_GROUNDING_*` to the geometry |
| Inference bypasses the router | `AGENT_S_PROVIDER` is not `openai` | Set it back to `openai` |
| Same model fails every request | It is cooled | `curl -s localhost:11435/v1/status/cooldowns` |
| Everything at one provider fails | Provider-wide cooldown after an auth error | Fix the key, then `POST /v1/status/reset` |
| Port already in use | A foreign process holds it | `ss -ltnp \| grep 11435`; `start-spine.sh` refuses to fight it |

---

## What must never go into a model request

The router redacts secrets on the way *out* of logs, errors and task records,
but it cannot unsend what you put in a prompt. Anything in a request body may
reach a third-party provider and be retained there.

Never include:

* API keys or the contents of `.env`
* SSH private keys, or any private key material
* Password dumps or credential stores
* Browser cookies or session tokens
* Approval tokens (they are single-use; leaking one is leaking a live
  authorisation)

This applies doubly to GUI tasks, where the agent can read the screen: a task
instruction like "log in to my bank" turns the display into a place where
credentials appear, and screenshots of it are stored under `state/`. Keep
credentials out of the agent's reach rather than relying on redaction.

See `SECURITY.md` for how redaction works and what it cannot do.

---

## Related

* `docs/API.md` — endpoints, payloads, error codes
* `docs/OPERATIONS.md` — runbook
* `docs/ARCHITECTURE.md` — why the spine is shaped this way
* `KEYS.md` — obtaining provider keys
* `.env.example` — the annotated template
