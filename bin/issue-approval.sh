#!/usr/bin/env bash
# Manage one-shot approval tokens for privileged actions.
#
#   ./bin/issue-approval.sh agent_s_gui_task "exact instruction"
#   ./bin/issue-approval.sh openrouter_paid  "anthropic/claude-sonnet-4.5"
#   ./bin/issue-approval.sh --list
#   ./bin/issue-approval.sh --revoke <token>
#   ./bin/issue-approval.sh --prune
#
# Options:
#   --ttl <seconds>     lifetime (default 3600; 0 = never expires)
#   --max-uses <n>      how many requests it authorises (default 1; 0 = any)
#   --note "<text>"     reminder stored with the token
#
# The token is printed on stdout (that is the point); everything else goes to
# stderr.  policy/approvals.json is created on first use with mode 0600 and is
# git-ignored.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
source "$ROOT/bin/lib.sh"

dai_need python3

CLI=(python3 -m lib.dai.approvals --file "$DAI_APPROVALS_FILE")

usage() { dai_usage; }

MODE="issue"
TTL=3600
MAX_USES=1
NOTE=""
POSITIONAL=()

while (($#)); do
  case "$1" in
    --list) MODE="list"; shift ;;
    --revoke) MODE="revoke"; shift; [[ $# -ge 1 ]] || dai_die "--revoke needs a token"; POSITIONAL+=("$1"); shift ;;
    --prune) MODE="prune"; shift ;;
    --ttl) shift; [[ $# -ge 1 ]] || dai_die "--ttl needs a value"; TTL="$1"; shift ;;
    --max-uses) shift; [[ $# -ge 1 ]] || dai_die "--max-uses needs a value"; MAX_USES="$1"; shift ;;
    --note) shift; [[ $# -ge 1 ]] || dai_die "--note needs a value"; NOTE="$1"; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) dai_die "unknown option: $1 (try --help)" ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done

cd "$ROOT"

case "$MODE" in
  list)
    "${CLI[@]}" list
    exit 0
    ;;
  prune)
    removed="$("${CLI[@]}" prune | dai_json_get removed)"
    dai_ok "pruned ${removed:-0} expired/exhausted token(s)"
    exit 0
    ;;
  revoke)
    token="${POSITIONAL[0]:-}"
    [[ -n "$token" ]] || dai_die "--revoke needs a token"
    if [[ "$("${CLI[@]}" revoke "$token" | dai_json_get revoked)" == "true" ]]; then
      dai_ok "token revoked"
    else
      dai_warn "no such token"
      exit 1
    fi
    exit 0
    ;;
esac

ACTION="${POSITIONAL[0]:-}"
SCOPE="${POSITIONAL[1]:-}"

if [[ -z "$ACTION" ]]; then
  usage >&2
  dai_die "an action is required: agent_s_gui_task | openrouter_paid"
fi
if [[ -z "$SCOPE" ]]; then
  dai_die "a scope is required — the exact instruction (or model id) this token authorises.
  Use \"*\" only if you really mean 'any value for this action'."
fi

if [[ ! -f "$DAI_APPROVALS_FILE" ]]; then
  mkdir -p "$(dirname "$DAI_APPROVALS_FILE")"
  printf '{\n  "version": 1,\n  "tokens": {}\n}\n' >"$DAI_APPROVALS_FILE"
  chmod 600 "$DAI_APPROVALS_FILE"
  # stderr: stdout of this command is the token, so `$(...)` must capture
  # nothing but the token.
  dai_dim "created $DAI_APPROVALS_FILE (mode 600, git-ignored)" >&2
fi

# Validate before issuing so a bad action/scope never writes a token.
if ! token="$("${CLI[@]}" issue "$ACTION" "$SCOPE" --ttl "$TTL" --max-uses "$MAX_USES" --note "$NOTE" 2>/dev/null)"; then
  dai_die "could not issue the token — check the action name and scope (see --help)"
fi

printf '%s\n' "$token"
dai_dim "action=$ACTION ttl=${TTL}s max_uses=$MAX_USES" >&2
dai_dim "use it once, e.g.:" >&2
if [[ "$ACTION" == "agent_s_gui_task" ]]; then
  dai_dim "  python3 skills/agent-s-delegate/scripts/delegate_task.py --instruction \"$SCOPE\" --live --approval-token <token>" >&2
else
  dai_dim "  curl -H 'X-DAI-Approval-Token: <token>' .../v1/chat/completions -d '{\"model\":\"openrouter/$SCOPE\",...}'" >&2
fi
