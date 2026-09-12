#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/logs" "$ROOT/state"

if ! curl -sf "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; then
  echo "note: local Ollama not up (cloud keys still work)" >&2
fi

"$ROOT/bin/stop-spine.sh" >/dev/null 2>&1 || true
sleep 0.3

# Dedicated agent desktop (never the owner's live desktop).
if command -v Xvfb >/dev/null 2>&1; then
  if ! xset -display :99 q >/dev/null 2>&1; then
    nohup Xvfb :99 -screen 0 1280x800x24 >"$ROOT/logs/xvfb.log" 2>&1 &
    echo $! >"$ROOT/state/xvfb.pid"
    sleep 0.5
  fi
  if xset -display :99 q >/dev/null 2>&1; then
    echo "Xvfb :99 up"
  else
    echo "warning: Xvfb :99 did not come up (agent desktop disabled)" >&2
  fi
fi

# Do not `source .env` here — values may contain spaces; Python load_dotenv handles it.
nohup python3 "$ROOT/services/model-router/server.py" >"$ROOT/logs/model-router.log" 2>&1 &
echo $! >"$ROOT/state/model-router.pid"
nohup python3 "$ROOT/services/agent-s-worker/server.py" >"$ROOT/logs/agent-s-worker.log" 2>&1 &
echo $! >"$ROOT/state/agent-s-worker.pid"

for i in $(seq 1 20); do
  if curl -sf http://127.0.0.1:11435/health >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl -sf http://127.0.0.1:11435/health >/dev/null; then
  echo "model-router failed to start. Tail:" >&2
  tail -30 "$ROOT/logs/model-router.log" >&2 || true
  exit 1
fi

echo "=== model-router ==="
curl -sf http://127.0.0.1:11435/health | python3 -m json.tool
echo "=== agent-s-worker ==="
curl -sf http://127.0.0.1:8765/health | python3 -m json.tool
echo "spine started"
