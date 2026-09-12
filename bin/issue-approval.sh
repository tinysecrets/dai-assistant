#!/usr/bin/env bash
# Issue a one-shot approval token bound to an exact action + scope.
# Usage: bin/issue-approval.sh <action> <scope-string>
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACTION="${1:?action required (e.g. agent_s_gui_task|openrouter_paid)}"
SCOPE="${2:-}"
python3 - <<PY
import json, secrets, time
from pathlib import Path
path = Path("$ROOT/policy/approvals.json")
data = json.loads(path.read_text())
token = secrets.token_urlsafe(18)
data.setdefault("tokens", {})[token] = {
    "action": "$ACTION",
    "scope": """$SCOPE""",
    "created_at": time.time(),
    "spent": False,
}
path.write_text(json.dumps(data, indent=2) + "\n")
print(token)
PY
