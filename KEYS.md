# Keys — what to put where

Everything waits on `/home/justin/workspace/debian-ai-assistant/.env`.

```bash
cd ~/workspace/debian-ai-assistant
cp -n .env.example .env
nano .env   # or: ./bin/import-keys.sh
./bin/doctor.sh
./bin/start-spine.sh
```

## Minimum to chat (pick one)

| Variable | Where you already might have it |
|----------|----------------------------------|
| `OPENROUTER_API_KEY` | Hermes / Genie / Gobi / OpenClaw (~$10 + free models) |
| `GROQ_API_KEY` | Groq console |
| `OLLAMA_API_KEY` | ollama.com (Hermes uses ollama-cloud today) |
| `CEREBRAS_API_KEY` | Cerebras (Genie Sidekick) |

**Best start:** set `OPENROUTER_API_KEY` — rotator uses your Genie free top-model pool and spins on 429s.

## Optional later

- Agent S grounding URL / GUI packages — only when you enable live computer use
- Paid OpenRouter — keep off; use `./bin/issue-approval.sh openrouter_paid "..."` if needed

## Never

- Paste keys into Vellum chat
- Commit `.env`
- Put keys in Agent S worker env dumps
