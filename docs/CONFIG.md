# Debian AI Assistant configuration

This document is the configuration contract for the Debian AI spine.

## Source of truth

Configuration is intentionally split by responsibility:

1. `.env` — local secrets and runtime overrides. Never commit it.
2. `config/rotation-pool.json` — provider model pools and rotation order.
3. `config/free-models.json` — catalog/snapshot data used for discovery and task hints.
4. `policy/sovereign.json` — safety and inference policy, including the paid-model gate.
5. Service defaults — used only when an environment variable is absent.

Environment variables already exported by the process take precedence over values loaded from `.env`.

## Provider routing

`dai/auto` is the public virtual text model. The router builds candidates from:

1. OpenRouter `:free` pool
2. Groq pool
3. Cerebras configured model
4. Ollama Cloud pool
5. local Ollama pool as last resort

`dai/vision-auto` is a vision-only virtual model and uses the `openrouter_free_vision` pool. Text-only models are not substituted into a vision request.

The rotation pool is the model-list source of truth; model IDs should not be duplicated in `.env`.

## Provider keys

- `OPENROUTER_API_KEY` enables OpenRouter.
- `GROQ_API_KEY` enables Groq.
- `CEREBRAS_API_KEY` enables Cerebras.
- `OLLAMA_API_KEY` enables Ollama Cloud.
- Local Ollama is keyless and is probed at its configured local endpoint.

Keys are presence-checked by the doctor and are never intended to be logged.

## Router controls

- `DAI_ROUTER_HOST` / `DAI_ROUTER_PORT` — listener, default `127.0.0.1:11435`.
- `DAI_COOLDOWN_SECONDS` — normal model rate-limit cooldown, default `90`.
- `DAI_SHORT_COOLDOWN_SECONDS` — short failure cooldown, default `30`.
- `DAI_PROVIDER_COOLDOWN_SECONDS` — provider-wide cooldown for authentication/quota failures, default `600`.
- `DAI_REQUEST_TIMEOUT` — normal upstream request timeout, default `120` seconds.
- `DAI_STREAM_TIMEOUT` — streaming timeout budget, default `600` seconds.
- `DAI_STRICT_MODELS` — when enabled, an explicit unavailable model fails instead of falling through to rotation.
- `DAI_ROUTER_TOKEN` — optional bearer token for router routes other than `/health` when authentication is enabled.
- `DAI_MAX_BODY_BYTES` — request body ceiling; default `8388608` bytes.
- `DAI_QUIET_LOGS` — suppress routine access logging when enabled.

Provider base URLs may be overridden with `DAI_OPENROUTER_BASE_URL`, `DAI_GROQ_BASE_URL`, `DAI_CEREBRAS_BASE_URL`, `DAI_OLLAMA_CLOUD_BASE_URL`, and `DAI_OLLAMA_LOCAL_BASE_URL`.

## Agent S

Agent S should normally use the router rather than contacting a provider directly:

```text
AGENT_S_PROVIDER=openai
AGENT_S_MODEL=dai/auto
AGENT_S_GROUND_PROVIDER=openai
AGENT_S_GROUND_MODEL=dai/vision-auto
```

`DAI_MODEL_ROUTER` points the worker at the router. `DAI_AGENT_S_CONCURRENCY`, `DAI_AGENT_S_MAX_QUEUE`, and `DAI_AGENT_S_MAX_TASKS` constrain worker load.

Live GUI settings are deliberately separate from the model-router policy. Keep the router in the path so model rotation, cooldowns, redaction, and the paid-model gate remain centralized.

## Security

Keep the router on `127.0.0.1` unless there is a concrete reason to expose it. If it is exposed beyond localhost, set `DAI_ROUTER_TOKEN` and enforce the bearer token on all non-health routes.

Never put API keys, `.env`, SSH private keys, password dumps, or browser cookies into model requests.
