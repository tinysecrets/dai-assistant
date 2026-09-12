#!/usr/bin/env python3
"""Submit a bounded Agent S task and wait for completion."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY = ROOT / "policy" / "sovereign.json"


def load_policy() -> dict:
    path = Path(os.environ.get("DAI_POLICY", DEFAULT_POLICY))
    return json.loads(path.read_text())


def http_json(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"error": raw}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--live", action="store_true", help="Disable dry-run (requires approval token)")
    parser.add_argument("--approval-token", default=None)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    policy = load_policy()
    base = policy["agent_s"]["worker_url"].rstrip("/")
    payload = {
        "instruction": args.instruction,
        "dry_run": not args.live,
    }
    if args.max_steps is not None:
        payload["max_steps"] = args.max_steps
    if args.approval_token:
        payload["approval_token"] = args.approval_token

    code, created = http_json("POST", f"{base}/v1/tasks", payload)
    if code >= 300:
        print(json.dumps(created, indent=2))
        return 1

    task_id = created["id"]
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        code, task = http_json("GET", f"{base}/v1/tasks/{task_id}")
        if code >= 300:
            print(json.dumps(task, indent=2))
            return 1
        if task.get("status") not in ("queued", "running"):
            print(json.dumps(task, indent=2))
            return 0 if str(task.get("status", "")).endswith("complete") or task.get("status") == "dry_run_complete" else 2
        time.sleep(0.5)

    print(json.dumps({"error": "timeout", "id": task_id}, indent=2))
    return 3


if __name__ == "__main__":
    sys.exit(main())
