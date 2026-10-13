# Wah-Lah AI Team: operator manual

## Operating hierarchy

1. **Owner:** Wah-Lah Industries controls goals, model choices, project priorities, and acceptance criteria.
2. **DAI overseer:** `dai/auto` is the coordinating entry point. It creates the plan, assigns the work, asks for evidence, and commissions an independent review.
3. **Shared model gateway:** FreeLLMAPI at `http://127.0.0.1:3001/v1`, model alias `auto`. The active profile is `Free-Only Free-First`.
4. **DAI router:** `http://127.0.0.1:11435/v1`. The DAI provider order places `freellmapi/auto` first. Under the strict free-only policy, explicit OpenRouter `:free` models and local Ollama remain available; unknown/paid cloud providers are not used as automatic fallbacks.
5. **Specialist tools:** OpenCode for code changes; DAI planning, research, debugging, and review agents; Agent S for bounded visual tasks; DAI voice bridge for audio workflows. A role only counts as available if its entry point exists and its own health/connectivity is working.
6. **Projects:** Genie uses DAI; DAI Omni provides the existing health/healing and agent coordination layer; Nova can use the gateway before its local fallback; Zero Gravity remains a Vercel-hosted application with its own server-side route and credentials.

The DAI overseer stays at `dai/auto`; model selection is separate from application authority and action approval. This integration does not retrain or rewrite the selected models.

## Quick commands

From the DAI repository:

```bash
./bin/dai team --status
./bin/dai team --list-capabilities
./bin/dai team --dry-run "Fix the model router timeout and add a test"
./bin/dai team --mode code "Implement the requested change with tests"
./bin/dai team --mode research "Research verified prospects in the selected market"
./bin/dai team --mode debug "Diagnose why a service is failing"
./bin/dai team --mode gui --dry-run "Open Chromium and inspect the current page"
./bin/dai team --mode voice "Read the latest system status aloud"
./bin/dai team --timeout 900 --max-retries 2 "Task description"
```

Use `--dry-run` first to inspect the assignment without calling a model. `--max-retries` is bounded to 0–2. The team runner stages the work rather than launching uncoordinated parallel changes to the same files.

## Mandatory non-trivial workflow

`plan -> specialist -> verification -> bounded correction if required -> independent review -> outcome record`

- Code work is delegated to the installed OpenCode CLI when present.
- Research and debugging use the corresponding DAI specialist agents.
- The review agent is called after the latest correction, not just before it.
- DAI’s known regression suite is run for code work performed inside this repository.
- For other code repositories, the runner requires a detected Python or JavaScript test suite. If it cannot run a deterministic project test, the run is marked unverified and the blocker is recorded; it does not call `git diff --check` alone a successful test.
- If a worker or reviewer is unavailable, the run is marked incomplete instead of being reported as a success.

## GUI and voice capabilities

GUI tasks default to a dry-run, so the worker reports the proposed action without touching a display:

```bash
./bin/dai team --mode gui --dry-run "Open Chromium and go to example.com"
```

Live control requires a fresh, single-use approval token scoped to the exact instruction and passed through the environment, never command arguments:

```bash
export DAI_APPROVAL_TOKEN="$(./bin/issue-approval.sh agent_s_gui_task "Open Chromium and go to example.com" | tail -n 1)"
./bin/dai team --mode gui --live-gui "Open Chromium and go to example.com"
unset DAI_APPROVAL_TOKEN
```

Verify the exact output format of `issue-approval.sh` for your current install before using the example pipeline. The worker itself enforces token scope. Live GUI operations are not automatically replayed when they fail, and the team result remains `executed-unverified` until a human or deterministic screen/task check confirms the postcondition.

Voice tasks route to `bin/dai-voice`. They are not automatically replayed during correction because that might duplicate audio or external actions.

## Shared learning

Artifacts are kept under `state/omni/team/runs/<run-id>/`; the private outcome log is `state/omni/team/runs.jsonl`. Edit `state/omni/team/owner-notes.md` to provide durable owner-authored knowledge such as project architecture, naming conventions, known failure fixes, and priorities.

The next task includes recent outcomes and owner notes in shared task context. Credentials and raw task bodies are not intentionally stored in the run log. This is **shared operational memory and test feedback, not model-weight training**. Keep entries factual; promote a candidate lesson to owner notes after a test or owner confirmation verifies it.

## Self-healing

DAI Omni already has a configured healing cycle. Use `./bin/dai team --status`, `./bin/dai doctor`, and the relevant service health endpoint before repair. For a diagnosed failure: preserve a backup, make a minimal reversible correction, run its test, and record the result. Avoid infinite retry loops; unresolved failures must name the exact blocker and responsible capability.

Health endpoints:

| Component | Local address |
|---|---|
| FreeLLMAPI | `http://127.0.0.1:3001/api/health` |
| DAI model router | `http://127.0.0.1:11435/health` |
| Agent S worker | `http://127.0.0.1:8765/health` |
| Voice bridge | `http://127.0.0.1:8766/health` |
| Dashboard | `http://127.0.0.1:8799` |

The dashboard port may expose HTML rather than a JSON health endpoint; a failed `/health` probe there only means that route is absent, not that the dashboard itself is down.

## Owner authority and approval boundaries

The team runner is owner-directed: use the owner's model roster and provider order. It does not quietly change the selected models. It must also honor the existing DAI approval policy for live desktop actions, package installs, sudo, credential export, external messages, Git publishing, destructive changes, and paid inference. A task can be carried right up to that boundary and should report the precise action awaiting the owner's authorization.

## Reports and recovery

- Each run gets a timestamped artifact folder.
- The outcome log records task hashes, modes, result status, test evidence, retry count, and artifact references; it does not need raw customer prompts.
- Restore a specific config from the corresponding backup before retrying a broad repair.
- A free-tier endpoint may become rate-limited, unavailable, or change its plan. The router should surface this and use the approved free/local fallback; no external provider is guaranteed free forever.
