#!/usr/bin/env bash
# Start the spine: model-router (:11435) + agent-s-worker (:8765), and the
# dedicated agent display when Xvfb is installed.
#
#   ./bin/start-spine.sh              start (restarts if already running)
#   ./bin/start-spine.sh --no-xvfb    skip the agent display
#   ./bin/start-spine.sh --status     report and exit without starting
#
# Idempotent: an existing spine is stopped first, so re-running is safe.
# Both services must answer /health before this script reports success.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

WANT_XVFB=1
STATUS_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-xvfb) WANT_XVFB=0 ;;
    --status) STATUS_ONLY=1 ;;
    -h|--help)
      dai_usage
      exit 0
      ;;
    *) dai_die "unknown option: $arg (try --help)" ;;
  esac
done

dai_need python3 curl

ROUTER_HOST="$(dai_env_get DAI_ROUTER_HOST 127.0.0.1)"
ROUTER_PORT="$(dai_env_get DAI_ROUTER_PORT 11435)"
WORKER_HOST="$(dai_env_get DAI_AGENT_S_HOST 127.0.0.1)"
WORKER_PORT="$(dai_env_get DAI_AGENT_S_PORT 8765)"
ROUTER_URL="http://$ROUTER_HOST:$ROUTER_PORT"
WORKER_URL="http://$WORKER_HOST:$WORKER_PORT"
AGENT_DISPLAY="$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1]))["agent_s"].get("display", ":99"))
except Exception:
    print(":99")
' "$DAI_POLICY_FILE" 2>/dev/null || echo ':99')"

if ((STATUS_ONLY)); then
  for pair in "model-router:$ROUTER_URL" "agent-s-worker:$WORKER_URL"; do
    name="${pair%%:*}"
    url="${pair#*:}"
    if dai_http_up "$url" 2; then
      dai_ok "$name up at $url"
    else
      dai_warn "$name not running"
    fi
  done
  exit 0
fi

mkdir -p "$DAI_STATE_DIR" "$DAI_LOG_DIR"
dai_env_ensure

# --- stop anything we already own ------------------------------------------

"$ROOT/bin/stop-spine.sh" --quiet >/dev/null 2>&1 || true

# Refuse to fight a foreign process holding our ports.
for pair in "model-router:$ROUTER_PORT" "agent-s-worker:$WORKER_PORT"; do
  name="${pair%%:*}"
  port="${pair#*:}"
  if dai_port_in_use "$port"; then
    dai_fail "port $port ($name) is already in use by another process"
    dai_say "  find it with: ss -ltnp | grep $port" >&2
    exit 1
  fi
done

# --- dedicated agent display (never the owner's live desktop) ---------------

if ((WANT_XVFB)) && dai_have Xvfb; then
  if dai_have xset && xset -display "$AGENT_DISPLAY" q >/dev/null 2>&1; then
    dai_ok "agent display $AGENT_DISPLAY already up"
  else
    nohup Xvfb "$AGENT_DISPLAY" -screen 0 "${DAI_AGENT_GEOMETRY:-1280x800x24}" \
      >"$DAI_LOG_DIR/xvfb.log" 2>&1 &
    echo $! >"$DAI_STATE_DIR/xvfb.pid"
    ready=0
    for _ in $(seq 1 20); do
      if dai_have xset && xset -display "$AGENT_DISPLAY" q >/dev/null 2>&1; then
        ready=1
        break
      fi
      sleep 0.25
    done
    if ((ready)); then
      dai_ok "Xvfb $AGENT_DISPLAY up (pid $(cat "$DAI_STATE_DIR/xvfb.pid"))"
    else
      dai_warn "Xvfb $AGENT_DISPLAY did not come up — live GUI tasks will stay blocked"
      dai_dim "  see $DAI_LOG_DIR/xvfb.log"
    fi
  fi
elif ((WANT_XVFB)); then
  dai_info "Xvfb not installed — skipping the agent display (dry-run still works)"
fi

# --- services --------------------------------------------------------------
#
# .env is deliberately NOT sourced here: values may contain spaces or quotes,
# and sourcing would export every secret into every child process.  The
# services parse it themselves through lib/dai/env.py.

dai_start_service "model-router" "$ROOT/services/model-router/server.py"
dai_start_service "agent-s-worker" "$ROOT/services/agent-s-worker/server.py"

failed=0
if dai_wait_up "$ROUTER_URL" 15 "model-router"; then
  dai_ok "model-router healthy at $ROUTER_URL"
else
  dai_fail "model-router failed to start — last 30 lines of $DAI_LOG_DIR/model-router.log:"
  tail -30 "$DAI_LOG_DIR/model-router.log" >&2 2>/dev/null || true
  failed=1
fi

if dai_wait_up "$WORKER_URL" 15 "agent-s-worker"; then
  dai_ok "agent-s-worker healthy at $WORKER_URL"
else
  dai_fail "agent-s-worker failed to start — last 30 lines of $DAI_LOG_DIR/agent-s-worker.log:"
  tail -30 "$DAI_LOG_DIR/agent-s-worker.log" >&2 2>/dev/null || true
  failed=1
fi

if ((failed)); then
  dai_say "" >&2
  dai_say "spine did not come up cleanly; run ./bin/doctor.sh for details" >&2
  exit 1
fi

# --- report ----------------------------------------------------------------

dai_say ""
dai_head "model-router"
dai_http "$ROUTER_URL/health" 5 | python3 -m json.tool 2>/dev/null || dai_warn "could not read router health"
dai_say ""
dai_head "agent-s-worker"
dai_http "$WORKER_URL/health" 5 | python3 -m json.tool 2>/dev/null || dai_warn "could not read worker health"

READY="$(dai_http "$ROUTER_URL/health" 5 | dai_json_get ready_for_chat || true)"
dai_say ""
if [[ "$READY" == "true" ]]; then
  dai_ok "ready_for_chat — try: ./bin/smoke.sh"
else
  dai_warn "not ready_for_chat yet: add a key to .env (see KEYS.md) or start local Ollama"
fi
dai_dim "logs: $DAI_LOG_DIR/ · stop with ./bin/stop-spine.sh"
dai_say "spine started"
