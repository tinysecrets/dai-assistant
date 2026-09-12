#!/usr/bin/env bash
# Stop the spine.  Idempotent, and safe to run when nothing is up.
#
#   ./bin/stop-spine.sh              stop services + the agent display
#   ./bin/stop-spine.sh --keep-xvfb  leave the agent display running
#   ./bin/stop-spine.sh --quiet      only report errors
#
# Only processes this repo started are killed: pid files are verified against
# the command line before use, so a recycled pid cannot take out something
# unrelated.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

KEEP_XVFB=0
QUIET=0
for arg in "$@"; do
  case "$arg" in
    --keep-xvfb) KEEP_XVFB=1 ;;
    --quiet|-q) QUIET=1 ;;
    -h|--help)
      dai_usage
      exit 0
      ;;
    *) dai_die "unknown option: $arg (try --help)" ;;
  esac
done

if ((QUIET)); then
  dai_say() { :; }
  dai_dim() { :; }
  dai_info() { :; }
fi

stopped=0

for name in model-router agent-s-worker; do
  if pid="$(dai_read_pid "$name")"; then
    dai_stop_pid "$pid" "$name"
    rm -f "$DAI_STATE_DIR/$name.pid"
    dai_ok "stopped $name (pid $pid)"
    stopped=1
  else
    rm -f "$DAI_STATE_DIR/$name.pid"
    dai_info "$name was not running"
  fi
done

# Fallback for services started without a pid file (e.g. by hand).  Scoped to
# this repo's absolute path so it cannot match an unrelated process.
for name in model-router agent-s-worker; do
  script="$ROOT/services/$name/server.py"
  if pgrep -f "^python3 $script\$" >/dev/null 2>&1; then
    pkill -f "^python3 $script\$" 2>/dev/null || true
    dai_warn "stopped an orphaned $name (no pid file)"
    stopped=1
  fi
done

if ((KEEP_XVFB)); then
  dai_info "leaving the agent display running (--keep-xvfb)"
elif pid="$(dai_read_pid xvfb Xvfb || true)" && [[ -n "${pid:-}" ]]; then
  dai_stop_pid "$pid" "Xvfb"
  rm -f "$DAI_STATE_DIR/xvfb.pid"
  dai_ok "stopped the agent display (pid $pid)"
  stopped=1
else
  rm -f "$DAI_STATE_DIR/xvfb.pid"
  dai_info "no agent display was recorded"
fi

if ((stopped)); then
  dai_say "spine stopped"
else
  dai_say "nothing was running"
fi
