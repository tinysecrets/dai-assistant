#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
STATE="/data/dai-execution-sandbox"
REAL_HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
UNIT_DIR="$REAL_HOME/.config/systemd/user"

printf '== DAI isolated OMEGA/SENTINEL execution ==\n'
command -v bwrap >/dev/null || { echo "FAIL: bubblewrap is missing"; exit 1; }
command -v prlimit >/dev/null || { echo "FAIL: prlimit is missing"; exit 1; }
command -v unshare >/dev/null || { echo "FAIL: unshare is missing"; exit 1; }
unshare --user --map-root-user true || { echo "FAIL: unprivileged user namespaces unavailable"; exit 1; }
test -x /usr/bin/python3 || { echo "FAIL: /usr/bin/python3 missing"; exit 1; }
if [[ ! -f "$STATE/state/api.token" || ! -f "$STATE/state/sentinel.token" ]]; then
    echo "FAIL: existing OMEGA/SENTINEL credentials are missing; refusing to rotate them"; exit 1
fi
test -f "$ROOT/services/execution-sandbox/server.py"
test -f "$ROOT/services/execution-sandbox/sentinel_server.py"
test -f "$ROOT/services/execution-sandbox/common.py"
test -f "$ROOT/services/execution-sandbox/sandbox.py"
test -f "$ROOT/services/execution-sandbox/sentinel.py"
test -f "$ROOT/lib/dai/execution.py"
mkdir -p "$STATE/projects" "$STATE/state" "$UNIT_DIR"
chmod 700 "$STATE" "$STATE/projects" "$STATE/state"
chmod 600 "$STATE/state/api.token" "$STATE/state/sentinel.token"
python3 -m py_compile "$ROOT/services/execution-sandbox/"*.py "$ROOT/lib/dai/execution.py" "$ROOT/bin/dai-execution"
PYTHONPATH="$ROOT/services/execution-sandbox" python3 -m unittest discover \
    -s "$ROOT/services/execution-sandbox/tests" -p 'test_*.py' -v
install -m 0644 "$ROOT/systemd/dai-execution-sandbox.service" "$UNIT_DIR/dai-execution-sandbox.service"
install -m 0644 "$ROOT/systemd/dai-sentinel.service" "$UNIT_DIR/dai-sentinel.service"
systemctl --user daemon-reload
systemctl --user enable dai-execution-sandbox.service dai-sentinel.service
systemctl --user restart dai-execution-sandbox.service
systemctl --user restart dai-sentinel.service
READY=0
for n in $(seq 1 30); do
    : "$n"
    if curl -fsS --max-time 1 http://127.0.0.1:8790/health >/dev/null 2>&1 &&
       curl -fsS --max-time 1 http://127.0.0.1:8791/health >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 1
done
if [[ "$READY" != "1" ]]; then
    systemctl --user status dai-execution-sandbox.service dai-sentinel.service --no-pager || true
    journalctl --user -u dai-execution-sandbox.service -u dai-sentinel.service -n 60 --no-pager || true
    echo "FAIL: both local execution services did not become healthy"
    exit 1
fi
cd "$ROOT"
./bin/dai-execution status
printf '\nInstalled and active: OMEGA 127.0.0.1:8790; SENTINEL 127.0.0.1:8791\n'
printf 'Persistent source/artifacts: %s/projects\n' "$STATE"
printf 'No model routing, paid provider, Docker daemon or mobile device is used.\n'
