#!/usr/bin/env python3
"""
Review agent for omni-assistant - reviews code, configs, or plans for issues.
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
                    "You are a review agent. Given a goal and optional context "
                    "(code, config, plan), review it for correctness, security, "
                    "performance, and best practices. Output a structured review "
                    "with: Summary, Issues (Critical/Warning/Info), and "
                    "Recommendations. Be specific and actionable."
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
        print("Usage: review_agent.py <goal>")
        return 1

    goal = sys.argv[1]
    print(f"[Review Agent] Starting review: {goal}", flush=True)
    try:
        review = ask_router(f"Review the following for issues, security, and best practices:\n\n{goal}")
        print("[Review Agent] Review:\n" + review, flush=True)
        print("[Review Agent] Agent completed successfully.", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[Review Agent] FAILED: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
