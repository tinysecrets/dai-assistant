#!/usr/bin/env bash
# dai-headquarters — launch the spine dashboard in a tmux split-screen.
#
#   ./bin/dai-headquarters.sh              # attach to (or create) the 'dai' session
#   ./bin/dai-headquarters.sh --detached   # create + dashboard, don't attach
#   ./bin/dai-headquarters.sh --kill       # kill dashboard + tmux session
#
# 2x2 grid: service health | models+cooldowns / voice+activity
#
# Stdlib only — needs tmux, python3, curl.  Polls the spine services directly;
#   the dashboard itself (dai dashboard) runs on :8799 and is meant to be
# opened in a browser for the full graphical view.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

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

PORT="$(dai_env_get DAI_DASHBOARD_PORT 8799)"
URL="http://127.0.0.1:$PORT"
PANE="$ROOT/bin/dai-hq-pane"

if ((KILL)); then
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  if pid="$(dai_read_pid "dai-dashboard" 2>/dev/null)"; then
    dai_stop_pid "$pid" "dai-dashboard" 2>/dev/null || true
    rm -f "$DAI_STATE_DIR/dai-dashboard.pid"
  fi
  dai_say "D-A-I headquarters stopped"
  exit 0
fi

if ! dai_have tmux; then
  dai_warn "tmux not installed — opening dashboard URL directly: $URL"
  if ! dai_http_up "$URL" 2; then
    dai_start_service "dai-dashboard" "$ROOT/lib/dai/dashboard.py" python3
    dai_wait_up "$URL" 5 "dashboard" || true
  fi
  dai_say "dashboard: $URL"
  exit 0
fi

if ! dai_http_up "$URL" 2; then
  dai_start_service "dai-dashboard" "$ROOT/lib/dai/dashboard.py" python3
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  if ((DETACHED)); then
    dai_say "D-A-I headquarters already running: $URL"
    exit 0
  fi
  exec tmux attach-session -t "$SESSION"
fi

tmux new-session -d -s "$SESSION" "$PANE status"
tmux split-window -h -t "$SESSION:0" "$PANE models"
tmux split-window -v -t "$SESSION:0.0" "$PANE voice"
tmux split-window -v -t "$SESSION:0.1" "$PANE activity"
tmux select-layout -t "$SESSION:0" tiled >/dev/null 2>&1 || true

if ((DETACHED)); then
  dai_say "D-A-I headquarters running: $URL"
  exit 0
fi

dai_say "D-A-I headquarters • web GUI: $URL (Ctrl+b d to detach)"
exec tmux attach-session -t "$SESSION"
