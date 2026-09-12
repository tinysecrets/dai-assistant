#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$ROOT/state/model-router.pid" ]]; then
  kill "$(cat "$ROOT/state/model-router.pid")" 2>/dev/null || true
  rm -f "$ROOT/state/model-router.pid"
fi
if [[ -f "$ROOT/state/agent-s-worker.pid" ]]; then
  kill "$(cat "$ROOT/state/agent-s-worker.pid")" 2>/dev/null || true
  rm -f "$ROOT/state/agent-s-worker.pid"
fi
pkill -f "services/model-router/server.py" 2>/dev/null || true
pkill -f "services/agent-s-worker/server.py" 2>/dev/null || true
if [[ -f "$ROOT/state/xvfb.pid" ]]; then
  kill "$(cat "$ROOT/state/xvfb.pid")" 2>/dev/null || true
  rm -f "$ROOT/state/xvfb.pid"
fi
pkill -f "Xvfb :99" 2>/dev/null || true
echo "spine stopped"
