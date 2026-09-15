#!/usr/bin/env bash
# Diagnose the spine: files, config, keys, services, optional GUI dependencies.
#
#   ./bin/doctor.sh            human-readable report
#   ./bin/doctor.sh --json     machine-readable (for CI / monitoring)
#   ./bin/doctor.sh --quiet    only the summary line
#
# Exit status: 0 = usable, 1 = something must be fixed.  Warnings never fail
# the run: a fresh clone with no keys and no vendor/ tree is a *working* spine
# that simply cannot chat yet, and it should not look broken.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

JSON=0
QUIET=0
for arg in "$@"; do
  case "$arg" in
    --json) JSON=1 ;;
    --quiet|-q) QUIET=1 ;;
    -h|--help)
      dai_usage
      exit 0
      ;;
    *) dai_die "unknown option: $arg (try --help)" ;;
  esac
done

# --json prints machine-readable output only; nothing else may touch stdout.
if ((QUIET || JSON)); then
  dai_say() { :; }
  dai_head() { :; }
  dai_dim() { :; }
  dai_info() { :; }
fi

dai_need python3 curl

ROUTER_PORT="$(dai_env_get DAI_ROUTER_PORT 11435)"
WORKER_PORT="$(dai_env_get DAI_AGENT_S_PORT 8765)"
VOICE_PORT="$(dai_env_get DAI_VOICE_BRIDGE_PORT 8766)"
ROUTER_URL="http://$(dai_env_get DAI_ROUTER_HOST 127.0.0.1):$ROUTER_PORT"
WORKER_URL="http://$(dai_env_get DAI_AGENT_S_HOST 127.0.0.1):$WORKER_PORT"
VOICE_URL="http://$(dai_env_get DAI_VOICE_BRIDGE_HOST 127.0.0.1):$VOICE_PORT"
OLLAMA_URL="http://127.0.0.1:$(dai_env_get DAI_OLLAMA_PORT 11434)"

# --- 1. repository files ----------------------------------------------------

dai_head "== Debian AI doctor =="
dai_dim "root: $ROOT"
dai_say ""
dai_head "Repository"

required_files=(
  ".env.example"
  "config/rotation-pool.json"
  "config/free-models.json"
  "policy/sovereign.json"
  "services/model-router/server.py"
  "services/agent-s-worker/server.py"
  "services/voice-bridge/server.py"
  "skills/agent-s-delegate/SKILL.md"
  "lib/dai/__init__.py"
)
for rel in "${required_files[@]}"; do
  if [[ -f "$ROOT/$rel" ]]; then
    dai_ok "$rel"
  else
    dai_fail "$rel is missing"
  fi
done

