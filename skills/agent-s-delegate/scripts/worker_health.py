#!/usr/bin/env python3
"""Report agent-s-worker and policy health as JSON.

Answers the only question that matters before delegating: can this worker do
the work, and if not, what is missing?

Exit codes:

    0  the worker answered /health and reports ready
    1  the worker is unreachable, or answered but is not ready
    4  usage or configuration error (policy unreadable, no worker URL)

Output is scrubbed through the spine redactor, so a value that happens to look
like a secret cannot leak into a chat transcript.

Flags:
    --worker URL   worker base URL (default: policy agent_s.worker_url)
    --quiet, -q    one compact line instead of the full report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY = ROOT / "policy" / "sovereign.json"
DEFAULT_WORKER = "http://127.0.0.1:8765"

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_USAGE = 4

# Readiness fields worth surfacing; the rest of /health stays in the full report.
KEY_FIELDS = (
    "ready",
    "live_capable",
    "dry_run_default",
    "agent_s_installed",
    "display",
    "display_ready",
    "require_approval_token",
    "bind_owner_live_desktop",
    "enabled",
    "queue_depth",
    "queue_max",
)


def make_redactor():
    """Use the shared redactor when available; degrade to identity otherwise.

    A skill installed into an assistant workspace may be invoked from a
    different working directory, so the import is best-effort rather than fatal.
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from lib.dai.redact import Redactor
    except Exception:

        class _Identity:
            @staticmethod
            def redact(text):
                return text

        return _Identity()

    redactor = Redactor()
    secret_parts = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
    for name, value in os.environ.items():
        if value and len(value) >= 8 and any(part in name.upper() for part in secret_parts):
            redactor.register(value)
    return redactor


def load_policy(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (policy, error).  Never raises: a bad policy is a report, not a crash."""
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
    if not isinstance(doc, dict):
        return None, f"policy file is not a JSON object: {path}"
    return doc, None


def policy_path() -> Path:
    return Path(os.environ.get("DAI_POLICY", str(DEFAULT_POLICY))).expanduser()


def load_agent_s_policy() -> Tuple[Dict[str, Any], Optional[str]]:
    """Return (agent_s_policy, note).

    Always attempted, even when --worker supplies the URL: the policy holds the
    safety keys this report is supposed to surface, so skipping it would make
    --worker silently hide a corrupt or unsafe policy.
    """
    path = policy_path()
    policy, err = load_policy(path)
    if policy is None:
        return {}, err
    agent_s = policy.get("agent_s")
    if not isinstance(agent_s, dict) or not agent_s:
        return {}, f"policy {path} has no agent_s object; using {DEFAULT_WORKER}"
    return agent_s, None


def resolve_worker(cli_value: Optional[str], agent_s_policy: Dict[str, Any]) -> str:
    """URL precedence: --worker, then DAI_AGENT_S_WORKER, then policy, then default."""
    if cli_value:
        return cli_value.rstrip("/")
    from_env = os.environ.get("DAI_AGENT_S_WORKER", "").strip()
    if from_env:
        return from_env.rstrip("/")
    url = agent_s_policy.get("worker_url")
    if isinstance(url, str) and url.strip():
        return url.strip().rstrip("/")
    return DEFAULT_WORKER


def fetch_health(base: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (payload, error)."""
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        return None, f"worker returned HTTP {exc.code} on /health"
    except urllib.error.URLError as exc:
        return None, f"worker unreachable at {base}: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, f"worker unreachable at {base}: {exc}"
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None, "worker returned invalid JSON on /health"
    if not isinstance(doc, dict):
        return None, "worker /health is not a JSON object"
    return doc, None


def explain(health: Dict[str, Any]) -> list:
    """Turn the readiness fields into the specific things that are missing."""
    notes = []
    if not health.get("enabled"):
        notes.append("agent_s.enabled is false in policy — the worker refuses all tasks")
    if not health.get("agent_s_installed"):
        notes.append(
            "gui-agents is not installed; live runs are refused. "
            "Install with: python3 -m venv ~/.local/agent-s-venv && "
            "~/.local/agent-s-venv/bin/pip install gui-agents"
        )
    if not health.get("display_ready"):
        notes.append(
            f"display {health.get('display')} is not answering; ./bin/start-spine.sh runs Xvfb when it is installed"
        )
    if health.get("bind_owner_live_desktop"):
        notes.append("bind_owner_live_desktop is true — forbidden by design; repolicy first")
    if not health.get("live_capable"):
        if health.get("dry_run_default"):
            notes.append("dry-run works; a live run is not possible yet")
        else:
            notes.append("dry_run_default is false but live is not possible — tasks will fail")
    if health.get("config_errors"):
        notes.append(f"config errors: {health['config_errors']}")
    return notes


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="worker_health.py",
        description="Report agent-s-worker and policy health as JSON.",
        epilog="exit codes: 0 healthy · 1 unreachable or not ready · 4 usage/config error",
    )
    parser.add_argument("--worker", default=None, help=f"worker base URL (default {DEFAULT_WORKER})")
    parser.add_argument("--quiet", "-q", action="store_true", help="print one compact line")
    args = parser.parse_args(argv)

    redactor = make_redactor()
    agent_s_policy, note = load_agent_s_policy()
    base = resolve_worker(args.worker, agent_s_policy)

    report: Dict[str, Any] = {
        "worker_url": base,
        "policy_path": str(policy_path()),
        "agent_s_policy": agent_s_policy,
    }
    if note:
        report["note"] = note

    health, err = fetch_health(base)
    if health is None:
        report["ok"] = False
        report["ready"] = False
        report["worker_error"] = err
        report["next_steps"] = [
            "start the spine: ./bin/start-spine.sh",
            "or check it: ./bin/doctor.sh",
        ]
        payload = json.dumps(report, indent=2, default=str)
        print(
            redactor.redact(payload)
            if not args.quiet
            else redactor.redact(json.dumps({"ok": False, "worker_url": base, "worker_error": err}, default=str))
        )
        # A note about the policy is a config problem; a missing worker is not.
        return EXIT_USAGE if (note and err) else EXIT_UNHEALTHY

    report["ok"] = bool(health.get("ok"))
    report["ready"] = bool(health.get("ready"))
    report["worker"] = health
    report["summary"] = {key: health.get(key) for key in KEY_FIELDS if key in health}
    report["next_steps"] = explain(health)

    if args.quiet:
        compact = {
            "ok": report["ok"],
            "ready": report["ready"],
            "live_capable": health.get("live_capable"),
            "dry_run_default": health.get("dry_run_default"),
        }
        if report["next_steps"]:
            compact["blockers"] = report["next_steps"]
        print(redactor.redact(json.dumps(compact, default=str)))
    else:
        print(redactor.redact(json.dumps(report, indent=2, default=str)))

    return EXIT_OK if report["ready"] else EXIT_UNHEALTHY


if __name__ == "__main__":
    sys.exit(main())
