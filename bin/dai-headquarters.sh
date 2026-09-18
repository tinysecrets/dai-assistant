#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/bin/lib.sh"
SESSION="dai"; DETACHED=0; KILL=0
for a in "$@"; do case "$a" in --detached|-d) DETACHED=1;; --kill) KILL=1;; -h|--help) echo 'dai hq [--detached|--kill]'; exit 0;; esac; done
if ((KILL)); then tmux kill-session -t "$SESSION" 2>/dev/null || true; echo 'D-A-I headquarters stopped'; exit 0; fi
PORT="$(dai_env_get DAI_DASHBOARD_PORT 8799)"; URL="http://127.0.0.1:$PORT"; PANE="$ROOT/bin/dai-hq-pane"
if ! curl -sf --max-time 2 "$URL/health" >/dev/null 2>&1; then
  dai_start_service dai-dashboard "$ROOT/lib/dai/dashboard.py" python3
fi
if tmux has-session -t "$SESSION" 2>/dev/null; then
  if ((DETACHED)); then echo "D-A-I headquarters already running: $URL"; exit 0; fi
  exec tmux attach-session -t "$SESSION"
fi
tmux new-session -d -s "$SESSION" "$PANE status"
tmux split-window -h -t "$SESSION:0" "$PANE models"
tmux split-window -v -t "$SESSION:0.0" "$PANE voice"
tmux split-window -v -t "$SESSION:0.1" "$PANE activity"
tmux select-layout -t "$SESSION:0" tiled >/dev/null 2>&1 || true
if ((DETACHED)); then echo "D-A-I headquarters running: $URL"; exit 0; fi
echo "D-A-I headquarters • web GUI: $URL"
exec tmux attach-session -t "$SESSION"
