#!/usr/bin/env bash
# Fill .env with keys found in other local assistant installs — without ever
# printing a secret value.
#
#   ./bin/import-keys.sh              import into .env (creates it if needed)
#   ./bin/import-keys.sh --dry-run    report what would be imported, write nothing
#   ./bin/import-keys.sh --from-env   also accept keys already in the environment
#
# Existing non-empty values are kept.  Nothing is overwritten silently and no
# value is echoed: you see key *names* and where they came from.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

dai_need python3

DRY_RUN=0
FROM_ENV=0
for arg in "$@"; do
  case "$arg" in
    --dry-run|-n) DRY_RUN=1 ;;
    --from-env) FROM_ENV=1 ;;
    -h|--help)
      dai_usage
      exit 0
      ;;
    *) dai_die "unknown option: $arg (try --help)" ;;
  esac
done

EXAMPLE="$ROOT/.env.example"
[[ -f "$EXAMPLE" ]] || dai_die ".env.example is missing — cannot seed .env"

if [[ ! -f "$DAI_ENV_FILE" ]]; then
  if ((DRY_RUN)); then
    dai_info ".env does not exist; a dry run would create it from .env.example"
  else
    cp "$EXAMPLE" "$DAI_ENV_FILE"
    chmod 600 "$DAI_ENV_FILE"
    dai_ok "created $DAI_ENV_FILE from .env.example (mode 600)"
  fi
fi

DAI_DRY_RUN="$DRY_RUN" DAI_FROM_ENV="$FROM_ENV" python3 - <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["ROOT"])
from lib.dai.env import parse_dotenv  # noqa: E402

env_path = Path(os.environ["DAI_ENV_FILE"])
example_path = Path(os.environ["ROOT"]) / ".env.example"
dry_run = os.environ["DAI_DRY_RUN"] == "1"
from_env = os.environ["DAI_FROM_ENV"] == "1"

WANTED = [
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "OLLAMA_API_KEY",
    "CEREBRAS_API_KEY",
    "CEREBRAS_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
]

PLACEHOLDERS = {"", "changeme", "your-key-here", "none", "null", "xxx"}


def is_real(value: str) -> bool:
    value = value.strip()
    return bool(value) and value.lower() not in PLACEHOLDERS and not value.lower().startswith("your-")


home = Path.home()
SOURCES = [
    home / ".config/genie/openrouter.env",
    home / ".config/genie/.env",
    home / ".hermes/.env",
    home / "gobi-sovereign/.env",
    home / "gobi-replica/.env",
    home / "liya-ai-workstation/.env",
    home / "workspace/debian-ai-assistant/.env",
    home / ".openclaw/.env",
    home / ".config/nova/.env",
]

current = parse_dotenv(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}
found: dict[str, tuple[str, str]] = {}  # key -> (value, origin)

for key in WANTED:
    if is_real(current.get(key, "")):
        found[key] = (current[key], "already in .env")
        continue
    if from_env and is_real(os.environ.get(key, "")):
        found[key] = (os.environ[key], "environment")
        continue
    for source in SOURCES:
        if not source.is_file():
            continue
        try:
            value = parse_dotenv(source.read_text(encoding="utf-8")).get(key, "")
        except (OSError, UnicodeDecodeError):
            continue
        if is_real(value):
            found[key] = (value, str(source))
            break

imported = [(k, o) for k, (v, o) in found.items() if o != "already in .env"]
kept = [k for k, (v, o) in found.items() if o == "already in .env"]
missing = [k for k in WANTED if k not in found]

print("import summary (values are never printed)")
for key, origin in imported:
    print(f"  would set  {key:<22} from {origin}")
for key in kept:
    print(f"  keeping    {key:<22} (already set)")
for key in missing:
    print(f"  missing    {key:<22}")

if dry_run:
    print("")
    print("dry run: nothing written. Re-run without --dry-run to apply.")
    sys.exit(0)

if not imported:
    print("")
    print("nothing to import — .env is already as full as this machine can make it.")
    sys.exit(0)

# Rewrite .env preserving its layout and comments.
base_text = env_path.read_text(encoding="utf-8") if env_path.exists() else example_path.read_text(encoding="utf-8")
lines: list[str] = []
written: set[str] = set()
for line in base_text.splitlines():
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        key = stripped.split("=", 1)[0].strip().removeprefix("export ").strip()
        if key in found and found[key][1] != "already in .env":
            lines.append(f"{key}={found[key][0]}")
            written.add(key)
            continue
    lines.append(line)

for key, (value, origin) in found.items():
    if key not in written and origin != "already in .env":
        lines.append(f"{key}={value}")
        written.add(key)

env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
os.chmod(env_path, 0o600)
print("")
print(f"wrote {len(written)} value(s) to {env_path} (mode 600)")
PY

dai_say ""
dai_say "Next: ./bin/doctor.sh"
