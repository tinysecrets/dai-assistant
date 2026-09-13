#!/usr/bin/env bash
# Shared helpers for bin/*.sh.  Source it; do not execute it.
#
#   ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#   source "$ROOT/bin/lib.sh"
#
# Nothing here prints a secret.  `.env` is parsed key-by-key instead of being
# sourced, because sourcing exports every value into every child process and
# breaks on values containing spaces or quotes.

# Guard against double-sourcing.
[[ -n "${_DAI_LIB_SH:-}" ]] && return 0
_DAI_LIB_SH=1

set -euo pipefail

# --- paths ------------------------------------------------------------------

DAI_REPO_ROOT="${DAI_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Each path honours its own name first, then a short alias, then the default —
# so a caller (or a test) can redirect state/logs/env without editing the repo.
DAI_ENV_FILE="${DAI_ENV_FILE:-${DAI_ENV:-$DAI_REPO_ROOT/.env}}"
DAI_STATE_DIR="${DAI_STATE_DIR:-$DAI_REPO_ROOT/state}"
DAI_LOG_DIR="${DAI_LOG_DIR:-$DAI_REPO_ROOT/logs}"
DAI_POLICY_FILE="${DAI_POLICY_FILE:-${DAI_POLICY:-$DAI_REPO_ROOT/policy/sovereign.json}}"
DAI_APPROVALS_FILE="${DAI_APPROVALS_FILE:-${DAI_APPROVALS:-$DAI_REPO_ROOT/policy/approvals.json}}"

export DAI_REPO_ROOT DAI_ENV_FILE DAI_STATE_DIR DAI_LOG_DIR DAI_POLICY_FILE DAI_APPROVALS_FILE

# Bun installs the vellum CLI here; keep it on PATH for every script.
export PATH="$HOME/.bun/bin:$PATH"

# --- output -----------------------------------------------------------------

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  _C_RESET=$'\033[0m'; _C_OK=$'\033[32m'; _C_WARN=$'\033[33m'
  _C_FAIL=$'\033[31m'; _C_DIM=$'\033[2m'; _C_BOLD=$'\033[1m'
else
  _C_RESET=""; _C_OK=""; _C_WARN=""; _C_FAIL=""; _C_DIM=""; _C_BOLD=""
fi

DAI_OK_COUNT=0
DAI_WARN_COUNT=0
DAI_FAIL_COUNT=0

dai_say()  { printf '%s\n' "$*"; }
dai_head() { dai_say "${_C_BOLD}$*${_C_RESET}"; }
dai_dim()  { dai_say "${_C_DIM}$*${_C_RESET}"; }

dai_ok()   { dai_say "  ${_C_OK}OK${_C_RESET}    $*"; DAI_OK_COUNT=$((DAI_OK_COUNT + 1)); }
dai_warn() { dai_say "  ${_C_WARN}WARN${_C_RESET}  $*"; DAI_WARN_COUNT=$((DAI_WARN_COUNT + 1)); }
dai_fail() { dai_say "  ${_C_FAIL}FAIL${_C_RESET}  $*"; DAI_FAIL_COUNT=$((DAI_FAIL_COUNT + 1)); }
dai_info() { dai_say "  ${_C_DIM}..${_C_RESET}    $*"; }

dai_die() {
  dai_say "${_C_FAIL}error:${_C_RESET} $*" >&2
  exit 1
}

# Print the calling script's leading comment block as its --help text.
# Derived from the file rather than a hardcoded line range, so editing a
# header comment can never silently truncate the help output.
#   dai_usage            # uses the caller's file
#   dai_usage <file>     # explicit
dai_usage() {
  local file="${1:-${BASH_SOURCE[1]:-$0}}"
  awk '
    NR == 1 && /^#!/ { next }
    /^#/ { sub(/^#[[:space:]]?/, ""); print; next }
    { exit }
  ' "$file"
}

dai_summary() {
  dai_say ""
  dai_say "summary: ok=$DAI_OK_COUNT warn=$DAI_WARN_COUNT fail=$DAI_FAIL_COUNT"
  if [[ "$DAI_FAIL_COUNT" -gt 0 ]]; then
    return 1
  fi
  return 0
}

# --- prerequisites ----------------------------------------------------------

dai_have() { command -v "$1" >/dev/null 2>&1; }

