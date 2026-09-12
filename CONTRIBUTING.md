# Contributing

Thank you. This repo has an unusual property worth understanding before you
change anything: **it has no dependencies and no build step.** That is a design
decision, not an oversight, and most contributions should preserve it.

---

## Ground rules

1. **Python stdlib only.** No new runtime dependency, ever. The spine has to run
   on a bare Debian `python3` with no install step and no network access. If you
   think you need a package, open an issue first — there is usually a stdlib
   path. Linters (`ruff`, `shellcheck`) are optional and dev-only.
2. **Python ≥ 3.9.** `from __future__ import annotations` is used so modern
   type syntax is fine; `match` statements and `X | Y` at *runtime* are not.
3. **Backward compatibility matters.** Vellum and the skill scripts may already
   consume these APIs. Keep file layout, service ports, endpoint paths, script
   names, policy structure and JSON response shapes stable. **Additive changes
   are welcome; removing or renaming a field is a breaking change** and needs a
   note in `CHANGELOG.md`.
4. **Safety keys stay policy-only.** `enabled`, `dry_run_default`,
   `bind_owner_live_desktop`, `require_approval_token`, `max_steps_hard_cap`.
   An environment variable must never be able to weaken one. See `SECURITY.md`.
5. **Nothing prints a secret.** Use the redactor. If you add a new secret-looking
   env var, make sure it is registered.
6. **No CORS.** Both services are localhost-only by intent.

## Getting started

```bash
git clone https://github.com/tinysecrets/dai-assistant.git
cd dai-assistant

make help          # every target, with descriptions
make lint          # compile + bash -n + service --check
make test          # the full suite
make doctor        # what is present, what is missing
```

No install step. `python3` and `bash` are enough.

Optional, for the same checks CI runs:

```bash
make install-dev   # ruff + shellcheck into .venv
source .venv/bin/activate
```

## Before you open a PR

```bash
make all           # lint + test + smoke — the same gate CI runs
```

Then check the pieces CI checks separately:

```bash
ruff check lib services skills tests      # if you installed it
shellcheck -x bin/*.sh bin/dai            # if you installed it
./bin/doctor.sh                           # must not fail on a clean checkout
```

`./bin/doctor.sh` should exit 0 on a fresh clone. Missing keys and a missing
`vendor/` are **warnings**, not failures — if your change makes doctor fail on a
clean checkout, that is the bug.

## How the tests are organised

| File | Covers |
| --- | --- |
| `tests/support.py` | Stub upstream provider, temporary spine, service fixtures, fake `agent_s`/`xset` |
| `tests/test_redact.py` | Redaction: prefixes, patterns, bearer tokens |
| `tests/test_env.py` | `.env` parsing and typed accessors |
| `tests/test_jsonio.py` | Atomic JSON store, cached files, locking |
| `tests/test_approvals.py` | Token TTL, scope, atomic single-use spend |
| `tests/test_routing.py` | Candidate planning, rotation, failure classification, cooldowns |
| `tests/test_router_http.py` | model-router over real HTTP, against the stub upstream |
| `tests/test_worker_http.py` | agent-s-worker over real HTTP, including live execution via a fake `agent_s` |
| `tests/test_shell.py` | `bin/` scripts: parsing, flags, helpers, lifecycle |
| `tests/test_skill_scripts.py` | Skill scripts, run for real against a temporary worker |
| `tests/test_repo_consistency.py` | Drift: `.env.example` ↔ code, docs ↔ routes, pool ↔ providers |

Run one module while iterating:

```bash
python3 -m unittest tests.test_routing -v
```

The HTTP tests start real servers on ephemeral ports and a stub provider, so
they need no keys and no network. Live GUI execution is tested with a fake
`agent_s` binary and a fake `xset` (see `tests/support.py`), which is why the
suite can assert on live-run behaviour without a display.

### The consistency tests are the interesting ones

`tests/test_repo_consistency.py` exists because this repo drifted badly once.
It fails when:

* `.env.example` documents a variable nothing reads, or code reads one that is
  undocumented
* a doc references a file or script that does not exist
* `docs/API.md` documents an error code no service can emit — **or** a service
  emits one the docs do not mention
* the rotation pool contains a non-`:free` OpenRouter model
* the shipped policy stops being safe
* a skill script calls an API that does not exist in `lib.dai`
* a text file loses its trailing newline, or a secret-shaped string is committed

If one of these fails, the test is usually right. Fix the drift rather than
deleting the assertion. If the check itself is wrong, say why in the PR.

## Adding things

### A new environment variable

1. Read it in the service (`env_str`/`env_int`/`env_bool`/`env_float`, or
   `self.setting(...)` for a worker knob).
2. Document it in `.env.example` with its default and what it does.
3. Document it in `docs/CONFIG.md`.
4. The consistency test enforces 1 ↔ 2 both ways; it will tell you if you skip
   one.

Never name it after a safety key.

### A new endpoint

1. Add the route in the service.
2. Document it in `docs/API.md`: method, path, request, response, and every
   error code it can return.
3. Add HTTP tests in `tests/test_router_http.py` or `tests/test_worker_http.py`.
4. If it returns a new error code, the consistency test will require it to
   appear in `docs/API.md`.

### A new provider

1. Add a `ProviderSpec` to `PROVIDERS` in `lib/dai/routing.py`.
2. Add its pool to `config/rotation-pool.json` (only `:free` ids for
   OpenRouter).
3. Add a section to `policy/sovereign.json` → `inference`.
4. Document the key in `KEYS.md`, `.env.example` and `docs/CONFIG.md`.
5. Add routing tests, then HTTP tests using the stub upstream.

### A new script in `bin/`

1. `source "$ROOT/bin/lib.sh"` — do not reimplement its helpers.
2. `set -euo pipefail`.
3. Use `dai_usage` for `--help` (it reads the header comment, so it cannot
   drift).
4. Reject unknown options with `dai_die`.
5. Never source `.env`; use `dai_env_get`.
6. Add a subcommand to `bin/dai` if it is part of the normal workflow.
7. `tests/test_shell.py` checks parsing, executability, `--help`, and unknown
   flags for every script automatically — a new `bin/*.sh` is covered on arrival.

## Style

* Comments explain **why**, not what. The interesting comments in this repo
  record a bug that was fixed or a decision that looks wrong but is not.
* Prefer a specific failure message over a generic one. "agent_s binary not
  found (venv: …). Install with: …" beats "failed".
* Distinguish *configuration faults* from *missing optional dependencies*.
  `--check` fails on the first and only warns on the second, so it works as a
  CI gate and on a fresh clone.
* Shell: quote expansions, anchor `pkill -f` patterns, validate a pid against
  its command line before killing it.

## Commit and PR

Small, focused commits. Imperative mood ("Fix cooldown casing in attempts").

A good PR description says what changed, why, and how you verified it. If you
touched a service, mention that `make all` passes.

If your change alters behaviour a client can observe, add a line under
`Unreleased` in `CHANGELOG.md`.

## Things that will get a PR closed

* A new runtime dependency
* A CORS header
* An env var that can weaken a safety key
* A secret in a test fixture that is not obviously fake
* Deleting a failing consistency assertion instead of fixing the drift
* Changing a documented JSON response shape without a migration note