for rel in bin/*.sh bin/dai; do
  [[ -e "$ROOT/$rel" ]] || continue
  # lib.sh is sourced by the other scripts, never executed.
  [[ "$(basename "$rel")" == "lib.sh" ]] && continue
  if [[ ! -x "$ROOT/$rel" ]]; then
    dai_fail "$rel is not executable (chmod +x \"$rel\")"
  fi
done

# --- 2. configuration validity ---------------------------------------------

dai_say ""
dai_head "Configuration"

for rel in config/rotation-pool.json config/free-models.json policy/sovereign.json; do
  path="$ROOT/$rel"
  if [[ ! -f "$path" ]]; then
    dai_fail "$rel missing"
  elif dai_check_json "$path"; then
    dai_ok "$rel parses"
  else
    dai_fail "$rel is not valid JSON"
  fi
done

if [[ -f "$DAI_APPROVALS_FILE" ]]; then
  if dai_check_json "$DAI_APPROVALS_FILE"; then
    perms="$(stat -c '%a' "$DAI_APPROVALS_FILE" 2>/dev/null || echo '?')"
    if [[ "$perms" == "600" || "$perms" == "400" ]]; then
      dai_ok "policy/approvals.json parses (mode $perms)"
    else
      dai_warn "policy/approvals.json is mode $perms — chmod 600 (it holds approval tokens)"
    fi
  else
    dai_fail "policy/approvals.json is not valid JSON"
  fi
else
  dai_info "policy/approvals.json absent — created on first bin/issue-approval.sh (fine for dry-run)"
fi

if [[ -f "$DAI_ENV_FILE" ]]; then
  perms="$(stat -c '%a' "$DAI_ENV_FILE" 2>/dev/null || echo '?')"
  if [[ "$perms" == "600" || "$perms" == "400" ]]; then
    dai_ok ".env present (mode $perms)"
  else
    dai_warn ".env is mode $perms — run: chmod 600 .env"
  fi
  if git -C "$ROOT" ls-files --error-unmatch .env >/dev/null 2>&1; then
    dai_fail ".env is tracked by git — remove it: git rm --cached .env"
  fi
else
  dai_warn ".env missing — cp .env.example .env, or ./bin/import-keys.sh"
fi

# Deep validation by the services themselves (pool contents, key shapes, policy keys).
# Each service separates `problems` (config/safety faults) from `warnings`
# (optional runtime deps that are absent), so nothing has to be re-classified
# here by matching substrings.
for svc in model-router agent-s-worker voice-bridge; do
  out="$(python3 "$ROOT/services/$svc/server.py" --check 2>&1)" || true
  report="$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    print("PROBLEM\tcheck did not return JSON")
    sys.exit(0)
if not isinstance(doc, dict):
    print("PROBLEM\tcheck returned a non-object")
    sys.exit(0)
for item in doc.get("problems") or []:
    print(f"PROBLEM\t{item}")
for item in doc.get("warnings") or []:
    print(f"WARNING\t{item}")
' 2>/dev/null || echo "PROBLEM	check failed to run")"
  if [[ -z "$report" ]]; then
    dai_ok "$svc --check clean"
  else
    while IFS=$'\t' read -r level line; do
      [[ -n "$line" ]] || continue
      if [[ "$level" == "PROBLEM" ]]; then
        dai_fail "$svc: $line"
      else
        dai_warn "$svc: $line"
      fi
    done <<<"$report"
  fi
done

# --- 3. keys ----------------------------------------------------------------

dai_say ""
dai_head "Provider keys"

key_report="$(python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["ROOT"])
from lib.dai.env import parse_dotenv
from pathlib import Path

env = dict(os.environ)
path = Path(os.environ["DAI_ENV_FILE"])
if path.exists():
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        env.setdefault(key, value)

providers = [
    ("OPENROUTER_API_KEY", "openrouter", "recommended: drives the free top-model rotation"),
    ("GROQ_API_KEY", "groq", "separate rate-limit bucket"),
    ("CEREBRAS_API_KEY", "cerebras", "fast fallback"),
    ("OLLAMA_API_KEY", "ollama_cloud", "Ollama Cloud"),
]
present, missing = [], []
for var, name, note in providers:
    (present if env.get(var, "").strip() else missing).append((var, name, note))
for var, name, note in present:
    value = env[var].strip()
    flag = "" if len(value) >= 12 else "  (looks truncated)"
    print(f"OK\t{name}\t{var} set{flag}")
for var, name, note in missing:
    print(f"MISS\t{name}\t{var}\t{note}")
PY
)"

any_key=0
while IFS=$'\t' read -r kind name rest extra; do
  [[ -n "${kind:-}" ]] || continue
  if [[ "$kind" == "OK" ]]; then
    dai_ok "$name — $rest"
    any_key=1
  else
    dai_warn "$name — $rest not set ($extra)"
  fi
done <<<"$key_report"

if ((any_key)); then
  dai_ok "at least one cloud provider key is configured"
else
  dai_warn "no cloud keys yet — chat needs one, or a local Ollama (see KEYS.md)"
fi

# --- 4. services ------------------------------------------------------------

dai_say ""
dai_head "Services"

check_service() {
  local label="$1" url="$2" pidname="$3"
  local body
  if body="$(dai_http "$url/health" 3)"; then
    local version ready
    version="$(printf '%s' "$body" | dai_json_get version)"
    dai_ok "$label is up (v${version:-?}) at $url"
    if [[ "$pidname" == "model-router" ]]; then
      ready="$(printf '%s' "$body" | dai_json_get ready_for_chat)"
      if [[ "$ready" == "true" ]]; then
        dai_ok "model-router ready_for_chat"
      else
        dai_warn "model-router is up but ready_for_chat=false — no usable provider yet"
      fi
      candidates="$(printf '%s' "$body" | dai_json_get candidates_available)"
      cooldowns="$(printf '%s' "$body" | dai_json_get cooldowns_active)"
      dai_info "candidates available: ${candidates:-?} (cooldowns active: ${cooldowns:-0})"
    elif [[ "$pidname" == "voice-bridge" ]]; then
      whisper="$(printf '%s' "$body" | dai_json_get whisper_loaded)"
      piper="$(printf '%s' "$body" | dai_json_get voice_loaded)"
      ffmpeg="$(printf '%s' "$body" | dai_json_get ffmpeg)"
      dai_info "whisper=${whisper:-?} piper=${piper:-?} ffmpeg=${ffmpeg:-?}"
      if [[ "$whisper" != "true" || "$piper" != "true" ]]; then
        dai_warn "voice-bridge up but models not loaded — it needs ~/.local/voice-venv (faster-whisper, piper)"
      fi
    else
      live="$(printf '%s' "$body" | dai_json_get live_capable)"
      dryrun="$(printf '%s' "$body" | dai_json_get dry_run_default)"
      dai_info "dry_run_default=${dryrun:-?} live_capable=${live:-?}"
    fi
    printf '%s' "$body" >"$DAI_STATE_DIR/.doctor-$pidname.json" 2>/dev/null || true
    return 0
  fi
  if pid="$(dai_read_pid "$pidname")"; then
    dai_fail "$label has a pid file ($pid) but is not answering — check logs/$pidname.log"
  elif dai_port_in_use "${url##*:}"; then
    dai_fail "port ${url##*:} is held by something that is not $label"
  else
    dai_warn "$label is not running — ./bin/start-spine.sh"
  fi
  return 1
}

router_up=0
worker_up=0
voice_up=0
check_service "model-router" "$ROUTER_URL" "model-router" && router_up=1 || true
check_service "agent-s-worker" "$WORKER_URL" "agent-s-worker" && worker_up=1 || true
check_service "voice-bridge" "$VOICE_URL" "voice-bridge" && voice_up=1 || true

if dai_http "$OLLAMA_URL/api/tags" 2 >/dev/null; then
  models="$(dai_http "$OLLAMA_URL/api/tags" 3 | python3 -c '
import json,sys
try:
    print(", ".join(m["name"] for m in json.load(sys.stdin).get("models", [])[:6]))
except Exception:
    print("")
' 2>/dev/null || true)"
  dai_ok "local Ollama reachable${models:+ ($models)}"
else
  dai_info "local Ollama not running (fine when using cloud keys)"
fi

# --- 5. optional GUI dependencies ------------------------------------------

dai_say ""
dai_head "Optional (live GUI only)"

venv_dir="${DAI_AGENT_S_VENV:-$HOME/.local/agent-s-venv}"
if [[ -x "$venv_dir/bin/agent_s" ]]; then
  dai_ok "gui-agents installed ($venv_dir/bin/agent_s)"
else
  dai_info "gui-agents not installed — dry-run still works. Install with:"
  dai_info "  python3 -m venv $venv_dir && $venv_dir/bin/pip install gui-agents"
fi

for tool in Xvfb xset tesseract; do
  if dai_have "$tool"; then
    dai_ok "$tool available"
  else
    case "$tool" in
      tesseract) dai_info "tesseract missing — OCR for Agent S (apt install tesseract-ocr)" ;;
      *) dai_info "$tool missing — needed for the dedicated agent display (apt install xvfb x11-utils)" ;;
    esac
  fi
done

voice_py="${DAI_VOICE_VENV:-$HOME/.local/voice-venv}/bin/python"
if [[ -x "$voice_py" ]]; then
  dai_ok "voice venv present ($voice_py)"
else
  dai_info "voice venv not installed — /v1/audio/* will be unavailable. Install with:"
  dai_info "  python3 -m venv ~/.local/voice-venv && ~/.local/voice-venv/bin/pip install faster-whisper piper-tts"
fi

display="$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1]))["agent_s"].get("display", ":99"))
except Exception:
    print(":99")
' "$DAI_POLICY_FILE" 2>/dev/null || echo ':99')"
if dai_have xset && xset -display "$display" q >/dev/null 2>&1; then
  dai_ok "agent display $display is answering"
else
  dai_info "agent display $display is not up (start-spine.sh starts Xvfb when installed)"
fi

if [[ -d "$ROOT/vendor/vellum-assistant" ]]; then
  dai_ok "vendor/vellum-assistant present"
else
  dai_warn "vendor/vellum-assistant missing — link it to hatch Vellum:"
  dai_warn "  mkdir -p vendor && ln -s <path-to-vellum-assistant> vendor/vellum-assistant"
fi
if [[ -d "$ROOT/vendor/Agent-S" ]]; then
  dai_ok "vendor/Agent-S present"
else
  dai_info "vendor/Agent-S missing — reference only; not needed to run the spine"
fi

if dai_have vellum || [[ -x "$HOME/.bun/bin/vellum" ]]; then
  dai_ok "vellum CLI linked"
else
  dai_warn "vellum CLI not found — run vendor/vellum-assistant/setup.sh, then ./bin/hatch-vellum.sh"
fi

for rel in skills/agent-s-delegate skills/english-to-code; do
  if [[ -f "$ROOT/$rel/SKILL.md" ]]; then
    dai_ok "skill $(basename "$rel")"
  else
    dai_fail "skill $rel/SKILL.md missing"
  fi
done

# --- summary ---------------------------------------------------------------

dai_say ""
if ((QUIET)); then
  if ((JSON)); then
    :  # stdout is reserved for the JSON report below
  else
    # dai_say is muted above (that is what keeps --json's stdout pure), so the
    # documented summary line must go out through printf instead.
    printf 'doctor: ok=%s warn=%s fail=%s\n' "$DAI_OK_COUNT" "$DAI_WARN_COUNT" "$DAI_FAIL_COUNT"
  fi
else
  dai_head "Next steps"
  if [[ ! -f "$DAI_ENV_FILE" ]]; then
    dai_say "  1. ./bin/import-keys.sh          # or: cp .env.example .env && edit"
  fi
  if ((router_up == 0 || worker_up == 0)); then
    dai_say "  2. ./bin/start-spine.sh          # start the router + worker"
  fi
  dai_say "  3. ./bin/smoke.sh                # end-to-end self-test"
  dai_say "  4. ./bin/hatch-vellum.sh         # once ready_for_chat is true"
  dai_say ""
  dai_say "Docs: README.md (overview) · docs/API.md (endpoints) · docs/OPERATIONS.md (runbook)"
fi

if ((JSON)); then
  DAI_JSON_ROUTER="$router_up" \
  DAI_JSON_WORKER="$worker_up" \
  DAI_JSON_VOICE="$voice_up" \
  DAI_JSON_KEYS="$any_key" \
  python3 - "$DAI_OK_COUNT" "$DAI_WARN_COUNT" "$DAI_FAIL_COUNT" <<'PY'
import json, os, sys
from pathlib import Path

ok, warn, fail = (int(a) for a in sys.argv[1:4])


def health(name):
    path = Path(os.environ["DAI_STATE_DIR"]) / f".doctor-{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


router = health("model-router")
worker = health("agent-s-worker")
voice = health("voice-bridge")
report = {
    # Mirrors the exit status: warnings (no keys, nothing running yet) are not
    # failures — a fresh clone is a working spine that cannot chat yet.
    "ok": fail == 0,
    "checks": {"ok": ok, "warn": warn, "fail": fail},
    "keys_configured": bool(int(os.environ["DAI_JSON_KEYS"])),
    "services": {
        "model_router": {
            "up": bool(int(os.environ["DAI_JSON_ROUTER"])),
            "ready_for_chat": (router or {}).get("ready_for_chat"),
            "candidates_available": (router or {}).get("candidates_available"),
            "cooldowns_active": (router or {}).get("cooldowns_active"),
            "config_errors": (router or {}).get("config_errors"),
        },
        "agent_s_worker": {
            "up": bool(int(os.environ["DAI_JSON_WORKER"])),
            "dry_run_default": (worker or {}).get("dry_run_default"),
            "live_capable": (worker or {}).get("live_capable"),
            "queue_depth": (worker or {}).get("queue_depth"),
        },
        "voice_bridge": {
            "up": bool(int(os.environ["DAI_JSON_VOICE"])),
            "whisper_loaded": (voice or {}).get("whisper_loaded"),
            "voice_loaded": (voice or {}).get("voice_loaded"),
        },
    },
}
print(json.dumps(report, indent=2))
PY
fi

dai_summary
