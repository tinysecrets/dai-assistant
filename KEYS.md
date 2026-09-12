# Keys

Everything waits on one file: `.env` at the repo root, mode `0600`, never
committed.

```bash
cp -n .env.example .env && chmod 600 .env
nano .env
# or pull keys from other assistant installs on this machine:
./bin/import-keys.sh
```

Then:

```bash
./bin/doctor.sh        # reports which providers are configured
./bin/start-spine.sh   # restart to pick up the new key
```

You need **one** key to chat. More keys means a wider rotation and more
headroom when a provider rate-limits you.

---

## What each key unlocks

| Variable | Provider | Why you want it |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter | **Best start.** The largest free-tier pool, and what `config/rotation-pool.json` is built around. One key, many models, rotation on `429` |
| `GROQ_API_KEY` | Groq | Very fast, and a **separate rate-limit bucket** — it keeps working when OpenRouter is throttling you |
| `CEREBRAS_API_KEY` | Cerebras | Fast fallback, another independent bucket |
| `OLLAMA_API_KEY` | Ollama Cloud | Hosted Ollama models |
| *(none)* | local Ollama | No key needed. Last resort, used only when `http://127.0.0.1:11434` answers |

OpenRouter also reads two optional, non-secret values used for its leaderboard
attribution:

| Variable | Default |
| --- | --- |
| `OPENROUTER_HTTP_REFERER` | `https://localhost/debian-ai` |
| `OPENROUTER_APP_TITLE` | `Debian AI Assistant` |

### Where to get them

* **OpenRouter** — <https://openrouter.ai/keys>. Free-tier models work with no
  credit; some free tiers ask for a small balance or a social login.
* **Groq** — <https://console.groq.com/keys>
* **Cerebras** — <https://cloud.cerebras.ai/>
* **Ollama Cloud** — <https://ollama.com/>
* **Local Ollama** — `ollama serve`, then `ollama pull hermes3:8b`

If another assistant on this machine already has keys, `./bin/import-keys.sh`
finds them. It prints key *names* and where each came from, and never prints a
value. Use `--dry-run` first to see what it would do, and `--from-env` to also
accept keys already in your environment.

---

## Verifying

```bash
./bin/doctor.sh
curl -s localhost:11435/v1/status/keys | python3 -m json.tool
./bin/dai models
./bin/dai chat "Say hi"
```

`/v1/status/keys` returns booleans and a `keys_needed` list — never a value:

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

`keys_needed` is advisory: it lists what would widen your rotation. You do not
need any of them.

---

## No keys yet?

That is a working state, not a broken one.

* `/health` reports `ready_for_chat: false` and `candidates_available` counting
  whatever local Ollama offers.
* Chat answers `503 no_providers_ready` with a message telling you to add a key.
* **Dry-run GUI tasks still work** — they validate the request and record the
  command a live run would execute, without calling any model.
* `./bin/smoke.sh` skips the inference checks and says so.

Local Ollama alone is enough to chat with no key at all:

```bash
ollama serve &
ollama pull hermes3:8b
./bin/start-spine.sh
```

---

## Paid models

Paid OpenRouter models are **off by default**. Requesting one returns
`403 paid_models_disabled`.

Two ways to allow it, in increasing order of commitment:

```bash
# One request, one model — preferred
TOKEN=$(./bin/issue-approval.sh openrouter_paid "anthropic/claude-sonnet-4.5")
curl -s localhost:11435/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "X-DAI-Approval-Token: $TOKEN" \
  -d '{"model":"openrouter/anthropic/claude-sonnet-4.5",
       "messages":[{"role":"user","content":"hi"}]}'
```

Or set `inference.openrouter.paid_enabled` to `true` in
`policy/sovereign.json`, which allows paid models without a per-request token.
That is a standing decision — the token route is the one that keeps spending
explicit.

The token can also go in the request body as `dai_approval_token`. It is
single-use, scoped to that exact model id, and expires after an hour by
default. See `docs/API.md` and `docs/OPERATIONS.md`.

---

## Rotating and revoking

Keys are only ever in `.env`. To rotate one:

```bash
nano .env
./bin/dai down && ./bin/dai up
curl -X POST localhost:11435/v1/status/reset    # clear cooldowns from the bad key
```

That last step matters: an auth failure cools the **whole provider** for ten
minutes, so after fixing a key the provider may still be sitting out. Reset it.

If you think a key was exposed — in a log, a screenshot, a chat transcript —
**revoke it at the provider immediately.** Do not wait to confirm whether the
redaction caught it. Then check whether it ever left the machine:

```bash
git log -p --all -S 'sk-or-v1' -- .      # should find nothing
grep -rn "$YOUR_KEY_PREFIX" logs/ state/  # should find only [REDACTED]
```

---

## Never

* Paste a key into a chat with an assistant. It becomes part of a transcript you
  do not control, and may be sent to a cloud model.
* Commit `.env`. It is git-ignored, and CI greps the tree for key-shaped
  strings — but do not rely on either.
* Put a key in a shell command's arguments. It is visible to every local user
  in `ps`. Use the environment or a file. For approval tokens specifically, use
  `DAI_APPROVAL_TOKEN` rather than `--approval-token`.
* Export keys into the worker's environment for a live GUI run. The worker reads
  `.env` itself and passes the key to `agent_s` as a redacted argument; that is
  the path that keeps it out of logs and task records.
* Set `AGENT_S_PROVIDER` to anything but `openai`. That value means "through the
  router", which is what keeps redaction, cooldowns and the paid gate in force.
  Anything else talks to a provider directly and bypasses all three.

---

## Related

* `docs/CONFIG.md` — every setting and which layer wins
* `docs/API.md` — endpoints and error codes
* `docs/OPERATIONS.md` — diagnosing a rotation problem
* `SECURITY.md` — how secrets are handled
