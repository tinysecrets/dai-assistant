#!/usr/bin/env bash
# dai-headquarters — launch the spine dashboard in a tmux split-screen.
#
#   ./bin/dai-headquarters.sh              # attach to (or create) the 'dai' session
#   ./bin/dai-headquarters.sh --detached   # create + dashboard, don't attach
#   ./bin/dai-headquarters.sh --kill       # kill dashboard + tmux session
#
# 2x2 grid: service health | models+cooldowns / router-logs | dashboard-status
#
# Stdlib only — needs tmux, python3, curl.  Polls the spine services directly;
#   the dashboard itself (dai dashboard) runs on :8799 and is meant to be
# opened in a browser for the full graphical view.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

router_url() {
  printf 'http://%s:%s' "$(dai_env_get DAI_ROUTER_HOST 127.0.0.1)" "$(dai_env_get DAI_ROUTER_PORT 11435)"
}
worker_url() {
  printf 'http://%s:%s' "$(dai_env_get DAI_AGENT_S_HOST 127.0.0.1)" "$(dai_env_get DAI_AGENT_S_PORT 8765)"
}

SESSION="dai"
DETACHED=0
KILL=0
for arg in "$@"; do
  case "$arg" in
    --detached|-d) DETACHED=1 ;;
    --kill) KILL=1 ;;
    -h|--help) dai_usage; exit 0 ;;
    *) dai_die "unknown option: $arg (try --help)" ;;
  esac
done

DASH_PORT="${DAI_DASHBOARD_PORT:-8799}"
DASH_URL="http://127.0.0.1:${DASH_PORT}"
ROUTER_URL="$(router_url)"
WORKER_URL="$(worker_url)"
VOICE_URL="http://$(dai_env_get DAI_VOICE_BRIDGE_HOST 127.0.0.1):$(dai_env_get DAI_VOICE_BRIDGE_PORT 8766)"
STATE="$DAI_STATE_DIR"
mkdir -p "$STATE"

if ((KILL)); then
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  dai_read_pid "dai-dashboard" | xargs -r kill 2>/dev/null || true
  rm -f "$STATE/.dai-hq-status.sh" "$STATE/.dai-hq-models.sh" "$STATE/.dai-hq-dash.sh"
  dai_say "headquarters stopped"
  exit 0
fi

# --- ensure dashboard server is up ------------------------------------------

if ! dai_http_up "$DASH_URL" 2; then
  dai_start_service "dai-dashboard" "$ROOT/lib/dai/dashboard.py" python3
fi

# --- generate pane scripts (avoids nested-quoting in tmux send-keys) --------

cat >"$STATE/.dai-hq-status.sh" <<EOF
#!/usr/bin/env bash
echo "=== service health ==="
while true; do
  for svc in "model-router:$ROUTER_URL" "agent-s-worker:$WORKER_URL" "voice-bridge:$VOICE_URL"; do
    name="\${svc%%:*}"; url="\${svc#*:}"
    if curl -sf --max-time 2 "\${url}/health" >/dev/null 2>&1; then
      echo "  OK   \$name"
    else
      echo "  --   \$name (down)"
    fi
  done
  echo
  echo "dashboard: $(curl -sf --max-time 2 "$DASH_URL/health" 2>/dev/null || echo 'down')"
  sleep 5
  clear
  echo "=== service health ==="
done
EOF

cat >"$STATE/.dai-hq-models.sh" <<EOF
#!/usr/bin/env bash
echo "=== models + cooldowns ==="
while true; do
  echo "## models (dai/auto pool)"
  curl -sf --max-time 2 "$ROUTER_URL/v1/models" 2>/dev/null | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
    for m in doc.get("data", []):
        print("  %-44s %s" % (m.get("id",""), m.get("owned_by","")), end="")
        meta = m.get("dai", {})
        if meta.get("origin"):
            print("  [%s]" % meta["origin"], end="")
        print()
except Exception:
    print("  (router not ready)")
' 2>/dev/null || echo "  (router not ready)"
  echo
  echo "## cooldowns"
  curl -sf --max-time 3 "$ROUTER_URL/v1/status/cooldowns" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "  (none)"
  sleep 8
  clear
  echo "=== models + cooldowns ==="
done
EOF

cat >"$STATE/.dai-hq-dash.sh" <<EOF
#!/usr/bin/env bash
tail -n 50 -f "$DAI_LOG_DIR"/model-router.log "$DAI_LOG_DIR"/agent-s-worker.log 2>/dev/null | \
  grep --line-buffered -E 'model-router|agent-s|ERROR|WARN|started|healthy|FAIL' || \
  tail -n 50 -f "$DAI_LOG_DIR/model-router.log" 2>/dev/null || \
  echo "no logs yet — run 'dai up'"
EOF

chmod +x "$STATE/.dai-hq-status.sh" "$STATE/.dai-hq-models.sh" "$STATE/.dai-hq-dash.sh"

# --- create tmux session (2x2) ---------------------------------------------

if tmux has-session -t "$SESSION" 2>/dev/null; then
  if ((DETACHED)); then
    dai_dim "session '$SESSION' already running (dashboard at $DASH_URL)"
    exit 0
  fi
  exec tmux attach-session -t "$SESSION"
fi

tmux new-session -d -s "$SESSION" "$STATE/.dai-hq-status.sh"

tmux split-window -h -t "$SESSION:0" "$STATE/.dai-hq-models.sh"

tmux split-window -v -t "$SESSION:0.0" "$STATE/.dai-hq-dash.sh"
tmux split-window -v -t "$SESSION:0.1"

tmux select-layout -t "$SESSION:0" tiled >/dev/null 2>&1 || true

if ((DETACHED)); then
  dai_dim "headquarters running in session '$SESSION' (dashboard at $DASH_URL)"
  exit 0
fi

dai_say "dai headquarters — Ctrl+b then d to detach, 'tmux kill-session -t $SESSION' to stop"
dai_dim "full dashboard: $DASH_URL"
exec tmux attach-session -t "$SESSION"
