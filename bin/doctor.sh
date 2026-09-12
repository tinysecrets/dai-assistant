#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.bun/bin:$PATH"

ok=0; warn=0; fail=0
say() { printf '%s\n' "$*"; }
pass() { say "  OK  $*"; ok=$((ok+1)); }
warn_() { say "  WARN $*"; warn=$((warn+1)); }
fail_() { say "  FAIL $*"; fail=$((fail+1)); }

say "=== Debian AI doctor ==="
say "root: $ROOT"

[[ -f "$ROOT/.env" ]] && pass ".env present" || warn_ ".env missing — cp .env.example .env (or ./bin/import-keys.sh)"
[[ -f "$ROOT/config/rotation-pool.json" ]] && pass "rotation pool" || fail_ "rotation-pool.json missing"
[[ -f "$ROOT/config/free-models.json" ]] && pass "free-models catalog" || warn_ "free-models.json missing"
[[ -x "$HOME/.bun/bin/vellum" ]] && pass "vellum CLI $($HOME/.bun/bin/vellum --help >/dev/null 2>&1 && echo ready)" || warn_ "vellum CLI not linked"
[[ -d "$ROOT/vendor/vellum-assistant" ]] && pass "vellum source linked" || fail_ "vellum vendor missing"
[[ -d "$ROOT/vendor/Agent-S" ]] && pass "Agent-S source linked" || fail_ "Agent-S vendor missing"
[[ -f "$ROOT/skills/agent-s-delegate/SKILL.md" ]] && pass "agent-s-delegate skill" || fail_ "skill missing"

# key presence only
python3 - <<PY
import os, re
from pathlib import Path
root = Path("$ROOT")
env = {}
p = root / ".env"
if p.exists():
    for line in p.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k,_,v=line.partition("=")
        env[k.strip()] = bool(v.strip())
need = ["OPENROUTER_API_KEY","GROQ_API_KEY","OLLAMA_API_KEY","CEREBRAS_API_KEY"]
have = [k for k in need if env.get(k)]
miss = [k for k in need if not env.get(k)]
print("  KEYS present:", ", ".join(have) if have else "(none yet — add to .env)")
print("  KEYS empty: ", ", ".join(miss) if miss else "(all primary filled)")
PY

if curl -sf http://127.0.0.1:11435/health >/dev/null 2>&1; then
  pass "model-router up"
  curl -sf http://127.0.0.1:11435/health | python3 -m json.tool | sed 's/^/    /'
else
  warn_ "model-router not running — ./bin/start-spine.sh"
fi

if curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; then
  pass "agent-s-worker up (dry-run)"
else
  warn_ "agent-s-worker not running — ./bin/start-spine.sh"
fi

if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  pass "local Ollama reachable"
else
  warn_ "local Ollama down (ok if using cloud keys only)"
fi

say ""
say "summary: ok=$ok warn=$warn fail=$fail"
if [[ "$fail" -gt 0 ]]; then exit 1; fi
say "READY when: spine running + at least one cloud key in .env (or local Ollama)."
say "Next: fill keys → ./bin/start-spine.sh → ./bin/hatch-vellum.sh"
