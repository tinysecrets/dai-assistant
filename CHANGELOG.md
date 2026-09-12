# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Changes a client can observe are marked **Breaking** where they alter a
documented contract.

---

## [Unreleased]

A correctness and quality overhaul of the whole spine. The initial commit
described the intended design; this makes the implementation match it. Every
item below was verified against a running service or a test, not assumed.

### Added

**Shared library** — `lib/dai/`, stdlib only, used by both services:

* `env.py` — `.env` parsing and typed accessors (`env_str`/`int`/`float`/`bool`).
  Real environment variables win over the file.
* `redact.py` — value-based secret redaction for logs, error bodies and task
  records.
* `jsonio.py` — atomic JSON store with an `RLock`, plus cached config files that
  degrade to a reported error instead of raising.
* `httpclient.py` — request helpers and an SSE line iterator; error bodies are
  redacted before they surface.
* `httpserver.py` — shared `JsonHandler`, `ServiceInfo`, error envelope,
  bounded bodies, and `serve_forever` with clean shutdown.
* `approvals.py` — one-shot approval tokens with TTL, exact scope matching, use
  counters, and atomic spend; plus a `python3 -m lib.dai.approvals` CLI.
* `routing.py` — provider specs, candidate planning, group-based rotation,
  failure classification and cooldown policy.
* `stats.py` — per-candidate counters.

**Services**

* `model-router`: streaming (`stream: true`) with rotation before the first
  byte; an SSE `dai_routed` comment line before content and a usage trailer
  emitted *before* `[DONE]`; provider-wide cooldowns on auth/quota failure that
  take effect mid-request; a paid-OpenRouter approval gate; `DAI_STRICT_MODELS`
  to forbid silent model substitution; `enabled_providers` honoured from policy.
* `model-router`: new read-only status endpoints `/v1/status/config`,
  `/v1/status/keys`, `/v1/status/cooldowns`, `/v1/status/stats`,
  `/v1/status/plan`, and `/v1/status/reset`.
* Both services: `--check`, `--print-config` and `--version`.
* `agent-s-worker`: real live execution of `agent_s`, cancellation, per-task
  timeout with process kill, bounded queue, task persistence and pruning,
  artifact serving with traversal refusal, and an `interrupted` status so a task
  cannot claim to be running after a restart.

**Scripts**

* `bin/lib.sh` — shared helpers: `.env` access without sourcing, coloured
  output with `NO_COLOR`, HTTP probes with timeouts, JSON field extraction,
  port checks, and pid-file handling that validates the recorded process's
  command line before killing it.
* `bin/dai` — one entry point (`doctor`, `up`, `down`, `status`, `logs`,
  `smoke`, `test`, `keys`, `approve`, `tokens`, `skills`, `hatch`, `models`,
  `chat`, `plan`, `config`, `version`).
* `bin/smoke.sh` — end-to-end self-test: static checks, the suite, and live
  checks against a running spine. Only attempts real inference when the router
  reports `ready_for_chat`; its GUI task is always a dry run.
* `bin/doctor.sh` rewritten with `--json` and `--quiet`, five sections, and
  next-step hints. It no longer fails on a fresh clone.

**Project files** — `Makefile`, `pyproject.toml`, `LICENSE` (MIT),
`SECURITY.md`, `CONTRIBUTING.md`, this changelog, and CI
(`.github/workflows/ci.yml`) covering three Python versions plus shellcheck,
ruff and repo hygiene.

**Docs** — `docs/API.md` (both services advertise it from `/health`; it did not
exist), `docs/CONFIG.md` (every setting and the precedence rules),
`docs/OPERATIONS.md` (runbook). `policy/approvals.example.json` documents the
token record shape.

**Tests** — 441 tests, none needing keys, network or a display:

* unit: `redact`, `env`, `jsonio`, `approvals`, `routing`
* HTTP integration against a stub provider: `router_http`, `worker_http`
* scripts run for real: `test_shell`, `test_skill_scripts`
* drift detection: `test_repo_consistency`

### Changed

* **`policy/sovereign.json` is version 3 and ships safe defaults.**
  `agent_s.dry_run_default` is now `true` (was `false`, meaning every task was
  live unless it asked otherwise). Adds `max_steps_hard_cap`, `geometry`,
  `venv_path`, an `approvals` section, and in-file `notes` so the rationale
  travels with the setting.
* **`.env.example` was rewritten.** Previously almost none of its variables
  were read by anything. Every variable in it is now live, documented with its
  default, and enforced against the code by a test.
* **Safety keys resolve from the policy file only.** `enabled`,
  `dry_run_default`, `bind_owner_live_desktop`, `require_approval_token` and
  `max_steps_hard_cap` cannot be set by an environment variable. Everything
  else is `.env` → policy → built-in default. The worker publishes the list at
  `/v1/settings` → `safety_keys_policy_only`.
* **`--check` separates configuration faults from missing optional
  dependencies.** `problems` still fail the check; a missing `agent_s` binary or
  an absent X display is now a `warning`, so `--check` works as a CI gate and on
  a fresh clone. `doctor.sh` reads both lists instead of re-classifying strings.
* **`DAI_QUIET_LOGS` is honoured by both services.** Previously only the router
  read it, so the worker logged every request regardless.
* **Error messages preserve their original casing.** The router previously
  lowercased upstream error text for classification and then displayed the
  lowercased copy, so `attempts[].error` was all-lowercase and `[REDACTED]`
  became `[redacted]`. Classification still matches case-insensitively.
* **Round-robin rotates within a provider group.** Previously rotation could
  move an explicitly requested model out of the first slot, and the vision boost
  did not survive round-robin. Group order is preserved while members rotate.
