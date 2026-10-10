# DAI Unified Branch Audit — 2026-10-10

## Scope and provenance

- Audit branch: `audit/dai-unified` in `tinysecrets/dai-assistant`.
- Branch was created from `arena/01a0c826-dai-assistant`, which was **33 commits ahead of `main`** and not behind it. This consolidates the existing `dai-assistant` branches without rewriting history.
- Ported selected newer changes from the separate private `tinysecrets/dai` repository: latest free-model pool additions, local-backup ignore rule, and the strict Gemini wake-word gate with regression tests.
- **Not yet a complete cross-repository merge.** The private `dai` repository still has divergent versions of overlapping files and a `bin/dai-omni` entry point that imports `lib.dai.omni.*` modules not found at the expected paths. Do not claim the two repositories are fully consolidated until those differences are inventoried and reconciled.

## Findings

| Severity | Finding | State |
|---|---|---|
| High | The previous `SYSTEM_MAP.md` and integration registry contained stale device roles and private device/network identifiers. | Sanitized in this branch; older public commits still retain historical copies. |
| High | Live readiness is not proven by registry entries. Phone, G8, A16, Lubuntu, PipeWire, and service health must be checked on the actual hosts. | Explicitly documented; runtime verification still pending. |
| High | `policy/sovereign.json` keeps `dry_run_default=true` and `require_approval_token=true`, but also sets `private_data_must_stay_local=false` and `free_cloud_only=false`. Those fields may conflict with a strict privacy/free-first policy unless the router enforces more specific rules. | Needs policy-owner decision and code-path verification; not silently changed here. |
| Medium | The private repository's `dai-omni` CLI appears incomplete at its current expected import paths; several commands can return “module not loaded.” | Not copied into the unified branch as a working feature. |
| Medium | The model pool lists a discovery timestamp and verified count that must be refreshed against the live provider catalog before treating every model as currently available or free. | Newer free-model entries merged; live provider verification still pending. |
| Medium | The current CI workflow triggers on `main` pushes and pull requests, not on arbitrary branch pushes. | Open a PR to make the CI gate run; do not merge until checks pass. |

## Safety checks retained

- Agent-S defaults to dry-run.
- Live GUI execution requires a single-use approval token scoped to the exact instruction.
- The model router is not an authorization system; model responses cannot weaken approval policy.
- Private endpoint addresses and device serials are not present in the sanitized system map or integration registry.
- Local runtime files, secrets, approval tokens, logs, and state remain ignored.

## Verification still required

1. CI: compile, service config checks, shell syntax, unit/integration suite, smoke checks, doctor, ShellCheck, Ruff, and hygiene.
2. On Debian and Lubuntu: verify the installed checkout/commit, `dai doctor`, `dai unify`, service health, router readiness, and the approval-gated dry-run.
3. Confirm live ADB discovery for the S22, G8, and A16; confirm the actual PipeWire default input and Deskflow peer.
4. Reconcile the private `dai` repository's divergent files and unique components into this branch before declaring cross-repository unification complete.
5. If removal of historical device identifiers from public Git history is desired, handle that separately with an explicit history-rewrite and coordination plan. Sanitizing current files does not erase old commits.

**Audit status: IN PROGRESS — branch consolidation and static hardening done; CI and live-host verification not yet passed.**
