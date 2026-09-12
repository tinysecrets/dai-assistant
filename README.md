# Debian AI Assistant — FINISHED SPINE

One private Debian AI stack:

- **Vellum** = personal assistant (identity, memory, phone pairing)
- **Model router** = cloud-first free top-model rotation (OpenRouter / Groq / Ollama Cloud / Cerebras → local last)
- **Agent S worker** = GUI hands (dry-run until you enable)

Nothing here overwrites Hermes, LIYA, Genie, NOVA, etc.

## You only need to add keys

```bash
cd ~/workspace/debian-ai-assistant
./bin/import-keys.sh          # pulls from Genie/Hermes/Gobi if present
# or: cp .env.example .env && nano .env
./bin/doctor.sh
./bin/start-spine.sh
./bin/hatch-vellum.sh         # when ready_for_chat is true
./bin/install-vellum-skill.sh
```

See **KEYS.md**.

## Endpoints (after start-spine)

| Service | URL |
|---------|-----|
| Model router (OpenAI-compatible) | `http://127.0.0.1:11435/v1` |
| Auto model id | `dai/auto` |
| Agent S worker | `http://127.0.0.1:8765` |

Chat example:

```bash
curl -s http://127.0.0.1:11435/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"dai/auto","messages":[{"role":"user","content":"Say hi"}]}'
```

## Layout

| Path | Purpose |
|------|---------|
| `.env` / `.env.example` | Your keys |
| `config/rotation-pool.json` | Top free model rotation list (from Genie) |
| `config/free-models.json` | Full free catalog snapshot |
| `policy/sovereign.json` | Approvals + cloud-first policy |
| `skills/agent-s-delegate` | Vellum skill |
| `vendor/` | Upstream + companion sources (symlinks) |

## Status

Spine is configured and startable. Live Agent S GUI and Vellum hatch wait on your keys + optional package installs (tesseract/Xvfb) when you want computer use.
