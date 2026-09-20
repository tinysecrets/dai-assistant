#!/usr/bin/env python3
"""
Research agent for omni-assistant.

Performs real research by asking the model-router (dai/auto) to gather and
synthesize findings for a goal. Stdlib only; talks to the same
/v1/chat/completions endpoint used by `bin/dai chat` and the dashboard.
"""

import json
import os
import sys
import urllib.error
import urllib.request


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
                    "You are a research agent. Given a goal, produce a concise, "
                    "well-structured briefing: key findings, notable facts, and "
                    "open questions. Be factual; flag uncertainty explicitly."
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
        print("Usage: research_agent.py <goal>")
        return 1

    goal = sys.argv[1]
    print(f"[Research Agent] Starting research: {goal}", flush=True)
    try:
        findings = ask_router(f"Research goal: {goal}")
        print("[Research Agent] Findings:\n" + findings, flush=True)
        print("[Research Agent] Agent completed successfully.", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[Research Agent] FAILED: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
