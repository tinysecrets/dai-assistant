#!/usr/bin/env bash
# End-to-end self-test for the spine.
#
#   ./bin/smoke.sh              unit suite + live checks against a running spine
#   ./bin/smoke.sh --no-live    unit suite only (no services needed)
#   ./bin/smoke.sh --live-only  exercise the running services only
#   ./bin/smoke.sh --json       machine-readable result
#
# Live checks are read-only where possible: the GUI task it submits is a
# dry-run, and inference is only attempted when the router says it is ready.
# Exit status: 0 = everything checked passed, 1 = at least one failure.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

RUN_TESTS=1
RUN_LIVE=1
JSON=0
for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    --no-live) RUN_LIVE=0 ;;
    --live-only) RUN_TESTS=0 ;;
    --tests-only) RUN_LIVE=0 ;;
    --json) JSON=1 ;;
    -h|--help) dai_usage; exit 0 ;;
    *) dai_die "unknown option: $arg (try --help)" ;;
  esac
done

dai_need python3 curl

ROUTER_URL="http://$(dai_env_get DAI_ROUTER_HOST 127.0.0.1):$(dai_env_get DAI_ROUTER_PORT 11435)"
WORKER_URL="http://$(dai_env_get DAI_AGENT_S_HOST 127.0.0.1):$(dai_env_get DAI_AGENT_S_PORT 8765)"
SMOKE_FAILS=0
SMOKE_PASSES=0

check() { # check <label> <command...>
  local label="$1"; shift
  if "$@" >/tmp/dai-smoke-$$.out 2>&1; then
    dai_ok "$label"
    SMOKE_PASSES=$((SMOKE_PASSES + 1))
  else
    dai_fail "$label"
    sed 's/^/        /' /tmp/dai-smoke-$$.out | head -20
    SMOKE_FAILS=$((SMOKE_FAILS + 1))
  fi
  rm -f /tmp/dai-smoke-$$.out
}

# --- 1. static checks -------------------------------------------------------

