#!/usr/bin/env python3
"""
Plan agent for omni-assistant - generates structured plans for complex goals.
"""

import sys
import os
import json
import urllib.request
import urllib.error


def router_base() -> str:
    host = os.environ.get("DAI_ROUTER_HOST", "127.0.0.1")
    port = os.environ.get("DAI_ROUTER_PORT", "11435")
    return f"http://{host}:{port}"


def ask_router(prompt: str, timeout: float = 180.0) -> str:
    body = {
        "model": "dai/auto",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a planning agent. Given a goal, produce a clear, "
                    "numbered step-by-step plan with concrete, actionable items. "
                    "Include dependencies, estimated effort, and any risks. "
                    "Output as clean markdown or plain text."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        f"{router_base()}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"router HTTP {exc.code}: {detail[:300]}") from exc
    return (doc.get("choices") or [{}])[0].get("message", {}).get("content", "")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: plan_agent.py <goal>")
        return 1

    goal = sys.argv[1]
    print(f"[Plan Agent] Starting planning: {goal}", flush=True)
    try:
        plan = ask_router(f"Create a detailed implementation plan for: {goal}")
        print("[Plan Agent] Plan:\n" + plan, flush=True)
        print("[Plan Agent] Agent completed successfully.", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[Plan Agent] FAILED: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
