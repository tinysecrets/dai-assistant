# Agent Toolchain

The D-A-I stack is intended to use tools instead of handing routine work back to Justin.

## Local tool lanes

- Agent S: GUI/computer-use tasks on dedicated Xvfb :99; live runs remain approval-gated.
- Voice bridge: local STT/TTS on :8766; use dai ask, dai listen, or the Vellum dai-voice skill.
- Model router: :11435; use dai/auto for normal inference and dai/vision-auto for GUI grounding.
- Android: ADB + scrcpy; registered devices live in config/integrations.json.
- Deskflow/Tailscale/SSH: remote desktop and device-layer operations.
- Workbench/Wah-Lah: Hatchable agent bridge for higher-level project orchestration.

## Default behavior

1. Prefer an existing tool or executable over manual shell instructions.
2. For GUI work, delegate to Agent S rather than describing clicks.
3. For recurring health/recovery work, use the automation layer and dai repair.
4. Keep live GUI actions approval-gated and scoped to the exact instruction.
5. Never expose or print secrets; keys stay in local .env or managed Hatchable secrets.

## Desktop entry points

D-A-I Repair Center restores the common local stack in one click.
Restore LG G8 Microphone uses the same repair path.
D-A-I Command Center opens the headquarters dashboard.
Wah-Lah Agent opens the linked Gamut agent.

## Verification

Run ./bin/dai doctor, ./bin/dai smoke, and ./bin/dai repair after major workstation changes.
