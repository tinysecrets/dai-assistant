# READY checklist

- [x] Vellum source installed + `vellum` CLI linked
- [x] Agent-S source vendored (not live yet)
- [x] Model router with free top-model rotation + 429 cooldown
- [x] Multi-provider slots: OpenRouter, Groq, Ollama Cloud, Cerebras, local Ollama
- [x] Agent S worker dry-run API
- [x] Genie free-models catalog + rotation pool
- [x] Policy cloud-first; paid OpenRouter off
- [x] `.env.example`, import-keys, doctor, start/stop, hatch, skill install
- [x] Companion scrap map (Genie rotator used; sidekick/bigagi noted)
- [x] **You:** keys in `.env` (OPENROUTER + OLLAMA present; GROQ/CEREBRAS optional)
- [x] **You:** `./bin/start-spine.sh` && `./bin/hatch-vellum.sh` (spine up, ready_for_chat, vellum healthy + replying)
- [~] **Later:** enable Agent S live GUI + grounding — `gui-agents` installed in `~/.local/agent-s-venv`; live tasks on display :99 time out because grounding needs a vision model (OpenRouter free vision 429s, Ollama Cloud needs credits, local models are text-only). See `state/agent-s-live-attempt.log`; a local VLM on Ollama would fix it.

When doctor says keys present + spine up + `ready_for_chat: true`, you're live.
