# Security

This repo drives GUI automation and spends money on inference, so its security
posture is about **containment and explicit consent**, not just avoiding leaks.

## Reporting a vulnerability

Please report privately, not in a public issue.

* GitHub: use the [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
  feature on this repository if it is enabled.
* Otherwise: contact the maintainer (`tinysecrets`) directly, and encrypt with
  the repo signing key if you have one.

Please include what you ran, what you expected, what happened, and whether the
issue needs a secret rotated. We aim to acknowledge within 72 hours.

**Rotate any key you believe was exposed.** Do not wait for a fix. Keys live
only in `.env` and `policy/approvals.json`, both mode `0600` and git-ignored.

---

## Threat model

What this software is designed to prevent:

| Threat | Mitigation |
| --- | --- |
| A prompt or model output causing unattended GUI control | Dry-run by default; live runs need a policy change **and** a one-shot token scoped to the exact instruction |
| An agent driving the desktop you are sitting at | A dedicated Xvfb display (`:99`); `bind_owner_live_desktop` is a safety key that no env var can set |
| A browser page driving automation or spending budget | No CORS headers on either service, and none will be added |
| Network exposure | Both services bind `127.0.0.1`; optional bearer auth for anything else |
| Secrets reaching logs, error bodies, or task records | Every secret-looking env value is registered with a redactor at startup and replaced with `[REDACTED]` on the way out |
| A secret leaking through a chat transcript | Redaction is by *value*, so a key matching no known pattern is still scrubbed |
| One bad provider key breaking all inference | Provider-wide cooldowns on auth/quota failure, effective immediately |
| A burst of requests spawning unbounded agent processes | Serialised tasks (`DAI_AGENT_S_CONCURRENCY=1`) and a bounded queue |
| An approval token being reused or guessed | Single-use by default, TTL-bounded, spent atomically at admission, masked in listings |
| Path traversal through the artifact endpoint | Only basenames already recorded in a task are served; traversal and encoded separators are rejected |
| A recycled pid causing us to kill an unrelated process | Pid files are validated against the recorded process's command line before use |
| A corrupt config file crashing a service | Policy and pool files degrade to safe built-in defaults and report `config_errors` |

Out of scope: anything requiring local code execution as the same user. If an
attacker can run code as you, they can read `.env` directly.

## Design rules

These are load-bearing. Changing one is a security decision, not a refactor.

1. **`.env` is parsed, never sourced.** Sourcing exports every secret into
   every child process and breaks on values containing spaces or quotes. The
   parser is `lib/dai/env.py`.
2. **Safety keys resolve from the policy file only.** `enabled`,
   `dry_run_default`, `bind_owner_live_desktop`, `require_approval_token` and
   `max_steps_hard_cap` cannot be changed by an environment variable. The
   worker publishes the list at `/v1/settings` → `safety_keys_policy_only`.
3. **No CORS.** Both services are localhost-only by intent.
4. **Empty approval scopes are rejected.** A token that authorises anything has
   to say `"*"` explicitly.
5. **Tokens are spent even when the task then fails.** A token authorises one
   *attempt*, so a failure cannot be retried by replaying it.
6. **Failure is explicit.** A task that cannot run reports a specific reason
   (`agent_s_not_installed`, `display_unavailable`, `model_router_not_ready`)
   rather than silently doing nothing.
7. **`/health` is unauthenticated on purpose**, so monitoring and
   `./bin/doctor.sh` work before any key exists. It exposes no secret.

## Secrets handling

| File | Mode | Committed | Contains |
| --- | --- | --- | --- |
| `.env` | `0600` | never | provider keys, host overrides |
| `policy/approvals.json` | `0600` | never | live approval tokens |
| `state/agent-s-tasks/` | `0600` files | never | task records, logs, screenshots |
| `.env.example` | `0644` | yes | empty placeholders only |
| `policy/approvals.example.json` | `0644` | yes | a fake record, for shape only |

`.gitignore` covers all of the above, and `!.env.example` keeps the template
shippable. A CI job greps the tree for key-shaped strings and for private key
headers.

Artifacts and task records can contain screenshots of whatever was on the agent
display. They are stored mode `0600` under `state/`, are never committed, and
are pruned past `DAI_AGENT_S_MAX_TASKS`.

## Verifying the posture

```bash
./bin/doctor.sh                        # files, config, keys, services, GUI deps
./bin/dai config                       # resolved config, secrets removed
curl -s localhost:8765/v1/settings     # includes safety_keys_policy_only
curl -s localhost:11435/v1/status/keys # booleans only, never a value
./bin/dai test                         # includes the safety-key and redaction tests
```

Tests that specifically enforce the rules above:

* `tests/test_redact.py` — redaction, including secret prefixes and patterns
* `tests/test_approvals.py` — TTL, scope, atomic single-use spend
* `tests/test_worker_http.py` — approval gating, policy safety, env cannot
  weaken a safety flag, artifact traversal refusal
* `tests/test_router_http.py` — paid-model gate, auth cooldowns, redacted errors
* `tests/test_repo_consistency.py` — the shipped policy stays safe, no committed
  secrets, documented error codes are real

## Known limitations

Stated plainly, because a limitation nobody wrote down becomes an incident:

* **No rate limiting of its own.** The router forwards to providers and cools
  down on their 429s; it does not police a local caller's request rate.
* **No transport encryption.** Localhost HTTP by design. Put a proxy in front
  if you need more, and set the bearer tokens.
* **`AGENT_S_PROVIDER` can bypass the router.** Any value other than `openai`
  talks to that provider directly, losing cooldowns, redaction and the paid gate.
  The shipped configuration keeps it at `openai`.
* **A live GUI agent can do damage inside its display.** Containment is the
  dedicated Xvfb display plus bounded steps plus a timeout — not sandboxing of
  the applications it drives.
* **gui-agents is a third-party dependency** and is not vendored or audited
  here. It is required only for live runs; dry-run needs nothing.
