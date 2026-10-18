# DAI Unified Branch Audit — 2026-10-10

## Branch and source inventory

- **Consolidation branch:** `audit/dai-unified`
- **Pull request:** [#5 — DAI unified branch: consolidate work and audit safety](https://github.com/tinysecrets/dai-assistant/pull/5)
- **Base:** `main`; branch is ahead and not behind it. No merge to `main` has been made.
- **Second source reviewed:** private `tinysecrets/dai`, main at `eb2b5d2`; unified code/test state verified at audit commit `1237bc0a3cc828206768b06826ea2cf6a5545f89`.
- The repositories' tracked-file inventories were compared. The private repository's only private-only tracked path is `speak`, a two-line output note, which was not imported as executable code. The unified branch adds eleven files for system mapping, mobile/voice support, Omni roles, tests, and this audit.
- Shared file differences are intentional in many cases: the unified branch retains current Agent-S APIs, system policy, G8/S22 integration, and path-hardening rather than blindly overwriting them with another checkout's versions. It is **consolidated for review, not a byte-for-byte mirror** of both Git repositories.

## Changes reconciled into the branch

- Updated free-model pool and backup-file ignore rules from the private repository.
- Strict Gemini wake gate: explicit leading summons only; ordinary mentions of “day/date” do not activate the assistant. Regression tests cover both explicit activation and ambient speech.
- Voice CLI and end-to-end smoke path reconciled to the current voice-bridge TTS profile. The TTS smoke test reads WAV output from a file, not a shell variable; URL parsing is quoted safely.
- Makefile checks use the optional voice virtual environment when available and check the live health endpoint for the running voice bridge.
- DAI paths resolve through physical paths for symlinked checkouts; policy-path precedence is covered by tests.
- Omni modules that used to be absolute-path symlinks into a local `.hermes` directory are now ordinary repository source files. Memory state directories/files are owner-only.
- A read-only Omni memory statistics endpoint is exposed by the dashboard. Its stats reader does not instantiate the mutating memory-store constructor, create missing state, or change file permissions; regression tests cover those behaviors.

## Safety and policy findings

| Severity | Finding | Assessment |
|---|---|---|
| **High** | Omni used to be represented by machine-specific absolute symlinks. | The source is now tracked and portable. |
| **High** | The original healer could attempt live fixes by default, and delegation could start OpenCode without an approval adapter. | Healing is now dry-run-only. Delegation records intent but starts no subprocess. Omni daemon start/stop/restart/upgrade commands fail closed until proper service-control and approval integration exists. |
| **Resolved with limitation** | Private-data routing was previously declarative only. | `private_data_must_stay_local=true` now blocks recognizable credential/private-key patterns and image payloads from cloud routes; explicit cloud pins return 403 and auto routes are local-only, failing closed when no local candidate exists. This is a targeted tripwire, not a complete PII classifier: arbitrary confidential prose may not be recognized. Use an explicitly local model for private personal details. |
| **Resolved 2026-10-17** | `inference.free_cloud_only=false` and paid routes were not blocked globally. | The policy now enables `free_cloud_only=true`; route planning only allows FreeLLMAPI, OpenRouter `:free` (with Google-branded IDs excluded), and local Ollama. The HTTP route returns 403 for paid OpenRouter models regardless of `paid_enabled` or approval tokens. Regression tests cover both checks. |
| **Medium** | `bin/dai-gemini.py` still recognizes fixed PulseAudio/PipeWire source names for the S22 and G8. | Those names may change after a reconnect/reset. Runtime source discovery and a real desktop-session microphone test remain required. The remote shell session did not have a reachable PulseAudio/PipeWire server, so this audit did not verify the active source. |
| **Medium** | Current device/registry declarations do not prove live device readiness. | Actual ADB detection for the S22, G8, and A16, Lubuntu peer health, Deskflow, PipeWire default input, and running services still require on-host verification. |

## Deliberate non-imports

- The two-line `speak` note in `tinysecrets/dai` was not copied because it is not an executable script or a maintainable interface.
- The private repository's additional automatic deletion of old files under `state/omni/runtime` was not imported. Deleting runtime artifacts should not be added without a bounded retention policy, a clear ownership boundary, and tests.
- The private helper's implicit fallback to a generated runtime policy was not imported. `policy/sovereign.json` remains the tracked policy source of truth; alternate paths must be explicit rather than silently switching policy files.
- The unified Gemini microphone mapping retains the G8/S22 integration but has not yet been made fully dynamic. This is a documented follow-up, not a verified working device connection.

## Verification — tested code/tree commit `1237bc0a3cc828206768b06826ea2cf6a5545f89`

- `python3 -m compileall -q lib services skills tests` — passed.
- `python3 -m unittest discover -s tests -t . -v` — **516 tests passed**, 0 failures on the Debian checkout.
- Ruff lint and format checks — passed.
- Shell syntax checks (`bash -n`) — passed.
- GitHub Actions — **passed**: Python 3.9, 3.11, and 3.12 test/smoke/clean-checkout doctor jobs; Ruff; ShellCheck; repository hygiene.

These checks run against a clean clone; they do **not** prove that the currently installed Debian/Lubuntu services or Android devices are updated or healthy.

## Release gate

1. Keep this PR unmerged until the remaining privacy-policy enforcement and live microphone/source mapping questions are resolved or explicitly accepted.
2. On Debian and Lubuntu, verify the installed checkout/commit, `dai doctor`, `dai unify`, router/worker/voice health, and an approval-gated dry-run.
3. Verify the S22/G8/A16 by live ADB discovery and confirm the actual PipeWire input in the interactive desktop session.
4. Refresh provider catalog evidence before asserting every model in the rotation pool is currently available and free.
5. Removing old device identifiers from earlier Git history, if desired, requires a separate planned history rewrite; sanitizing present-day files does not erase old commits.

**Audit status: 516 tests and the full CI matrix passed on the verified code/test tree. This report correction is documentation-only. Production readiness is still conditional on the release-gate items above.**
