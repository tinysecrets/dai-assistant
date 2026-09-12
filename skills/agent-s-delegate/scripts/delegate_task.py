#!/usr/bin/env python3
"""Submit a bounded Agent S task and wait for it to reach a terminal status.

Talks to the agent-s-worker HTTP API (see docs/API.md).  Dry-run by default:
nothing touches an input device unless you pass --live *and* a valid approval
token.

Exit codes:

    0  the task reached a successful terminal status
       (succeeded, dry_run_complete)
    1  the worker refused the request or could not be queried
       (HTTP 4xx/5xx; the response body is printed)
    2  the task ran and did not succeed
       (failed, blocked, timeout, cancelled, interrupted)
    3  --timeout elapsed while the task was still queued or running
    4  usage or configuration error (no worker URL, unreadable policy)

The distinction between 1 and 2 matters: 1 means the request never became a
task, 2 means it did and the outcome was not success.

Approval tokens are credentials.  Prefer the DAI_APPROVAL_TOKEN environment
variable over --approval-token, which is visible to any local user in the
output of `ps`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY = ROOT / "policy" / "sovereign.json"
DEFAULT_WORKER = "http://127.0.0.1:8765"

# Terminal statuses, split by whether they count as success.
SUCCESS_STATUSES = ("succeeded", "dry_run_complete")
FAILURE_STATUSES = ("failed", "blocked", "timeout", "cancelled", "interrupted")
PENDING_STATUSES = ("queued", "running")

EXIT_OK = 0
EXIT_API = 1
EXIT_TASK_FAILED = 2
EXIT_WAIT_TIMEOUT = 3
EXIT_USAGE = 4


def load_policy(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (policy, error).  A missing policy file is not fatal."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"policy file not found: {path}"
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"policy file unreadable: {path} ({exc})"
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"policy file is not valid JSON: {path} ({exc})"
    return (doc if isinstance(doc, dict) else None), None


def policy_path() -> Path:
    return Path(os.environ.get("DAI_POLICY", str(DEFAULT_POLICY))).expanduser()


def load_agent_s_policy() -> Tuple[Dict[str, Any], Optional[str]]:
    """Return (agent_s_policy, note).

    Always attempted, even when --worker supplies the URL, so a corrupt or
    missing policy is still reported instead of being silently bypassed.
    """
    path = policy_path()
    policy, err = load_policy(path)
    if policy is None:
        return {}, err
    agent_s = policy.get("agent_s")
    if not isinstance(agent_s, dict):
        return {}, f"policy {path} has no agent_s object; using {DEFAULT_WORKER}"
    return agent_s, None


def worker_url(args: argparse.Namespace, agent_s_policy: Dict[str, Any]) -> str:
    """URL precedence: --worker, then DAI_AGENT_S_WORKER, then policy, then default."""
    if args.worker:
        return args.worker.rstrip("/")
    from_env = os.environ.get("DAI_AGENT_S_WORKER", "").strip()
    if from_env:
        return from_env.rstrip("/")
    url = agent_s_policy.get("worker_url")
    if isinstance(url, str) and url.strip():
        return url.strip().rstrip("/")
    return DEFAULT_WORKER


def http_json(
    method: str, url: str, body: Optional[Dict[str, Any]] = None, timeout: float = 30.0
) -> Tuple[Optional[int], Dict[str, Any]]:
    """One HTTP call.  Returns (status, payload); status is None on a transport error."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, _as_dict(json.loads(raw))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") or "{}"
        try:
            return exc.code, _as_dict(json.loads(raw))
        except json.JSONDecodeError:
            return exc.code, {"error": "non_json_response", "detail": raw[:500]}
    except urllib.error.URLError as exc:
        return None, {"error": "connection_failed", "detail": str(exc.reason)}
    except (TimeoutError, OSError) as exc:
        return None, {"error": "connection_failed", "detail": str(exc)}
    except json.JSONDecodeError:
        return None, {"error": "non_json_response", "detail": "worker returned invalid JSON"}


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {"error": "unexpected_shape", "detail": value}


def emit(payload: Any, quiet: bool, keys: Optional[Tuple[str, ...]] = None) -> None:
    """Print the result.  --quiet prints one compact line instead of the record."""
    if not quiet:
        print(json.dumps(payload, indent=2, default=str))
        return
    if not isinstance(payload, dict):
        print(payload)
        return
    chosen = keys or ("id", "status", "error", "detail", "summary")
    print(json.dumps({k: payload[k] for k in chosen if k in payload}, default=str))


