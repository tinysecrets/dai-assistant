#!/usr/bin/env bash
# Import API keys from known local files into .env without printing secret values.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/.env"
EXAMPLE="$ROOT/.env.example"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$EXAMPLE" "$ENV_FILE"
  echo "Created $ENV_FILE from example"
fi

python3 <<'PY'
import os, re
from pathlib import Path
root = Path(os.environ.get("ROOT", Path.home() / "workspace/debian-ai-assistant"))
# ROOT passed via env below
PY
ROOT="$ROOT" python3 <<'PY'
import os, re
from pathlib import Path

root = Path(os.environ["ROOT"])
env_path = root / ".env"
text = env_path.read_text() if env_path.exists() else ""

def get_val(path: Path, key: str):
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k,_,v = line.partition("=")
        if k.strip() == key:
            v=v.strip().strip('"').strip("'")
            if v and not v.startswith("your-") and v not in ("", "changeme"):
                return v
    return None

sources = [
    Path.home() / ".config/genie/openrouter.env",
    Path.home() / ".hermes/.env",
    Path.home() / "gobi-sovereign/.env",
    Path.home() / "gobi-replica/.env",
    Path.home() / "liya-ai-workstation/.env",
]

wanted = [
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "OLLAMA_API_KEY",
    "CEREBRAS_API_KEY",
    "CEREBRAS_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
]

found = {}
for key in wanted:
    # keep existing non-empty
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    if m and m.group(1).strip():
        found[key] = "KEEP"
        continue
    for src in sources:
        v = get_val(src, key)
        if v:
            found[key] = v
            break

# apply
lines = text.splitlines() if text else (root / ".env.example").read_text().splitlines()
out = []
seen = set()
for line in lines:
    if "=" in line and not line.strip().startswith("#"):
        k = line.split("=",1)[0].strip()
        if k in found and found[k] != "KEEP":
            out.append(f"{k}={found[k]}")
            seen.add(k)
            continue
        if k in found and found[k] == "KEEP":
            out.append(line)
            seen.add(k)
            continue
    out.append(line)

for k,v in found.items():
    if k not in seen and v != "KEEP":
        out.append(f"{k}={v}")

env_path.write_text("\n".join(out) + "\n")
os.chmod(env_path, 0o600)

imported = [k for k,v in found.items() if v != "KEEP"]
kept = [k for k,v in found.items() if v == "KEEP"]
print("import complete")
print("  imported:", ", ".join(imported) if imported else "(none)")
print("  already set:", ", ".join(kept) if kept else "(none)")
print("  missing:", ", ".join(k for k in wanted if k not in found) if any(k not in found for k in wanted) else "(none of primary)")
PY
echo "Done. Secrets not printed. Run: ./bin/doctor.sh"
