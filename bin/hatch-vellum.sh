#!/usr/bin/env bash
# Hatch a local Vellum assistant pointed at the Debian AI model-router.
#
#   ./bin/hatch-vellum.sh              hatch (refuses if no inference path is ready)
#   ./bin/hatch-vellum.sh --force      hatch even without a ready provider
#   ./bin/hatch-vellum.sh --name foo   choose the assistant name
#
# Safe to re-run.  Only the keys Vellum actually needs are exported, and only
# for the single command that uses them — .env is parsed, never sourced.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

FORCE=0
NAME="${VELLUM_ASSISTANT_NAME:-debian-ai}"
while (($#)); do
  case "$1" in
    --force) FORCE=1; shift ;;
    --name) shift; [[ $# -ge 1 ]] || dai_die "--name needs a value"; NAME="$1"; shift ;;
    -h|--help) dai_usage; exit 0 ;;
    *) dai_die "unknown option: $1 (try --help)" ;;
  esac
done

dai_need python3

ROUTER_URL="http://$(dai_env_get DAI_ROUTER_HOST 127.0.0.1):$(dai_env_get DAI_ROUTER_PORT 11435)"

VELLUM_BIN=""
if dai_have vellum; then
  VELLUM_BIN="$(command -v vellum)"
elif [[ -x "$HOME/.bun/bin/vellum" ]]; then
  VELLUM_BIN="$HOME/.bun/bin/vellum"
fi
if [[ -z "$VELLUM_BIN" ]]; then
  dai_die "vellum CLI not found (expected ~/.bun/bin/vellum).
  Install it from vendor/vellum-assistant:  cd vendor/vellum-assistant && ./setup.sh"
fi
dai_ok "vellum CLI: $VELLUM_BIN"

if ! dai_http_up "$ROUTER_URL" 3; then
  dai_die "model-router is not up at $ROUTER_URL — run ./bin/start-spine.sh first"
fi
dai_ok "model-router is up at $ROUTER_URL"

READY="$(dai_http "$ROUTER_URL/health" 5 | dai_json_get ready_for_chat || true)"
if [[ "$READY" != "true" && "$FORCE" -ne 1 ]]; then
  dai_fail "no inference path is ready (no cloud key, no local Ollama)"
  dai_say "  Add a key to .env (see KEYS.md), restart the spine, or pass --force." >&2
  exit 2
fi
if [[ "$READY" == "true" ]]; then
  dai_ok "router reports ready_for_chat"
else
  dai_warn "hatching anyway (--force): the assistant will not be able to answer yet"
fi

OPENROUTER_KEY="$(dai_env_get OPENROUTER_API_KEY)"
dai_head "Hatching assistant '$NAME'"

# One hatch attempt.  Config-key names differ between Vellum versions, so try
# the explicit model config first and fall back to a plain hatch — but never
# hatch twice, which used to leave two assistants behind.
hatched=0
if [[ -n "$OPENROUTER_KEY" ]]; then
  dai_info "OpenRouter key present: hatching with native provider config"
  if "$VELLUM_BIN" hatch --name "$NAME" --disable-platform -d \
      --config "agents.defaults.model.primary=openrouter/auto" >/dev/null 2>&1; then
    hatched=1
  fi
fi
if ((hatched == 0)); then
  dai_info "hatching with defaults (inference configured after hatch)"
  if "$VELLUM_BIN" hatch --name "$NAME" --disable-platform -d; then
    hatched=1
  fi
fi

if ((hatched == 0)); then
  dai_die "vellum hatch failed — see the output above"
fi
dai_ok "hatch completed"

dai_say ""
dai_head "Post-hatch steps"
dai_say "  export PATH=\"\$HOME/.bun/bin:\$PATH\""
dai_say "  vellum use $NAME"
dai_say ""
dai_say "  # Point inference at the rotator (model id: dai/auto)"
dai_say "  #   base URL: $ROUTER_URL/v1   (OpenAI-compatible)"
if [[ -n "$OPENROUTER_KEY" ]]; then
  dai_say "  # or use OpenRouter natively:"
  dai_say "  OPENROUTER_API_KEY=<from .env> vellum setup --provider openrouter"
fi
dai_say ""
dai_say "  # Install the GUI-delegation skill into the assistant workspace:"
dai_say "  ./bin/install-vellum-skill.sh"
dai_say ""
dai_say "  vellum wake"
dai_say ""
dai_dim "Phone pairing later: vellum pair"