dai_need() {
  local missing=()
  local cmd
  for cmd in "$@"; do
    dai_have "$cmd" || missing+=("$cmd")
  done
  if ((${#missing[@]})); then
    dai_die "missing required command(s): ${missing[*]}"
  fi
}

# --- .env access (never source it) -----------------------------------------

# Read one key without exporting anything else.
# Precedence matches lib/dai/env.py exactly: a real environment variable wins
# over .env, which wins over the supplied default.
dai_env_get() {
  local key="$1" default="${2:-}"
  # Already set in the environment?  Use it (an operator can override one key
  # for a single command without editing .env).
  if [[ -n "${!key:-}" ]]; then
    printf '%s\n' "${!key}"
    return 0
  fi
  if [[ ! -f "$DAI_ENV_FILE" ]]; then
    printf '%s\n' "$default"
    return 0
  fi
  DAI_ENV_KEY="$key" DAI_ENV_FILE="$DAI_ENV_FILE" DAI_ENV_DEFAULT="$default" python3 -c '
import os, sys
sys.path.insert(0, os.environ["DAI_REPO_ROOT"])
from lib.dai.env import parse_dotenv
key = os.environ["DAI_ENV_KEY"]
value = parse_dotenv(open(os.environ["DAI_ENV_FILE"], encoding="utf-8").read()).get(key, "")
print(value if value else os.environ.get("DAI_ENV_DEFAULT", ""))
' 2>/dev/null || printf '%s\n' "$default"
}

# Report whether .env has a non-empty value for a key, without printing it.
dai_env_has() {
  [[ -n "$(dai_env_get "$1")" ]]
}

dai_env_ensure() {
  if [[ ! -f "$DAI_ENV_FILE" ]]; then
    if [[ -f "$DAI_REPO_ROOT/.env.example" ]]; then
      cp "$DAI_REPO_ROOT/.env.example" "$DAI_ENV_FILE"
      chmod 600 "$DAI_ENV_FILE"
      dai_warn "created $DAI_ENV_FILE from .env.example — add your keys"
    else
      dai_warn "no .env and no .env.example; services will start key-less"
    fi
  fi
}

# --- HTTP -------------------------------------------------------------------

# GET a URL, print the body, return non-zero on failure.  Always time-bounded
# so a hung service cannot stall doctor or start-spine.
dai_http() {
  local url="$1" timeout="${2:-3}"
  curl -sfS --max-time "$timeout" "$url" 2>/dev/null
}

dai_http_up() {
  dai_http "${1%/}/health" "${2:-3}" >/dev/null 2>&1
}

# Wait for /health to answer.  Usage: dai_wait_up <base-url> <seconds> <label>
dai_wait_up() {
  local base="$1" seconds="${2:-10}" label="${3:-service}"
  local deadline=$((SECONDS + seconds))
  while ((SECONDS < deadline)); do
    if dai_http_up "$base" 1; then
      return 0
    fi
    sleep 0.25
  done
  dai_say "${_C_FAIL}$label did not become healthy within ${seconds}s${_C_RESET}" >&2
  return 1
}

# Extract one JSON field with python3 — jq is not a dependency.
# Dotted path, where a numeric segment indexes a list:
#   echo '{"a":{"b":[1,2]}}' | dai_json_get a.b.1     -> 2
# Prints nothing (and exits 0) when the path is absent, so callers can use
# `value="$(... | dai_json_get x || true)"` without special-casing errors.
dai_json_get() {
  local field="$1"
  python3 -c '
import json, sys

try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(0)

value = doc
for part in sys.argv[1].split("."):
    if isinstance(value, dict):
        if part not in value:
            sys.exit(0)
        value = value[part]
    elif isinstance(value, list):
        if not part.lstrip("-").isdigit() or not -len(value) <= int(part) < len(value):
            sys.exit(0)
        value = value[int(part)]
    else:
        sys.exit(0)

if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print("")
elif isinstance(value, (dict, list)):
    print(json.dumps(value))
else:
    print(value)
' "$field" 2>/dev/null
}

# --- ports ------------------------------------------------------------------

dai_port_in_use() {
  local port="$1"
  if dai_have ss; then
    # A bare `grep && return` would abort the script under `set -e` on no match.
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"; then
      return 0
    fi
  fi
  python3 - "$port" <<'PY' 2>/dev/null
import socket, sys
port = int(sys.argv[1])
with socket.socket() as s:
    s.settimeout(0.4)
    sys.exit(0 if s.connect_ex(("127.0.0.1", port)) == 0 else 1)
PY
}

# --- service lifecycle ------------------------------------------------------

# Read and verify a pid file.  Usage: dai_read_pid <name> [cmdline-substring]
# The recorded pid is only returned when its command line still matches, so a
# recycled pid can never cause us to kill an unrelated process.
dai_read_pid() {
  local name="$1"
  # NOTE: assign `pattern` on its own line.  `local a=1 b=${a}` expands every
  # word before assigning any of them, so referencing $name there trips
  # `set -u` with "name: unbound variable".
  local pattern="${2:-}"
  [[ -n "$pattern" ]] || pattern="services/$name/server.py"
  local file="$DAI_STATE_DIR/$name.pid"
  [[ -f "$file" ]] || return 1
  local pid
  pid="$(cat "$file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || { rm -f "$file"; return 1; }
  if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o args= 2>/dev/null | grep -qF -- "$pattern"; then
    printf '%s\n' "$pid"
    return 0
  fi
  rm -f "$file"
  return 1
}

dai_start_service() {
  local name="$1" script="$2" interp="$3"
  [[ -n "$interp" ]] || interp=python3
  local log="$DAI_LOG_DIR/$name.log"
  mkdir -p "$DAI_STATE_DIR" "$DAI_LOG_DIR"
  : >"$log"
  nohup "$interp" "$script" >>"$log" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" >"$DAI_STATE_DIR/$name.pid"
  dai_dim "started $name (pid $pid) → $log"
}

dai_stop_pid() {
  local pid="$1" label="${2:-process}"
  kill "$pid" 2>/dev/null || true
  # Wait ~2s for a graceful exit before escalating to SIGKILL.
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.1
  done
  dai_warn "$label (pid $pid) ignored SIGTERM; sending SIGKILL"
  kill -9 "$pid" 2>/dev/null || true
}

# --- config validation ------------------------------------------------------

# Validate a JSON file: 0 = parses, 1 = missing or corrupt.  Callers that need
# to tell those apart should test -f first (doctor does).
dai_check_json() {
  local path="$1"
  [[ -f "$path" ]] || return 1
  python3 -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$path" >/dev/null 2>&1
}
