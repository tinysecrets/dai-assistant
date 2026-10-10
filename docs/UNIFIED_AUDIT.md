# DAI Unified Branch Audit — 2026-10-10

## Branch and source inventory

- **Consolidation branch:** `audit/dai-unified`
- **Pull request:** [#5 — DAI unified branch: consolidate work and audit safety](https://github.com/tinysecrets/dai-assistant/pull/5)
- **Base:** `main`; branch is ahead and not behind it. No merge to `main` has been made.
- **Second source reviewed:** private `tinysecrets/dai`, main at `eb2b5d2`; unified branch code at audit commit `b90ed3c98755c559cf3193a74f3e62d4698fbb0a`.
- The repositories' tracked-file inventories were compared. The private repository's only private-only tracked path is `speak`, a two-line output note, which was not imported as executable code. The unified branch adds eleven files for system mapping, mobile/voice support, Omni roles, tests, and this audit.
- Shared file differences are intentional in many cases: the unified branch retains current Agent-S APIs, system policy, G8/S22 integration, and path-hardening rather than blindly overwriting them with another checkout's versions. It is **consolidated for review, not a byte-for-byte mirror** of both Git repositories.

## Changes reconciled into the branch

- Updated free-model pool and backup-file ignore rules from the private repository.
- Strict Gemini wake gate: explicit leading summons only; ordinary mentions of “day/date” do not activate the assistant. Regression tests cover both explicit activation and ambient speech.
- Voice CLI and end-to-end smoke path reconciled to the current voice-bridge TTS profile. The TTS smoke test reads WAV output from a file, not a shell variable; URL parsing is quoted safely.
- Makefile checks use the optional voice virtual environment when available and check the live health endpoint for the running voice bridge.
- DAI paths resolve through physical paths for symlinked checkouts; policy-path precedence is covered by tests.
- Omni modules that used to be absolute-path symlinks into a local `.hermes` directory are now ordinary repository source files. Memory state directories/files are owner-only.
- A read-only Omni memory statistics endpoint is exposed by the dashboard.

## Safety and policy findings

| Severity | Finding | Assessment |
|---|---|---|
| **High** | Omni used to be represented by machine-specific absolute symlinks. | The source is now tracked and portable. |
| **High** | The original healer could attempt live fixes by default, and delegation could start OpenCode without an approval adapter. | Healing is now dry-run-only. Delegation records intent but starts no subprocess. Omni daemon start/stop/restart/upgrade commands fail closed until proper service-control and approval integration exists. |
| **High** | `inference.private_data_must_stay_local=false` and `inference.never_send_to_cloud` exist in policy, but source inspection found no router enforcement for either field. | Treat these as declarative policy, **not a privacy boundary**. Do not send confidential prompts on the assumption that those fields keep them local. Add content/data classification and tests before claiming that guarantee. |
| **Medium** | `inference.free_cloud_only=false`. | Default strategy is free-model rotation and `openrouter.paid_enabled=false`; paid OpenRouter use is separately approval-gated. The policy does not globally prohibit cloud providers or prove that a provider's free tier will remain free. |
| **Medium** | `bin/dai-gemini.py` still recognizes fixed PulseAudio/PipeWire source names for the S22 and G8. | Those names may change after a reconnect/reset. Runtime source discovery and a real desktop-session microphone test remain required. The remote shell session did not have a reachable PulseAudio/PipeWire server, so this audit did not verify the active source. |
| **Medium** | Current device/registry declarations do not prove live device readiness. | Actual ADB detection for the S22, G8, and A16, Lubuntu peer health, Deskflow, PipeWire default input, and running services still require on-host verification. |

## Deliberate non-imports

- The two-line `speak` note in `tinysecrets/dai` was not copied because it is not an executable script or a maintainable interface.
- The private repository's additional automatic deletion of old files under `state/omni/runtime` was not imported. Deleting runtime artifacts should not be added without a bounded retention policy, a clear ownership boundary, and tests.
- The private helper's implicit fallback to a generated runtime policy was not imported. `policy/sovereign.json` remains the tracked policy source of truth; alternate paths must be explicit rather than silently switching policy files.
- The unified Gemini microphone mapping retains the G8/S22 integration but has not yet been made fully dynamic. This is a documented follow-up, not a verified working device connection.

## Verified on commit `b90ed3c98755c559cf3193a74f3e62d4698fbb0a`

- `python3 -m compileall -q lib services skills tests` — passed.
- `python3 -m unittest discover -s tests -t . -v` — **485 tests passed**, 0 failures on the Debian checkout.
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

**Audit status: tests and CI passed on the verified code commit; production readiness is still conditional on the release-gate items above.**
