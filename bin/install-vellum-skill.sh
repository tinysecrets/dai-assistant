#!/usr/bin/env bash
# Install spine skills into a Vellum assistant workspace.
#
#   ./bin/install-vellum-skill.sh                      install every skill in skills/
#   ./bin/install-vellum-skill.sh agent-s-delegate     install one skill
#   ./bin/install-vellum-skill.sh --link               symlink instead of copy
#   ./bin/install-vellum-skill.sh --dest <dir>         install into <dir>/skills
#                                                      (a <dir> already named
#                                                       "skills" is used as-is)
#   ./bin/install-vellum-skill.sh --list               show what would be installed
#
# When no assistant workspace can be found the skills are staged under
# skills-ready/ and the exact copy command is printed.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

MODE="copy"
DEST_OVERRIDE=""
LIST_ONLY=0
NAMED=()

while (($#)); do
  case "$1" in
    --link|-l) MODE="link"; shift ;;
    --copy) MODE="copy"; shift ;;
    --dest) shift; [[ $# -ge 1 ]] || dai_die "--dest needs a directory"; DEST_OVERRIDE="$1"; shift ;;
    --list) LIST_ONLY=1; shift ;;
    -h|--help) dai_usage; exit 0 ;;
    -*) dai_die "unknown option: $1 (try --help)" ;;
    *) NAMED+=("$1"); shift ;;
  esac
done

SKILLS_DIR="$ROOT/skills"
[[ -d "$SKILLS_DIR" ]] || dai_die "no skills/ directory in $ROOT"

# --- which skills -----------------------------------------------------------

skills=()
if ((${#NAMED[@]})); then
  for name in "${NAMED[@]}"; do
    [[ -f "$SKILLS_DIR/$name/SKILL.md" ]] || dai_die "skill not found: skills/$name (no SKILL.md)"
    skills+=("$name")
  done
else
  while IFS= read -r skill_md; do
    skills+=("$(basename "$(dirname "$skill_md")")")
  done < <(find "$SKILLS_DIR" -mindepth 2 -maxdepth 2 -name SKILL.md -type f | sort)
fi

((${#skills[@]})) || dai_die "no skills found under $SKILLS_DIR"

# Every SKILL.md must have frontmatter with name + description, or Vellum will
# not load it.
for name in "${skills[@]}"; do
  md="$SKILLS_DIR/$name/SKILL.md"
  if ! head -1 "$md" | grep -q '^---$'; then
    dai_fail "$name/SKILL.md has no YAML frontmatter"
    exit 1
  fi
  for field in name description; do
    if ! awk '/^---$/{n++; next} n==1' "$md" | grep -q "^${field}:"; then
      dai_fail "$name/SKILL.md frontmatter is missing '$field'"
      exit 1
    fi
  done
done
dai_ok "${#skills[@]} skill(s) validated: ${skills[*]}"

if ((LIST_ONLY)); then
  for name in "${skills[@]}"; do
    dai_say "  $name"
    sed -n '/^---$/,/^---$/p' "$SKILLS_DIR/$name/SKILL.md" | grep -E '^(name|description):' | sed 's/^/    /'
  done
  exit 0
fi

# --- where to ---------------------------------------------------------------

dest_base=""
if [[ -n "$DEST_OVERRIDE" ]]; then
  # Accept either the workspace root or the skills dir itself: appending
  # "skills" to a path that already ends in "skills" is an easy footgun.
  if [[ "$(basename "$DEST_OVERRIDE")" == "skills" ]]; then
    dest_base="$(dirname "$DEST_OVERRIDE")"
  else
    dest_base="$DEST_OVERRIDE"
  fi
  mkdir -p "$dest_base/skills"
else
  # Durable skills live directly under each assistant dir's skills/; the nested
  # workspace/skills is a per-instance runtime mirror, not the load location.
  candidates=(
    "$HOME/.config/vellum-local/assistants"
    "$HOME/.local/share/vellum-local/assistants"
    "$HOME/.config/vellum"
    "$HOME/.local/share/vellum"
    "$HOME/Library/Application Support/vellum"
  )
  for base in "${candidates[@]}"; do
    [[ -d "$base" ]] || continue
    while IFS= read -r found; do
      [[ -n "$found" ]] || continue
      dest_base="$(dirname "$found")"
      break 2
    done < <(find "$base" -mindepth 2 -maxdepth 2 -type d -name skills 2>/dev/null | sort | head -1)
  done
fi

if [[ -z "$dest_base" ]]; then
  staging="$ROOT/skills-ready"
  rm -rf "$staging"
  mkdir -p "$staging"
  for name in "${skills[@]}"; do
    cp -a "$SKILLS_DIR/$name" "$staging/$name"
  done
  dai_warn "no Vellum assistant workspace found yet (hatch first)"
  dai_ok "staged ${#skills[@]} skill(s) under ${staging#"$ROOT"/}"
  dai_say ""
  dai_say "After ./bin/hatch-vellum.sh, copy them in:"
  dai_say "  cp -a $staging/* <assistant-workspace>/skills/"
  dai_say ""
  dai_say "or re-run with an explicit destination:"
  dai_say "  ./bin/install-vellum-skill.sh --dest <assistant-workspace>/skills"
  exit 0
fi

mkdir -p "$dest_base/skills"
for name in "${skills[@]}"; do
  src="$SKILLS_DIR/$name"
  target="$dest_base/skills/$name"
  rm -rf "$target"
  if [[ "$MODE" == "link" ]]; then
    ln -s "$src" "$target"
    dai_ok "linked $name → $target"
  else
    cp -a "$src" "$target"
    dai_ok "installed $name → $target"
  fi
done

dai_say ""
dai_dim "Ask Vellum to load the skill(s) by name: ${skills[*]}"
