# DAI System Map

**Purpose:** DAI is the central orchestration frame. This document records intended roles, not proof that a device or service is currently connected. Run `./bin/dai doctor`, `./bin/dai unify`, and the workstation/device checks before treating a capability as ready.

## Canonical workstation roles

| Node | Role | Boundary |
|---|---|---|
| Debian | Primary brain: DAI router, policy, dashboard, orchestration | The central control plane |
| Lubuntu | Secondary workstation / worker | Remote changes require explicit authorization |
| Samsung S22 Ultra | Primary mobile interface; optional hot-plug phone, camera, and microphone bridge | Detect transport and health at runtime; never infer connectivity from a saved IP |
| LG G8 | Permanent edge-node target for sensors, camera, and optional audio bridge | Treat as unavailable until ADB and service health are observed |
| Samsung A16 | Worker node | Keep execution bounded and approval-gated |
| TV / Roku | Media endpoint only | No workstation administration |
| Deskflow | Input bridge between authorized desktops | Explicit user-session control only |
| scrcpy / ADB | Android display and device-control bridge | Confirm exact device serial at runtime; do not hard-code personal device identifiers in tracked files |
| PipeWire | Single PC microphone-routing layer | Select source from live device discovery |
| Tailscale + SSH | Network fabric | Discover current peer addresses; do not commit private addresses |

## Required safety invariants

- `dry_run_default: true` remains the default for computer-use tasks.
- `require_approval_token: true` remains required for live Agent-S execution.
- Approval tokens must be single-use and scoped to the exact action.
- Destructive commands, package installation, elevated privileges, secret export, external messages, publishing, and system configuration changes require explicit approval.
- Provider/model choice must never weaken the execution policy. Model output is untrusted input, not authorization.
- Keep API keys, private SSH keys, cookies, device serials, private IPs, and live runtime state out of tracked files.
- A registry entry is only a declaration. A capability is “ready” only after a live health check proves it.

## Verification sequence

1. `./bin/doctor.sh`
2. `./bin/dai unify`
3. `./bin/smoke.sh --tests-only`
4. `python3 -m unittest discover -s tests -t . -v`
5. Check router, Agent-S, dashboard, and voice-bridge health endpoints on localhost.
6. Confirm the phone/edge devices with live ADB discovery and confirm the actual PipeWire default source.
7. Review the service unit definitions and logs before enabling or restarting services.

**Status:** Re-audit required. The previous map contained stale device roles and identifiers. This sanitized version intentionally omits personal serials, network addresses, and local-only paths.
