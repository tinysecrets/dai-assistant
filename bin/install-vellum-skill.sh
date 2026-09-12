#!/usr/bin/env bash
# Copy agent-s-delegate skill into the active Vellum assistant workspace if found.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILL_NAME="${1:-agent-s-delegate}"
SRC="$ROOT/skills/$SKILL_NAME"
[[ -d "$SRC" ]] || { echo "skill not found: $SRC" >&2; exit 1; }

# Common Vellum assistant workspaces. Durable skills live directly under each
# assistant dir's `skills/` (config over lazily-shared state); the nested
# `workspace/skills` is the per-instance runtime mirror, not the load location.
CANDIDATES=(
  "$HOME/.config/vellum-local/assistants"
  "$HOME/.local/share/vellum-local/assistants"
  "$HOME/.config/vellum"
  "$HOME/.local/share/vellum"
  "$HOME/Library/Application Support/vellum"
)

found=""
for base in "${CANDIDATES[@]}"; do
  [[ -d "$base" ]] || continue
  # Prefer a top-level skills dir per assistant over any nested one
  while IFS= read -r -d '' d; do
    found="$d"
    break 2
  done < <(find "$base" -mindepth 2 -maxdepth 2 -type d -name skills 2>/dev/null | head -1 | tr '\n' '\0')
done

# Also check vellum ps for workspace hints
if [[ -z "$found" ]] && command -v vellum >/dev/null; then
  export PATH="$HOME/.bun/bin:$PATH"
  echo "Looking up assistant workspace via vellum ps..."
  vellum ps 2>/dev/null || true
fi

if [[ -z "$found" ]]; then
  # Stage for manual copy
  DEST="$ROOT/skills-ready/$SKILL_NAME"
  rm -rf "$DEST"
  mkdir -p "$ROOT/skills-ready"
  cp -a "$SRC" "$DEST"
  echo "Vellum workspace not found yet (hatch first)."
  echo "Skill staged at: $DEST"
  echo "After hatch, copy into 003cassistant-workspace003e/skills/$SKILL_NAME"
  exit 0
fi

DEST="$found/$SKILL_NAME"
rm -rf "$DEST"
cp -a "$SRC" "$DEST"
echo "Installed skill → $DEST"
