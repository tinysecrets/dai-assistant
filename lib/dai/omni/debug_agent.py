#!/usr/bin/env python3
"""
Debug agent for omni-assistant.

Performs real diagnostics: polls the spine service health endpoints, scans the
tail of recent logs for errors, then asks the model-router (dai/auto) to
interpret the evidence for the given goal. Stdlib only.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def router_base() -> str:
    host = os.environ.get("DAI_ROUTER_HOST", "127.0.0.1")
    port = os.environ.get("DAI_ROUTER_PORT", "11435")
    return f"http://{host}:{port}"


def _get_json(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def gather_health() -> dict:
    r_host = os.environ.get("DAI_ROUTER_HOST", "127.0.0.1")
    r_port = os.environ.get("DAI_ROUTER_PORT", "11435")
    w_host = os.environ.get("DAI_AGENT_S_HOST", "127.0.0.1")
    w_port = os.environ.get("DAI_AGENT_S_PORT", "8765")
    v_host = os.environ.get("DAI_VOICE_BRIDGE_HOST", "127.0.0.1")
    v_port = os.environ.get("DAI_VOICE_BRIDGE_PORT", "8766")
    return {
        "router": _get_json(f"http://{r_host}:{r_port}/health"),
        "worker": _get_json(f"http://{w_host}:{w_port}/health"),
        "voice": _get_json(f"http://{v_host}:{v_port}/health"),
    }


def scan_log_errors(max_lines: int = 40) -> dict:
    """Return the last error-ish lines from each service log."""
    log_dir = ROOT / "logs"
    findings = {}
    if not log_dir.exists():
        return findings
    needles = ("error", "traceback", "exception", "failed", "refused")
    for log in sorted(log_dir.glob("*.log")):
        try:
            lines = log.read_text(errors="replace").splitlines()
        except Exception:
            continue
        hits = [ln for ln in lines[-500:] if any(n in ln.lower() for n in needles)]
        if hits:
            findings[log.name] = hits[-max_lines:]
    return findings


def ask_router(prompt: str, timeout: float = 180.0) -> str:
    body = {
        "model": "dai/auto",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a debug agent for a local AI service spine. Given "
                    "health snapshots and log excerpts, diagnose the likely root "
                    "cause and propose concrete next steps. Be specific and terse."
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
        print("Usage: debug_agent.py <goal>")
        return 1

    goal = sys.argv[1]
    print(f"[Debug Agent] Starting debug: {goal}", flush=True)

    health = gather_health()
    print("[Debug Agent] Health snapshot:", flush=True)
    for name, h in health.items():
        state = "down" if h is None else "up"
        print(f"  {name}: {state}", flush=True)

    log_errors = scan_log_errors()
    if log_errors:
        print(f"[Debug Agent] Found error lines in {len(log_errors)} log(s).", flush=True)
    else:
        print("[Debug Agent] No error lines found in recent logs.", flush=True)

    evidence = json.dumps({"health": health, "log_errors": log_errors}, indent=2)[:6000]
    prompt = f"Debug goal: {goal}\n\nEvidence:\n{evidence}"
    try:
        diagnosis = ask_router(prompt)
        print("[Debug Agent] Diagnosis:\n" + diagnosis, flush=True)
        print("[Debug Agent] Agent completed successfully.", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        # Router unreachable is itself a useful finding; report and exit non-fatally.
        print(f"[Debug Agent] Router interpretation unavailable: {exc}", flush=True)
        print("[Debug Agent] Raw evidence above stands as the report.", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
