# Target architecture

```
Owner (desktop / later phone pairing)
        │
        ▼
Vellum Assistant  ← identity, memory, proactivity, approvals, skills
        │  skill: agent-s-delegate
        ▼
policy gate (local JSON + approval tokens)
        │
        ├─► model-router (:11435) ─► Ollama (:11434)  [default]
        │                        └─► OpenRouter       [explicit allow only]
        │
        └─► agent-s-worker (:8765)
                 │
                 ▼
            dedicated DISPLAY (Xvfb/VNC agent desktop) + browser/apps
                 │
                 ▼
            Agent S / gui-agents (when enabled)
```

## Non-goals for this first spine

- No overwrite of Hermes/NOVA/LIYA/OpenClaw working trees
- No paid OpenRouter calls
- No live control of the owner's everyday desktop session
- No `sudo` / package installs without explicit approval
- No full `vellum hatch` until you approve the Bun/setup install