def task_summary(task: Dict[str, Any]) -> Dict[str, Any]:
    """The few fields worth showing, pulled out of the full record."""
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    out = {
        "id": task.get("id"),
        "status": task.get("status"),
        "dry_run": task.get("dry_run"),
        "instruction": task.get("instruction"),
        "max_steps": task.get("max_steps"),
    }
    for key in ("summary", "error", "detail", "live_blockers", "would_run", "artifacts", "actions"):
        if key in result:
            out[key] = result[key]
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delegate_task.py",
        description="Submit a bounded Agent S task and wait for it to finish.",
        epilog=(
            "exit codes: 0 success · 1 worker refused/unreachable · 2 task did not succeed · "
            "3 wait timed out · 4 usage/config error"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--instruction", required=True, help="one bounded task (max 4000 chars)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="request a live run instead of a dry run (needs an approval token)",
    )
    parser.add_argument(
        "--approval-token",
        default=None,
        help="token from bin/issue-approval.sh; prefer $DAI_APPROVAL_TOKEN (not visible in ps)",
    )
    parser.add_argument("--max-steps", type=int, default=None, help="step budget (clamped by policy)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait for a terminal status (default 600)")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="seconds between status polls (default 0.5)")
    parser.add_argument("--worker", default=None, help=f"worker base URL (default {DEFAULT_WORKER})")
    parser.add_argument(
        "--cancel-on-timeout",
        action="store_true",
        help="cancel the task if --timeout elapses before it finishes",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="print a compact summary, not the record")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.timeout <= 0:
        print("error: --timeout must be positive", file=sys.stderr)
        return EXIT_USAGE
    if args.poll_interval <= 0:
        print("error: --poll-interval must be positive", file=sys.stderr)
        return EXIT_USAGE

    agent_s_policy, note = load_agent_s_policy()
    base = worker_url(args, agent_s_policy)
    if note:
        print(f"note: {note}", file=sys.stderr)
    if not base:
        print("error: could not resolve a worker URL", file=sys.stderr)
        return EXIT_USAGE

    instruction = args.instruction.strip()
    if not instruction:
        print("error: --instruction must not be empty", file=sys.stderr)
        return EXIT_USAGE
    if len(instruction) > 4000:
        print("error: --instruction must be 4000 characters or fewer", file=sys.stderr)
        return EXIT_USAGE

    token = args.approval_token or os.environ.get("DAI_APPROVAL_TOKEN") or ""
    if args.live and not token.strip():
        print(
            "error: --live needs an approval token.\n"
            '  TOKEN=$(./bin/issue-approval.sh agent_s_gui_task "<exact instruction>")\n'
            '  export DAI_APPROVAL_TOKEN="$TOKEN"   # preferred over --approval-token',
            file=sys.stderr,
        )
        return EXIT_USAGE

    payload: Dict[str, Any] = {"instruction": instruction, "dry_run": not args.live}
    if args.max_steps is not None:
        payload["max_steps"] = args.max_steps
    if token.strip():
        payload["approval_token"] = token.strip()

    per_call = max(5.0, min(60.0, float(args.poll_interval) * 20))
    status, created = http_json("POST", f"{base}/v1/tasks", payload, timeout=per_call)
    if status is None or status >= 300:
        emit(created, args.quiet)
        if status is None:
            print(f"error: worker unreachable at {base} — try ./bin/dai up", file=sys.stderr)
        return EXIT_API

    task_id = created.get("id")
    if not isinstance(task_id, str) or not task_id:
        emit(created, args.quiet)
        print("error: worker accepted the task but returned no id", file=sys.stderr)
        return EXIT_API

    if args.quiet:
        print(f"submitted {task_id} ({'live' if args.live else 'dry-run'})", file=sys.stderr)

    deadline = time.time() + args.timeout
    last: Dict[str, Any] = created
    while True:
        status, task = http_json("GET", f"{base}/v1/tasks/{task_id}", timeout=per_call)
        if status is None or status >= 300:
            emit(task, args.quiet)
            return EXIT_API
        last = task
        state = str(task.get("status") or "")
        if state not in PENDING_STATUSES:
            break
        if time.time() >= deadline:
            if args.cancel_on_timeout:
                http_json("POST", f"{base}/v1/tasks/{task_id}/cancel", timeout=per_call)
                print(f"cancelled {task_id} after {args.timeout}s", file=sys.stderr)
            emit(
                {
                    "error": "wait_timeout",
                    "id": task_id,
                    "status": state,
                    "detail": f"still {state} after {args.timeout}s; it may finish later",
                },
                args.quiet,
            )
            return EXIT_WAIT_TIMEOUT
        time.sleep(args.poll_interval)

    summary = task_summary(last)
    emit(summary if args.quiet else last, args.quiet)

    state = str(last.get("status") or "")
    if state in SUCCESS_STATUSES:
        return EXIT_OK
    if state in FAILURE_STATUSES:
        return EXIT_TASK_FAILED
    # An unknown status is neither success nor a recognised failure: report it
    # as a failure so a caller cannot mistake it for success.
    print(f"error: unrecognised task status {state!r}", file=sys.stderr)
    return EXIT_TASK_FAILED


if __name__ == "__main__":
    sys.exit(main())
