# Inspection report (2026-09-11)

Verified from local trees and upstream clones under `vendor/`. Nothing below claims untested runtime behavior.

## Host

| Fact | Value |
|------|--------|
| OS | Debian GNU/Linux 13 (trixie) |
| CPU | Intel i5-8400 (6c/6t) |
| RAM | ~11 GiB, **no swap** |
| GPU | Intel UHD Graphics 630 only (no NVIDIA) |
| Root disk | ~40 GiB free of 117 GiB |
| Local Ollama models | `llama3.2:3b`, `hermes3:8b` |
| Running container | `gobi-replica` |

## Existing local assistant stacks (left untouched)

| Path | Role |
|------|------|
| `~/.hermes` + `hermes-agent` | Active personal agent (CLI/gateway/desktop family); currently default model via `ollama-cloud` |
| `~/.openclaw` | Earlier personal-agent install; Agent-S ships an official OpenClaw skill |
| `NOVA-SOVEREIGN-AI` | Unified chat/agent-team/sidekick on Ollama/OpenRouter |
| `liya-ai-workstation` | Neural OS workstation UI + Mem0 + local CV |
| `real_Genie` / Ember | Local Ollama companion (FastAPI + React) |
| `gobi-sovereign` / `gobi-replica` | Deployed Node assistant replica |
| `deft` | Mobile/client surface (Expo) |
| `agent-vibes` | Protocol bridge / VS Code tooling |

Do **not** delete or overwrite these. Vellum becomes the canonical identity/memory layer; reuse pieces only via explicit adapters.

## Vellum Assistant (`vendor/vellum-assistant`)

| Fact | Evidence |
|------|----------|
| License | MIT |
| Runtime | Bun monorepo; `./setup.sh` installs deps + links global `vellum` |
| Models | Anthropic/OpenAI/Gemini/Fireworks/OpenRouter/MiniMax + OpenAI-compatible + **Ollama**; embeddings local ONNX by default |
| Skills | `SKILL.md` + optional `scripts/`; plugins under workspace `plugins/` |
| Phone reach | Device pairing + tunnel documented (`vellum pair` / pair-a-device docs) |
| Official desktop downloads | macOS + Windows marketed |
| Linux client | `clients/linux` exists (Electron AppImage). **First iteration**: login/chat work; native helper (dictation, verified input, computer-use, permission probes) **fail closed** until a sidecar exists |
| Built-in browser | `vellum-browser-use` skill (`assistant browser` CLI); preferred Chrome extension mode |
| Computer-use sandbox | Architecture describes containerized Xvnc/Chrome desktop for managed/container paths — not a finished Debian host computer-use sidecar |

## Agent S (`vendor/Agent-S`)

| Fact | Evidence |
|------|----------|
| License | Apache-2.0 |
| Package | `gui-agents` — install into the dedicated venv: `python3 -m venv ~/.local/agent-s-venv && ~/.local/agent-s-venv/bin/pip install gui-agents` |
| Platforms | Linux, macOS, Windows (README badges + Linux deps) |
| Control | Screen + mouse/keyboard (`pyautogui`; Linux needs display) |
| Main models | OpenAI, Anthropic, Gemini, Azure, **vLLM**, OpenRouter |
| Grounding | **Required**; recommended UI-TARS-1.5-7B endpoint |
| Official OpenClaw skill | `integrations/openclaw/` |
| OCR | Needs `tesseract` (`apt install tesseract-ocr` — **not installed yet**; needs owner approval) |

## Hardware implication for Agent S

UI-TARS-1.5-7B and heavy VLMs are a poor fit for this machine without an external GPU or remote grounding endpoint. Local plan: keep grounding/main GUI models behind the model-router; default Agent S worker to **dry-run** until a dedicated display + approved model path exist.

## Integration choice (least invasive)

1. Keep upstream trees read-only under `vendor/` (symlinks to `workspace/repos/inspect/`).
2. Talk to Vellum through its native **skill** surface (bash + scripts), not by forking Vellum.
3. Run Agent S as a **separate worker service** with a structured task/result API.
4. Put all cloud inference behind `services/model-router` (Ollama first; OpenRouter off by default).