* **`venv_path` supports `~`.** The worker expands it, so the policy file can
  say `~/.local/agent-s-venv` and `/v1/settings` reports the real path.
* **`skills/agent-s-delegate/scripts/delegate_task.py`** now has documented exit
  codes (0 success · 1 refused/unreachable · 2 task did not succeed · 3 wait
  timed out · 4 usage), reads the token from `DAI_APPROVAL_TOKEN` so it is not
  visible in `ps`, and adds `--worker`, `--quiet`, `--poll-interval` and
  `--cancel-on-timeout`.
* **`skills/agent-s-delegate/scripts/worker_health.py`** reports instead of
  raising, distinguishes a config error (exit 4) from an unhealthy worker
  (exit 1), redacts its output, and explains what blocks a live run.
* **`bin/` scripts** were rewritten on the shared library: strict mode, real
  `--help`, rejected unknown flags, and no sourcing of `.env`.
* `bin/install-vellum-skill.sh` validates `SKILL.md` frontmatter, supports
  `--link`, `--dest` and `--list`, replaces a previous copy cleanly, and stages
  into `skills-ready/` with the exact copy command when no workspace exists.
* `bin/hatch-vellum.sh` refuses to hatch without a ready inference path, hatches
  once instead of twice, and exports only the keys Vellum needs, only for the
  command that uses them.
* `bin/stop-spine.sh` is pid-file based. The previous `pkill -f` pattern could
  match the calling shell's own command line and kill it.
* `.gitignore` expanded (`.env.*`, pid files, build and cache dirs, editor and
  OS files) while keeping `!.env.example` shippable.

### Fixed

Blockers — the spine could not be used at all on a fresh clone:

* **`agent-s-worker` returned 500 on every `POST /v1/tasks`.** It loaded
  `policy/approvals.json` before checking whether the task was a dry run, so a
  missing (git-ignored, never-created) file crashed every submission.
* **`bin/issue-approval.sh` aborted with `FileNotFoundError`** for the same
  reason. It now creates `policy/approvals.json` at mode `0600` on first use,
  and its stdout is exactly the token so `TOKEN=$(...)` works.
* **`model-router` dropped the connection on malformed JSON.** An unhandled
  `JSONDecodeError` reset the socket; it is now a clean
  `400 {"error": "invalid_json"}`.

Correctness:

* **`redact()` leaked secret prefixes.** Four regex bugs: a `\b` boundary before
  a label inside a compound word, bare `token` plus whitespace matching
  ordinary prose, a `\b` before `-----BEGIN`, and a stem-only replacement.
* **Substring matching on `"rate"` caused false rate-limit cooldowns**, taking
  healthy models out of rotation. Classification is now by status code plus
  specific patterns.
* **Policy `timeout_seconds` was never read.**
* **`/v1/completions` was proxied to the chat endpoint**, and **`stream: true`
  was forced to `false`**.
* **Provider cooldowns set mid-request had no effect on that request.** The
  candidate snapshot was taken before the cooldown was recorded, so a model
  whose provider had just failed auth was still tried. Now re-checked at the top
  of each attempt and annotated `"skipped": "cooled_during_request"`.
* **An SSE usage trailer emitted after `[DONE]`** was silently discarded by most
  client libraries. The router now intercepts the upstream `[DONE]`.
* **`queue.Queue(maxsize=0)` is unbounded**, so `DAI_AGENT_S_MAX_QUEUE` was not
  a bound. Now clamped to ≥ 1.
* **`env_bool` treated an empty string as false**, making the documented
  "empty means unset, use the default" behaviour unreachable.
* **Approval tokens had no expiry, accepted an empty scope as a wildcard, and
  spent non-atomically**, so two concurrent requests could both use a
  single-use token.
* **A deadlock in `ApprovalStore.consume`**: a non-reentrant `Lock` around a
  nested update. Now an `RLock` (also in `jsonio` and `stats`).
* **`ensure_display` could raise** and take the worker down; display probing is
  now bounded and reported.
* **Unbounded thread creation** per request.
* **`bin/install-vellum-skill.sh` wrote `003c`/`003e`** — HTML entity typos that
  produced garbage instead of a redirection.
* **A successful live GUI task exited 2** from `delegate_task.py`, because the
  success test was `status.endswith("complete")` and `succeeded` does not end
  with "complete".
* **`grounding_height` shipped as 1080 in `.env.example` while policy and the
  display geometry said 800.** Since `.env` overrides policy for host knobs,
  this silently misaligned GUI grounding — clicks would land in the wrong place.
  Those variables are now left commented so the policy values apply.

Hygiene:

* Missing trailing newlines, hardcoded absolute paths, dead code, a
  `docs/API.md` that both services advertised from `/health` but which did not
  exist, and an incomplete `README.md`.

### Notes for anyone upgrading

* If you relied on tasks being live by default, set
  `agent_s.dry_run_default` to `false` in `policy/sovereign.json` **and** issue
  an approval token per instruction. That is now two deliberate acts, which is
  the point.
* If you set `DAI_AGENT_S_ENABLED`, `DAI_AGENT_S_DRY_RUN` or
  `DAI_AGENT_S_MAX_STEPS_CAP` in `.env`, they are ignored. Move them to the
  policy file.
* Response shapes are unchanged apart from additions (`dai_routed`,
  `/v1/status/*`, `warnings` in `--check`). Ports, paths and existing fields are
  as they were.

---

## [1.0.0] - initial commit

The design as first committed: a model-router with free-tier rotation, an
agent-s-worker with dry-run gating, two Vellum skills, a policy file, and
operational scripts. Described the intended behaviour; the implementation gaps
are itemised under `Unreleased` above.

[Unreleased]: https://github.com/tinysecrets/dai-assistant/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/tinysecrets/dai-assistant/releases/tag/v1.0.0
