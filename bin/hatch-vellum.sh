#!/usr/bin/env bash
# Hatch a local Vellum assistant pointed at the Debian AI model-router.
# Safe to re-run: refuses if no inference path is ready unless --force.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.bun/bin:$PATH"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if ! command -v vellum >/dev/null; then
  echo "vellum CLI missing. Expected ~/.bun/bin/vellum" >&2
  exit 1
fi

if ! curl -sf http://127.0.0.1:11435/health >/dev/null; then
  echo "model-router not up. Run ./bin/start-spine.sh first." >&2
  exit 1
fi

READY=$(curl -sf http://127.0.0.1:11435/health | python3 -c 'import sys,json; print(json.load(sys.stdin).get("ready_for_chat"))')
if [[ "$READY" != "True" && "$FORCE" -ne 1 ]]; then
  echo "No cloud keys / local Ollama ready yet." >&2
  echo "Add keys to .env (see KEYS.md), restart spine, or pass --force to hatch anyway." >&2
  exit 2
fi

# Prefer OpenRouter directly in Vellum if key present (native), else openai-compatible → router.
set -a
# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && source "$ROOT/.env"
set +a

NAME="${VELLUM_ASSISTANT_NAME:-debian-ai}"
SKILL_SRC="$ROOT/skills/agent-s-delegate"

echo "Hatching Vellum assistant name=$NAME (local env)..."
vellum env set local >/dev/null 2>&1 || true

HATCH_ARGS=(hatch --name "$NAME" --disable-platform -d)

if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  echo "Using OpenRouter key via vellum setup after hatch (native provider)."
  # Hatch first; configure provider after. Also set default model to dai-style free.
  vellum "${HATCH_ARGS[@]}" \
    --config "agents.defaults.model.primary=openrouter/auto" 2>/dev/null \
    || vellum hatch --name "$NAME" --disable-platform -d
else
  echo "Hatching with openai-compatible → http://127.0.0.1:11435/v1 (dai/auto)."
  # Config keys vary by Vellum version; hatch minimally then print manual steps.
  vellum hatch --name "$NAME" --disable-platform -d || true
fi

echo ""
echo "=== post-hatch steps (run once keys exist) ==="
echo "  export PATH=\"\$HOME/.bun/bin:\$PATH\""
echo "  vellum use $NAME   # if needed"
echo "  # Native OpenRouter:"
echo "  OPENROUTER_API_KEY=... vellum setup --provider openrouter"
echo "  # OR point at local rotator (openai-compatible) via assistant UI / llm-provider-setup skill"
echo "  vellum wake"
echo "  # Install Agent S delegate skill into the assistant workspace:"
echo "  ./bin/install-vellum-skill.sh"
echo ""
echo "Pair phone later: vellum pair"
