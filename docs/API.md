# API reference

Two localhost services make up the spine. Both speak JSON over HTTP and share
one error envelope, one redaction rule, and one auth scheme.

| Service | Default | Purpose |
| --- | --- | --- |
| `model-router` | `http://127.0.0.1:11435` | OpenAI-compatible inference with free-model rotation |
| `agent-s-worker` | `http://127.0.0.1:8765` | Bounded GUI-agent tasks (dry-run by default) |

Everything here is captured from the running services, not from intent. To
re-verify any of it: `./bin/start-spine.sh && ./bin/smoke.sh`.

---

## Conventions

### Content type

Send and expect `application/json`. Streaming responses are
`text/event-stream` (SSE).

### Error envelope

Every non-2xx response has the same shape:

```json
{
  "error": "invalid_json",
  "detail": "Request body is not valid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
}
```

`error` is a stable machine-readable code (see the tables at the end). `detail`
is prose for a human and may change wording. Some errors add fields —
`attempts` on routing failures, `task_id` on worker admission failures.

**Never parse `detail`.** Branch on `error`.

### Redaction

Both services register every secret-looking environment value with a redactor
at startup. Any value that reaches a log line, an error `detail`, or a recorded
task is replaced with `[REDACTED]`. This is why a dry-run `would_run` array
shows `[REDACTED]` where an API key would be:

```json
"--model_api_key", "[REDACTED]"
```

Redaction is applied to *values*, not key names, so a secret that matches no
known token pattern is still scrubbed.

### Authentication

Off by default — both services bind `127.0.0.1` and are meant for a single
machine. To require a bearer token:

```
DAI_ROUTER_TOKEN=...        # model-router
DAI_AGENT_S_TOKEN=...       # agent-s-worker
```

Then every route except `/health` needs `Authorization: Bearer <token>`.
A missing or wrong token returns `401 {"error": "unauthorized"}`.

`/health` stays open deliberately so monitoring and `./bin/doctor.sh` work
before any key exists.

### No CORS

Neither service sends `Access-Control-Allow-*` headers, and neither will. A
browser page must not be able to drive GUI automation or spend inference
budget. Call these from a local process.

### Trailing slashes

`/v1/tasks/` is normalised to the collection endpoint (`/v1/tasks`). Other
paths are matched exactly; an unknown path is a `404`, not a redirect.

---

## model-router

### `GET /health`

Full readiness report. Always `200`, even when nothing can chat — the payload
says what is missing.

```json
{
  "ok": true,
  "service": "model-router",
  "version": "1.1",
  "mode": "cloud_first_free_rotation",
  "ready_for_chat": false,
  "providers_configured": {"openrouter": false, "groq": false, "cerebras": false,
                           "ollama_cloud": false, "ollama_local": true},
  "providers_enabled": ["openrouter", "groq", "cerebras", "ollama_cloud", "ollama_local"],
  "candidates_available": 2,
  "cooldowns_active": 0,
  "streaming": true,
  "uptime_seconds": 0.2,
  "pool": "/abs/path/config/rotation-pool.json",
  "config_errors": [],
  "notes": []
}
```

`ready_for_chat` is the field to act on: it is `true` only when at least one
candidate can actually be reached (a cloud key is set, or local Ollama
answers). `candidates_available` counts models the planner would try right now.

### `GET /ready`

Same payload as `/health`, but returns **`503`** when `ready_for_chat` is
false. Use this one in orchestrators and healthchecks that should fail loudly;
use `/health` when you want to render *why*.

### `GET /version`

```json
{"service": "model-router", "version": "1.1", "docs": "docs/API.md", "python": "3.11.2"}
```

### `GET /v1/models`

OpenAI-shaped list. Two virtual ids come first, then every pool model the
router can currently reach:

```json
{
  "object": "list",
  "data": [
    {"id": "dai/auto", "object": "model", "owned_by": "debian-ai",
     "description": "Rotation across cloud free + local pools (text focused)"},
    {"id": "dai/vision-auto", "object": "model", "owned_by": "debian-ai",
     "description": "Rotation across vision-capable free models (GUI grounding)"},
    {"id": "openrouter/z-ai/glm-5.2:free", "object": "model", "owned_by": "openrouter",
     "dai": {"provider": "openrouter", "model": "z-ai/glm-5.2:free",
             "origin": "pool", "free": true}}
  ]
}
```

