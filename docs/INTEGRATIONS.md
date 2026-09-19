# DAI Frame Integrations

DAI is the central frame. Capabilities remain in their native projects while
the frame owns routing, policy, approvals, service health and orchestration.

## Core

- Vellum: primary personal-assistant runtime, identity and durable memory.
- Model router: free-tier inference rotation at :11435.
- Agent-S worker: bounded computer use at :8765.
- Voice bridge: local STT/TTS boundary at :8766.
- Headquarters/dashboard: operator surface at :8799.
- Ollama: local inference fallback at :11434.
- Qdrant: Vellum memory substrate.
- Heartbeat: proactive scheduled activity.

## Device layer

- Tailscale + SSH: remote machine access.
- Deskflow: keyboard/mouse and desktop coordination.
- ADB + scrcpy: Android device control.
- Xvfb + Agent-S: isolated GUI automation display.

## Project layer

config/integrations.json is the authoritative registry of the projects and
devices that belong to the DAI frame. Projects are adapters/capability sources;
their working trees are not copied into DAI.

This avoids turning DAI into a monorepo while still giving the assistant one
place to discover what it can use.

## Vellum

vendor/vellum-assistant is linked to the existing local Vellum checkout.
DAI's agent-s-delegate, dai-voice, and english-to-code skills are copied
into the Vellum assistant workspace during dai unify.

The Vellum debian-ai instance uses the dai-free profile and local Qdrant
memory. Keep Vellum's broader native skills in Vellum; DAI supplies the
machine-control and inference spine beneath them.

## Reconcile

Run:

./bin/dai unify

It is idempotent and checks the registry, Vellum link, skills, policy,
router, Agent-S worker, dashboard and voice bridge.
