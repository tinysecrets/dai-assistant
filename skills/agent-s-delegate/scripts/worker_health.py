#!/usr/bin/env python3
"""Print Agent S worker + policy health as JSON."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY = ROOT / "policy" / "sovereign.json"


def main() -> int:
    policy_path = Path(os.environ.get("DAI_POLICY", DEFAULT_POLICY))
    policy = json.loads(policy_path.read_text())
    base = policy["agent_s"]["worker_url"].rstrip("/")
    out = {"policy_path": str(policy_path), "agent_s_policy": policy["agent_s"]}
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
            out["worker"] = json.loads(resp.read().decode())
            out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["worker_error"] = str(e)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