Virtual ids:

| id | Meaning |
| --- | --- |
| `dai/auto` | Rotate across every enabled provider, text-optimised |
| `dai/vision-auto` | Rotate across vision-capable free models (GUI grounding) |

Any provider model can also be requested explicitly as `<provider>/<model>`,
e.g. `groq/llama-3.3-70b-versatile` or `ollama_local/hermes3:8b`.

### `POST /v1/chat/completions`

OpenAI-compatible, plus rotation metadata.

```json
{
  "model": "dai/auto",
  "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
  "stream": false
}
```

Accepted fields are the usual OpenAI ones (`model`, `messages`, `stream`,
`temperature`, `max_tokens`, `tools`, `tool_choice`, `response_format`, …).
Unknown fields are forwarded upstream rather than rejected.

Two router-specific fields are consumed and **not** forwarded:

| Field | Purpose |
| --- | --- |
| `dai_approval_token` | One-shot token authorising a paid OpenRouter model |
| `dai_hint` | Text used to detect a vision request and boost the vision pool |

The approval token may also be sent as a header: `X-DAI-Approval-Token: <token>`.

#### Response

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "z-ai/glm-5.2:free",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
               "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
  "dai_routed": {
    "provider": "openrouter",
    "model": "z-ai/glm-5.2:free",
    "requested": "dai/auto",
    "attempts": 1,
    "fallback_from": null,
    "streamed": false
  }
}
```

`dai_routed` is the router's own trailer. `attempts` counts providers tried;
`fallback_from` is set when you asked for a specific model and the router
substituted one (see `DAI_STRICT_MODELS` in `docs/CONFIG.md` to forbid that).

#### Streaming

With `"stream": true` the response is SSE. Each chunk is OpenAI-shaped:

```
: dai_routed {"provider":"openrouter","model":"z-ai/glm-5.2:free","attempts":1}

data: {"id":"...","object":"chat.completion.chunk","choices":[{"delta":{"content":"pong"}}]}

data: {"usage":{"prompt_tokens":12,"completion_tokens":1,"total_tokens":13},"dai_routed":{...}}