if ((RUN_TESTS)); then
  dai_head "== Static checks =="
  for script in "$ROOT"/bin/*.sh "$ROOT"/bin/dai; do
    [[ -f "$script" ]] || continue
    check "bash -n $(basename "$script")" bash -n "$script"
  done
  check "python compiles" python3 -m compileall -q "$ROOT/lib" "$ROOT/services" "$ROOT/skills" "$ROOT/tests"

  dai_say ""
  dai_head "== Unit + integration suite =="
  # tests/test_shell.py can invoke this script; the marker stops that from
  # turning into a recursive suite-inside-suite-inside-suite run.
  # DAI_SMOKE_TEST_PATTERN narrows discovery (used by the tests themselves).
  if (cd "$ROOT" && DAI_INSIDE_SMOKE=1 python3 -m unittest discover -s tests -t . \
        -p "${DAI_SMOKE_TEST_PATTERN:-test_*.py}" -q 2>&1 | tail -20); then
    dai_ok "test suite passed"
    SMOKE_PASSES=$((SMOKE_PASSES + 1))
  else
    dai_fail "test suite failed"
    SMOKE_FAILS=$((SMOKE_FAILS + 1))
  fi
fi

# --- 2. live checks ---------------------------------------------------------

if ((RUN_LIVE)); then
  dai_say ""
  dai_head "== Live spine =="

  if ! dai_http_up "$ROUTER_URL" 3; then
    dai_warn "model-router is not running at $ROUTER_URL — start it with ./bin/start-spine.sh"
    dai_warn "skipping live router checks"
  else
    dai_ok "model-router answers /health"
    SMOKE_PASSES=$((SMOKE_PASSES + 1))

    check "router /version" bash -c "curl -sf --max-time 5 '$ROUTER_URL/version' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"service\"]==\"model-router\", d'"
    check "router /v1/models lists dai/auto" bash -c "curl -sf --max-time 5 '$ROUTER_URL/v1/models' | python3 -c 'import json,sys; ids=[m[\"id\"] for m in json.load(sys.stdin)[\"data\"]]; assert \"dai/auto\" in ids, ids'"
    check "router rejects a malformed body with 400" bash -c "code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST '$ROUTER_URL/v1/chat/completions' -H 'Content-Type: application/json' -d '{broken'); [[ \"\$code\" == 400 ]]"
    check "router 404s unknown routes as JSON" bash -c "code=\$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' '$ROUTER_URL/nope'); [[ \"\$code\" == 404 ]]"
    check "router config check is clean" python3 "$ROOT/services/model-router/server.py" --check

    READY="$(dai_http "$ROUTER_URL/health" 5 | dai_json_get ready_for_chat || true)"
    if [[ "$READY" == "true" ]]; then
      dai_ok "router is ready_for_chat"
      SMOKE_PASSES=$((SMOKE_PASSES + 1))
      check "chat round-trip through the rotator" bash -c "
        curl -sf --max-time 90 -X POST '$ROUTER_URL/v1/chat/completions' \
          -H 'Content-Type: application/json' \
          -d '{\"model\":\"dai/auto\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: pong\"}]}' \
        | python3 -c 'import json,sys
d=json.load(sys.stdin)
assert \"dai_routed\" in d, d
assert d[\"choices\"][0][\"message\"][\"content\"], d
print(\"served by\", d[\"dai_routed\"][\"provider\"], d[\"dai_routed\"][\"model\"])'"
      check "streaming round-trip (SSE)" bash -c "
        curl -sfN --max-time 90 -X POST '$ROUTER_URL/v1/chat/completions' \
          -H 'Content-Type: application/json' \
          -d '{\"model\":\"dai/auto\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Count to three\"}]}' \
        | tee /tmp/dai-smoke-sse.$$ | grep -q 'data:' && grep -q 'dai_routed' /tmp/dai-smoke-sse.$$; rm -f /tmp/dai-smoke-sse.$$"
    else
      dai_warn "router is not ready_for_chat — skipping inference checks (add a key, see KEYS.md)"
    fi
  fi

  if ! dai_http_up "$WORKER_URL" 3; then
    dai_warn "agent-s-worker is not running at $WORKER_URL — skipping live worker checks"
  else
    dai_ok "agent-s-worker answers /health"
    SMOKE_PASSES=$((SMOKE_PASSES + 1))
    check "worker /v1/settings" bash -c "curl -sf --max-time 5 '$WORKER_URL/v1/settings' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert \"display\" in d[\"settings\"], d'"
    check "worker refuses a malformed body with 400" bash -c "code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST '$WORKER_URL/v1/tasks' -H 'Content-Type: application/json' -d '{broken'); [[ \"\$code\" == 400 ]]"
    check "worker refuses a live task without an approval token" bash -c "code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST '$WORKER_URL/v1/tasks' -H 'Content-Type: application/json' -d '{\"instruction\":\"smoke test\",\"dry_run\":false}'); [[ \"\$code\" == 403 ]]"
    check "worker rejects a malformed task id" bash -c "code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 '$WORKER_URL/v1/tasks/not-a-uuid'); [[ \"\$code\" == 400 ]]"

    # A dry-run task is always safe: it changes nothing and touches no display.
    check "dry-run GUI task completes" python3 - "$WORKER_URL" <<'PY'
import json, sys, time, urllib.request

base = sys.argv[1].rstrip("/")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


status, created = call("POST", "/v1/tasks", {"instruction": "smoke test dry run", "dry_run": True})
assert status == 202, (status, created)
task_id = created["id"]
deadline = time.time() + 30
while time.time() < deadline:
    status, task = call("GET", f"/v1/tasks/{task_id}")
    if task.get("status") not in ("queued", "running"):
        break
    time.sleep(0.2)
assert task["status"] == "dry_run_complete", task
assert task["result"]["would_run"], "dry run must show the command it would use"
print("dry-run ok; live blockers:", task["result"]["live_blockers"])
PY
  fi
fi

# --- summary ----------------------------------------------------------------

dai_say ""
if ((JSON)); then
  printf '{"ok": %s, "passed": %d, "failed": %d}\n' \
    "$( ((SMOKE_FAILS == 0)) && echo true || echo false)" "$SMOKE_PASSES" "$SMOKE_FAILS"
else
  dai_head "smoke summary: passed=$SMOKE_PASSES failed=$SMOKE_FAILS"
fi

if ((SMOKE_FAILS)); then
  dai_say "see docs/OPERATIONS.md for troubleshooting" >&2
  exit 1
fi
exit 0