data: [DONE]
```

Two guarantees worth relying on:

* The routing decision arrives as an **SSE comment line** (`:`) before the
  first content chunk, so a client can log which model answered even if it
  discards comments.
* The usage/`dai_routed` trailer is emitted **before** `data: [DONE]`. The
  router intercepts the upstream `[DONE]` to keep that ordering, because a
  trailer after `[DONE]` is dropped by most client libraries.

Rotation still happens for streaming requests, but only **before the first
byte** is written. Once a provider has started streaming, a mid-stream failure
cannot be retried transparently — the client sees the truncation. That is the
honest behaviour; silently restarting a half-delivered answer is worse.

#### Errors

| Status | `error` | Cause |
| --- | --- | --- |
| 400 | `invalid_json` | Body is not parseable JSON |
| 400 | `messages_required` | `messages` missing or empty |
| 400 | `invalid_model` | `model` is present but not a usable id |
| 401 | `unauthorized` | Bearer token required and missing/wrong |
| 403 | `paid_models_disabled` | Paid OpenRouter model without policy opt-in or a token |
| 403 | `missing_approval_token` | Token required and not supplied |
| 403 | `unknown_approval_token` | Token is not in `policy/approvals.json` |
| 403 | `approval_expired` | Past its TTL |
| 403 | `approval_exhausted` | Already used `max_uses` times |
| 403 | `approval_scope_mismatch` | Token authorises a different model |
| 403 | `approval_action_mismatch` | Token is for another action (e.g. `agent_s_gui_task`) |
| 405 | `method_not_allowed` | Wrong verb for this path |
| 413 | `payload_too_large` | Over `DAI_MAX_BODY_BYTES` |
| 503 | `no_providers_ready` | No key configured at all, and no local Ollama |
| 503 | `requested_model_unavailable` | A key exists, but not for the model you asked for |
| 503 | `all_candidates_failed` | Every candidate failed; see `attempts` |

`no_providers_ready` and `requested_model_unavailable` are deliberately
different: the first means "add any key", the second means "add *that*
provider's key". Both are normal on a fresh clone.

`all_candidates_failed` includes the per-candidate breakdown, which is the
fastest way to diagnose a rotation problem:

```json
{
  "error": "all_candidates_failed",
  "detail": "Every candidate failed or was rate-limited. Wait for cooldowns to expire, or add another provider key (see KEYS.md).",
  "attempts": [
    {"candidate": "openrouter:google/gemma-4-31b-it:free", "status": 429,
     "kind": "rate_limit", "error": "Rate limit exceeded", "cooldown_applied": 90.0}
  ]
}
```

`kind` is one of `rate_limit`, `auth`, `quota`, `server`, `network`,
`bad_response`, `unknown`. `auth` and `quota` cool the **whole provider**, not
just the model, and take effect immediately — including for candidates already
selected for the current request.

### `POST /v1/completions`

Legacy (non-chat) completions. Same routing, but requires `prompt`:

```json
{"model": "dai/auto", "prompt": "Once upon a time"}
```

Missing `prompt` → `400 {"error": "prompt_required"}`.

### Deliberately unsupported

These return **`501`** with an explanation rather than `404`, because a client
that asks for them deserves to know why:

| Path | `detail` |
| --- | --- |
| `/v1/embeddings` | Vellum embeds locally (ONNX) by default; the router does not proxy embeddings. |
| `/v1/audio/transcriptions` | Audio transcription is not part of the spine. |
| `/v1/audio/speech` | Text-to-speech is not part of the spine. |
| `/v1/images/generations` | Image generation is not part of the spine. |
| `/v1/moderations` | Moderation is handled upstream by the provider. |

### Status endpoints

All read-only except `reset`. None exposes a secret.

#### `GET /v1/status/config`

Resolved configuration: host, port, env file path and existence, whether auth
is required, timeouts, pool/catalog/policy paths, and any config parse errors.

#### `GET /v1/status/keys`

```json
{
  "providers": {"openrouter": true, "groq": false, "cerebras": false,
                "ollama_cloud": false, "ollama_local": true},
  "keys_needed": ["GROQ_API_KEY", "CEREBRAS_API_KEY", "OLLAMA_API_KEY"],
  "env_file": "/abs/path/.env",
  "env_exists": true,
  "redacted_secrets": 2
}
```

Booleans only — never a key value. `keys_needed` is what to add to `.env` to
widen the rotation.

#### `GET /v1/status/cooldowns`

```json
{"count": 1, "cooldowns": {"openrouter:google/gemma-4-31b-it:free": 1789204199.5}}
```

Values are absolute unix timestamps: the moment the candidate becomes eligible
again. Keys are either `provider:model` (a single model) or `provider`
(every model at that provider).

#### `GET /v1/status/stats`

Per-candidate counters (requests, successes, failures by kind). Empty until
traffic has flowed.

#### `GET /v1/status/plan`

What the planner *would* try, without calling anything. Accepts
`?model=<id>`:

```json
{
  "requested": {"provider": "ollama_local", "model": "hermes3:8b"},
  "fallback_from": null,
  "notes": [],
  "candidates": [
    {"provider": "ollama_local", "model": "hermes3:8b",
     "public_id": "ollama_local/hermes3:8b",
     "base_url": "http://127.0.0.1:11434/v1",
     "origin": "pool", "cooldown_key": "ollama_local:hermes3:8b"}
  ]
}
```

`origin` is `pool`, `explicit`, or `vision`. This is the endpoint to use when
rotation behaves surprisingly — it shows the candidate order and which entries
are cooled.

Shortcut: `./bin/dai plan [model]`.

#### `POST /v1/status/reset`

Clears all cooldowns. Returns the number cleared. Useful after fixing a key:

```
curl -X POST http://127.0.0.1:11435/v1/status/reset
```

---

## agent-s-worker

### `GET /health`

```json
{
  "ok": true,
  "service": "agent-s-worker",
  "version": "1.1",
  "enabled": true,
  "dry_run_default": true,
  "display": ":99",
  "display_ready": false,
  "bind_owner_live_desktop": false,
  "require_approval_token": true,
  "agent_s_installed": false,
  "agent_s_path": null,
  "live_capable": false,
  "grounding_model": "dai/vision-auto",
  "model": "dai/auto",
  "model_url": "http://127.0.0.1:11435/v1",
  "queue_depth": 0,
  "queue_max": 32,
  "running": [],
  "concurrency": 1,
  "approvals_file": "/abs/path/policy/approvals.json",
  "approvals_present": false,
  "uptime_seconds": 0.2,
  "config_errors": [],
  "ready": true
}
```

Two readiness fields, and the difference matters:

* **`ready`** — the worker can accept *something*. True when enabled and
  either dry-run is the default or a live run is actually possible. Dry-run
  needs no `agent_s` binary and no display, so a fresh clone is `ready: true`.
* **`live_capable`** — a live GUI run could succeed right now: `agent_s`
  installed, display answering, router reachable. False on a fresh clone.

### `GET /ready`

Same payload; `503` when `ready` is false.

### `GET /version`

```json
{"service": "agent-s-worker", "version": "1.1", "docs": "docs/API.md"}
```

### `GET /v1/settings`

The resolved settings the worker will apply, plus the safety contract:

```json
{
  "settings": {
    "enabled": true, "dry_run_default": true,
    "bind_owner_live_desktop": false, "require_approval_token": true,
    "display": ":99", "geometry": "1280x800x24",
    "provider": "openai", "model": "dai/auto",
    "model_url": "http://127.0.0.1:11435/v1",
    "ground_provider": "openai", "ground_model": "dai/vision-auto",
    "grounding_width": 1280, "grounding_height": 800,
    "task_timeout_seconds": 600, "max_steps_default": 15,
    "max_steps_hard_cap": 20, "venv": "/abs/path/.local/agent-s-venv",
    "extra_args": "", "allow_xvfb": true
  },
  "safety_keys_policy_only": ["enabled", "dry_run_default",
    "bind_owner_live_desktop", "require_approval_token", "max_steps_hard_cap"],
  "policy_path": "/abs/path/policy/sovereign.json",
  "env_file": "/abs/path/.env",
  "config_errors": []
}
```

`safety_keys_policy_only` is the machine-readable statement of the precedence
rule: those five resolve from the policy file **only**. An environment variable
named for them is ignored, so nothing in `.env` can switch on live desktop
control or drop the approval requirement. Every other key is
`.env` → policy → built-in default. See `docs/CONFIG.md`.

Note `provider: "openai"` means "through the model-router", which keeps
cooldowns, redaction and the paid gate in force. Any other value talks to that
provider directly and bypasses the router.

### `POST /v1/tasks`

```json
{
  "instruction": "Open Chromium and go to example.com",
  "dry_run": true,
  "max_steps": 15,
  "approval_token": "..."
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `instruction` | yes | One bounded task, ≤ 4000 chars |
| `dry_run` | no | Defaults to policy `agent_s.dry_run_default` (true as shipped) |
| `max_steps` | no | Clamped to `max_steps_hard_cap` (20) |
| `approval_token` | for live runs | Required when `dry_run` is false |

Returns **`202`** with a flat body (no wrapper object):

```json
{"id": "fb9c5f3b-2a96-4f65-8af5-a20d80315a79", "status": "queued", "dry_run": true}
```

The task then runs asynchronously. Poll `GET /v1/tasks/{id}`.

#### Dry-run result

A dry run validates the request, records the **exact** command that a live run
would execute, and touches no input device:

```json
{
  "id": "fb9c5f3b-...",
  "instruction": "Open Chromium and go to example.com",
  "max_steps": 15,
  "dry_run": true,
  "created_at": 1789204104.77,
  "status": "dry_run_complete",
  "result": {
    "status": "dry_run_complete",
    "actions": [],
    "artifacts": [],
    "screenshots_allowed": false,
    "live_owner_desktop": false,
    "summary": "Dry-run accepted on display :99: Open Chromium and go to example.com",
    "would_run": ["agent_s", "--provider", "openai", "--model", "dai/auto",
                  "--model_api_key", "[REDACTED]", "...",
                  "--task", "Open Chromium and go to example.com"],
    "live_blockers": ["gui-agents not installed", "display :99 is not up"]
  }
}
```

`would_run` is the audit trail: it shows precisely what would be invoked, with
secrets redacted. `live_blockers` lists what is missing before a live run could
work — the same information `./bin/doctor.sh` reports.

#### Live runs

A live task needs **all** of: policy `agent_s.dry_run_default` allowing it (or
an explicit `"dry_run": false`), `live_capable` true, and a valid approval
token scoped to that exact instruction.

Tokens are issued per instruction and spent at admission:

```
TOKEN=$(./bin/issue-approval.sh agent_s_gui_task "Open Chromium and go to example.com")
curl -X POST http://127.0.0.1:8765/v1/tasks \
  -H 'Content-Type: application/json' \
  -d "{\"instruction\":\"Open Chromium and go to example.com\",\"dry_run\":false,\"approval_token\":\"$TOKEN\"}"
```

The token is consumed **even if the task later fails** — spending happens at
admission, so a failure cannot be retried with the same token. Issue a new one.
That is intentional: a token authorises one attempt, not one success.

#### Errors

| Status | `error` | Cause |
| --- | --- | --- |
| 400 | `instruction_required` | Empty or missing `instruction` |
| 400 | `instruction_too_long` | Over 4000 characters |
| 400 | `invalid_max_steps` | `max_steps` is not an integer |
| 400 | `invalid_json` | Body is not parseable JSON |
| 400 | `body_required` | No request body at all |
| 400 | `invalid_task_id` | Id is not a UUID |
| 401 | `unauthorized` | Bearer token required and missing/wrong |
| 403 | `missing_approval_token` | Live task with no token |
| 403 | `unknown_approval_token` | Token not in `policy/approvals.json` |
| 403 | `approval_scope_mismatch` | Token's scope is a different instruction |
| 403 | `approval_action_mismatch` | Token is for another action (e.g. `openrouter_paid`) |
| 403 | `approval_expired` | Past its TTL |
| 403 | `approval_exhausted` | Already used `max_uses` times |
| 403 | `agent_s_disabled_in_policy` | `agent_s.enabled` is false |
| 403 | `live_desktop_forbidden_until_repolicy` | `bind_owner_live_desktop` is true |
| 404 | `artifact_gone` | Artifact was recorded but the file no longer exists |
| 405 | `method_not_allowed` | Wrong verb for this path |
| 413 | `payload_too_large` | Over `DAI_MAX_BODY_BYTES` |
| 429 | `queue_full` | `DAI_AGENT_S_MAX_QUEUE` reached |

A live run that is *accepted* can still fail later — those are task result
codes, not HTTP codes, and are listed below.

Admission errors include the id they would have used, so a client can correlate
a rejection with its own request:

```json
{"error": "missing_approval_token",
 "detail": "This action needs a token: bin/issue-approval.sh agent_s_gui_task <scope>",
 "task_id": "6989f814-1bd8-4379-96c6-f1d8d0180be2"}
```

### `GET /v1/tasks`

Lists task summaries, newest first.

Query parameters:

| Param | Default | Notes |
| --- | --- | --- |
| `status` | any | Filter to one status (see lifecycle below) |
| `limit` | 50 | Clamped to 1–500 |

```json
{
  "object": "list",
  "count": 3,
  "queue_depth": 0,
  "running": [],
  "data": [
    {"id": "fb9c5f3b-...", "instruction": "Open Chromium and go to example.com",
     "status": "dry_run_complete", "dry_run": true,
     "created_at": 1789204104.77, "started_at": 1789204104.77,
     "finished_at": 1789204104.78, "error": null,
     "summary": "Dry-run accepted on display :99: ...", "artifacts": 0}
  ]
}
```

Summaries are deliberately small; `artifacts` is a count, not a list.

### `GET /v1/tasks/{id}`

The full record, including `result` with `actions`, `artifacts`, `would_run`
and any `error`/`detail`.

* `400 invalid_task_id` — not a UUID
* `404 not_found` — no such task (records are pruned past `DAI_AGENT_S_MAX_TASKS`)

### `GET /v1/tasks/{id}/artifacts/{name}`

Serves one recorded artifact (screenshots, `agent_s.stdout.log`,
`agent_s.stderr.log`).

`{name}` must be the basename of a path already present in the task's
`result.artifacts`. Anything else is refused — this endpoint cannot be used to
read arbitrary files, and traversal (`..`, encoded `%2F`) is rejected with
`400`. Files are stored mode `0600`.

### `POST /v1/tasks/{id}/cancel`

Cancels a queued or running task.

* Running → `202 {"cancelling": true}`; the agent process is signalled and the
  final status becomes `cancelled`.
* Already terminal → `200 {"cancelled": false, "detail": "already finished"}`.
  Cancelling a finished task is not an error.
* Never started → `200 {"cancelled": true}`, status `cancelled`.

### Task status lifecycle

```
queued ──> running ──> succeeded
   │           ├──> dry_run_complete
   │           ├──> failed
   │           ├──> blocked
   │           ├──> timeout
   │           ├──> cancelled
   │           └──> interrupted
   └──> cancelled          (never started)
```

| Status | Meaning |
| --- | --- |
| `queued` | Accepted, waiting for a worker slot |
| `running` | Executing (or dry-run validating) |
| `dry_run_complete` | Dry run finished; see `result.would_run` |
| `succeeded` | Live run finished successfully |
| `failed` | Live run failed; `result.error` is a code, `result.detail` explains |
| `blocked` | Refused before doing anything (missing display, policy) |
| `timeout` | Exceeded `task_timeout_seconds`; process killed |
| `cancelled` | Cancelled by request |
| `interrupted` | Worker restarted mid-task; the run did not finish cleanly |

`interrupted` exists so a task cannot be silently reported as running forever
after a crash or a `./bin/stop-spine.sh` during a live run.

A live-run failure is specific and actionable:

```json
{"status": "failed",
 "error": "agent_s_not_installed",
 "detail": "gui-agents not found (looked in ~/.local/agent-s-venv/bin and PATH). Install with: python3 -m venv ~/.local/agent-s-venv && ~/.local/agent-s-venv/bin/pip install gui-agents"}
```

---

## Approval tokens

Tokens authorise exactly one privileged action. They live in
`policy/approvals.json` (mode `0600`, git-ignored, created on first use).

```
./bin/issue-approval.sh agent_s_gui_task "exact instruction"   # -> token on stdout
./bin/issue-approval.sh openrouter_paid  "anthropic/claude-sonnet-4.5"
./bin/issue-approval.sh --list | --revoke <token> | --prune
./bin/issue-approval.sh --ttl 60 --max-uses 3 --note "why"
```

| Action | Scope is | Sent as |
| --- | --- | --- |
| `agent_s_gui_task` | the exact instruction | body `approval_token` |
| `openrouter_paid` | the model id | body `dai_approval_token` or header `X-DAI-Approval-Token` |

Rules the store enforces:

* An **empty scope is rejected.** A token that may be used for anything has to
  say `"*"` out loud.
* Scope matching is exact (or `*`). A token for one instruction will not
  authorise a differently-worded one.
* Tokens expire (`ttl_seconds`, default 3600; `--ttl 0` for no expiry).
* Tokens have a use counter (`max_uses`, default 1) and are spent **atomically
  at admission**, so two concurrent requests cannot both use a single-use token.
* Listing masks tokens (`E6W0…poHv`). The full value is printed once, at issue.

See `policy/approvals.example.json` for the record shape and
`docs/OPERATIONS.md` for the operating procedure.

---

## Status codes

| Code | Used for |
| --- | --- |
| 200 | Successful GET, and cancel of an already-finished task |
| 202 | Task accepted (async), cancel accepted for a running task |
| 400 | Malformed request — bad JSON, missing field, invalid id, traversal |
| 401 | Bearer token required and missing/wrong |
| 403 | Policy or approval refusal |
| 404 | Unknown route or unknown task |
| 413 | Body over the configured limit |
| 429 | Queue full |
| 501 | Endpoint deliberately not implemented |
| 502 | Upstream provider returned an unusable response |
| 503 | Service not ready, or every candidate failed |

`503` from `/ready` is normal on a fresh clone and is not a fault.

---

## Error codes

Two distinct namespaces. Both are stable strings safe to branch on, but they
appear in different places and mean different things.

### HTTP `error` codes

Returned in the top-level error envelope with a non-2xx status.

**Shared (both services):** `invalid_json`, `unauthorized`, `not_found`,
`method_not_allowed`, `payload_too_large`, `body_required`,
`bad_content_length`, `length_required`, `internal_error`

**model-router:** `messages_required`, `prompt_required`, `invalid_model`,
`paid_models_disabled`, `no_providers_ready`, `requested_model_unavailable`,
`all_candidates_failed`, `not_implemented`

**agent-s-worker:** `instruction_required`, `instruction_too_long`,
`invalid_max_steps`, `invalid_task_id`, `agent_s_disabled_in_policy`,
`live_desktop_forbidden_until_repolicy`, `queue_full`, `artifact_gone`

**Approval store (raised by either service):** `missing_approval_token`,
`unknown_approval_token`, `approval_expired`, `approval_exhausted`,
`approval_scope_mismatch`, `approval_action_mismatch`

### Task result codes

Appear in `result.error` on a task record with status `failed`, `blocked`,
`timeout`, `cancelled` or `interrupted`. The HTTP response was already `202` —
the task was *accepted*, then could not complete. Poll the task to see these.

| Code | Meaning |
| --- | --- |
| `agent_s_not_installed` | `gui-agents` not found in the venv or PATH |
| `agent_s_launch_failed` | The binary was found but could not be started |
| `agent_s_nonzero_exit` | `agent_s` exited non-zero; log tail is in `detail` |
| `agent_s_timeout` | Exceeded `task_timeout_seconds`; process killed |
| `display_unavailable` | Agent display is not answering and Xvfb is off |
| `model_router_not_ready` | Router unreachable or not `ready_for_chat` |
| `agent_s_disabled_in_policy` | `agent_s.enabled` is false |
| `live_desktop_forbidden_until_repolicy` | `bind_owner_live_desktop` is true |
| `cancelled_before_start` | Cancelled while still queued |
| `cancelled_by_owner` | Cancelled during the run |
| `queue_full` | Queue filled between admission and dispatch |
| `worker_shutdown` | Worker stopped while the task was running |
| `worker_exception` | Unexpected internal error |

`worker_exception` is the only one that should never appear in normal
operation; if it does, the log tail in `logs/agent-s-worker.log` has the
traceback.

---

## Client examples

### Python (stdlib only)

```python
import json, urllib.request

def chat(prompt, model="dai/auto"):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11435/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        doc = json.loads(resp.read().decode())
    routed = doc.get("dai_routed") or {}
    print(f"[{routed.get('provider')}/{routed.get('model')} · {routed.get('attempts')} attempt(s)]")
    return doc["choices"][0]["message"]["content"]
```

`skills/agent-s-delegate/scripts/delegate_task.py` is a worked example of the
worker API, including polling to a terminal status.

### curl

```bash
R=http://127.0.0.1:11435
W=http://127.0.0.1:8765

curl -s $R/v1/models | python3 -m json.tool
curl -s -X POST $R/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"dai/auto","messages":[{"role":"user","content":"Say hi"}]}'
curl -sN -X POST $R/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"dai/auto","stream":true,"messages":[{"role":"user","content":"Count to three"}]}'

ID=$(curl -s -X POST $W/v1/tasks -H 'Content-Type: application/json' \
  -d '{"instruction":"Open Chromium and go to example.com","dry_run":true}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s $W/v1/tasks/$ID | python3 -m json.tool
```

Or use the wrapper: `./bin/dai chat "Say hi"`, `./bin/dai models`,
`./bin/dai plan`.

---

## Related

* `docs/ARCHITECTURE.md` — why the spine is shaped this way
* `docs/CONFIG.md` — every setting and the precedence rules
* `docs/OPERATIONS.md` — runbook: start, diagnose, approve, recover
* `KEYS.md` — getting provider keys
