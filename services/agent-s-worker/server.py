#!/usr/bin/env python3
"""Agent S worker — bounded GUI task queue. Dry-run until policy enables live GUI."""

from __future__ import annotations

import glob
import json
import os
import signal
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = Path(os.environ.get("DAI_ENV", ROOT / ".env"))


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


load_dotenv(ENV_PATH)

POLICY_PATH = Path(os.environ.get("DAI_POLICY", ROOT / "policy" / "sovereign.json"))
APPROVALS_PATH = Path(os.environ.get("DAI_APPROVALS", ROOT / "policy" / "approvals.json"))
STATE_DIR = Path(os.environ.get("DAI_AGENT_S_STATE", ROOT / "state" / "agent-s-tasks"))
HOST = os.environ.get("DAI_AGENT_S_HOST", "127.0.0.1")
PORT = int(os.environ.get("DAI_AGENT_S_PORT", "8765"))

STATE_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()


def load_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def save_task(task: dict[str, Any]) -> None:
    path = STATE_DIR / f"{task['id']}.json"
    with _lock:
        path.write_text(json.dumps(task, indent=2) + "\n")


def load_task(task_id: str) -> dict[str, Any] | None:
    path = STATE_DIR / f"{task_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def validate_approval(approvals: dict[str, Any], token: str | None, task: dict[str, Any]) -> str | None:
    if not token:
        return "missing_approval_token"
    rec = approvals.get("tokens", {}).get(token)
    if not rec:
        return "unknown_approval_token"
    if rec.get("spent"):
        return "approval_token_already_spent"
    if rec.get("action") != "agent_s_gui_task":
        return "approval_action_mismatch"
    if rec.get("scope") and rec.get("scope") != task.get("instruction"):
        return "approval_scope_mismatch"
    return None


def find_agent_s() -> str | None:
    candidates = [
        Path(os.environ.get("DAI_AGENT_S_VENV", Path.home() / ".local" / "agent-s-venv")) / "bin" / "agent_s",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return shutil.which("agent_s")


def ensure_display(display: str, geometry: str = "1280x800x24") -> None:
    if not display:
        return
    num = display.removeprefix(":")
    display = f":{num}"
    try:
        subprocess.run(
            ["xset", "-display", display, "q"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return
    except (OSError, subprocess.SubprocessError):
        pass
    if not shutil.which("Xvfb"):
        return
    log = STATE_DIR / "xvfb.log"
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", geometry],
        stdout=log.open("ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(40):
        ok = subprocess.run(
            ["xset", "-display", display, "q"],
            capture_output=True,
            text=True,
            check=False,
        )
        if ok.returncode == 0 or proc.poll() is not None:
            break
        time.sleep(0.25)


def run_agent_s(task: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    agent_cfg = policy["agent_s"]
    if not agent_cfg.get("enabled"):
        return {"status": "blocked", "error": "agent_s_disabled_in_policy"}

    agent_s = find_agent_s()
    if not agent_s:
        return {
            "status": "failed",
            "error": "agent_s_not_installed",
            "detail": "Install gui-agents into ~/.local/agent-s-venv (pip install gui-agents).",
        }

    display = agent_cfg.get("display") or ":99"
    ensure_display(display)

    router = (os.environ.get("DAI_MODEL_ROUTER", "http://127.0.0.1:11435") or "").rstrip("/")
    if not router:
        return {"status": "failed", "error": "model_router_not_configured"}

    grounding_model = agent_cfg.get("grounding_model", "openrouter/thinkingmachines/inkling:free")
    grounding_width = int(agent_cfg.get("grounding_width", 1280))
    grounding_height = int(agent_cfg.get("grounding_height", 800))
    max_traj = min(max(2, int(task.get("max_steps") or agent_cfg.get("max_steps_default", 15))), 20)
    timeout_s = int(agent_cfg.get("task_timeout_seconds", 600))

    venv_bin = str(Path(agent_s).parent)
    env = dict(os.environ)
    env["DISPLAY"] = display
    env["PATH"] = f"{venv_bin}:{env.get('PATH', '/usr/bin:/bin')}"

    artifacts_dir = STATE_DIR / "artifacts" / task["id"]
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        agent_s,
        "--provider", "openai",
        "--model", "dai/auto",
        "--model_url", f"{router}/v1",
        "--model_api_key", "",
        "--ground_provider", "openai",
        "--ground_url", f"{router}/v1",
        "--ground_api_key", "",
        "--ground_model", grounding_model,
        "--grounding_width", str(grounding_width),
        "--grounding_height", str(grounding_height),
        "--max_trajectory_length", str(max_traj),
        "--task", task["instruction"],
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(artifacts_dir),
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "error": "agent_s_timeout",
            "detail": f"Exceeded {timeout_s}s; {exc.stdout or ''}".strip(),
            "actions": [],
            "artifacts": [],
        }

    screenshots = sorted(glob.glob(str(artifacts_dir / "*.png")) + glob.glob(str(artifacts_dir / "*.jpg")))
    tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
    ok = proc.returncode == 0
    return {
        "status": "succeeded" if ok else "failed",
        "error": None if ok else "agent_s_nonzero_exit",
        "exit_code": proc.returncode,
        "summary": f"agent_s exit {proc.returncode}: {task['instruction']}",
        "actions": [],
        "artifacts": screenshots,
        "log_tail": tail,
        "display": display,
        "grounding_model": grounding_model,
    }


def execute_task(task_id: str) -> None:
    task = load_task(task_id)
    if not task:
        return
    policy = load_json(POLICY_PATH)
    task["status"] = "running"
    task["started_at"] = time.time()
    save_task(task)

    if policy["agent_s"].get("dry_run_default", True) or task.get("dry_run", True):
        result = {
            "status": "dry_run_complete",
            "actions": [],
            "summary": (
                f"Dry-run accepted on display {policy['agent_s'].get('display')}: "
                f"{task.get('instruction')}"
            ),
            "artifacts": [],
            "screenshots_allowed": False,
            "live_owner_desktop": False,
        }
    else:
        result = run_agent_s(task, policy)

    task["status"] = result.get("status", "failed")
    task["result"] = result
    task["finished_at"] = time.time()
    save_task(task)


class Handler(BaseHTTPRequestHandler):
    server_version = "dai-agent-s-worker/1.0"

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode() or "{}")

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        policy = load_json(POLICY_PATH)
        if self.path in ("/", "/health"):
            self._send(
                200,
                {
                    "ok": True,
                    "service": "agent-s-worker",
                    "enabled": policy["agent_s"].get("enabled", False),
                    "dry_run_default": policy["agent_s"].get("dry_run_default", True),
                    "display": policy["agent_s"].get("display"),
                    "bind_owner_live_desktop": policy["agent_s"].get("bind_owner_live_desktop", False),
                    "ready": True,
                },
            )
            return
        if self.path.startswith("/v1/tasks/"):
            task_id = self.path.rsplit("/", 1)[-1]
            task = load_task(task_id)
            if not task:
                self._send(404, {"error": "not_found"})
                return
            self._send(200, task)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/tasks":
            self._send(404, {"error": "not found"})
            return

        policy = load_json(POLICY_PATH)
        approvals = load_json(APPROVALS_PATH)
        body = self._read_json()
        instruction = (body.get("instruction") or "").strip()
        if not instruction:
            self._send(400, {"error": "instruction_required"})
            return

        if policy["agent_s"].get("bind_owner_live_desktop"):
            self._send(403, {"error": "live_desktop_forbidden_until_repolicy"})
            return

        task = {
            "id": str(uuid.uuid4()),
            "instruction": instruction,
            "max_steps": int(body.get("max_steps") or policy["agent_s"].get("max_steps_default", 15)),
            "dry_run": bool(body.get("dry_run", policy["agent_s"].get("dry_run_default", True))),
            "created_at": time.time(),
            "status": "queued",
            "result": None,
        }

        if policy["agent_s"].get("require_approval_token", True) and not task["dry_run"]:
            err = validate_approval(approvals, body.get("approval_token"), task)
            if err:
                self._send(403, {"error": err})
                return
            token = body["approval_token"]
            approvals["tokens"][token]["spent"] = True
            APPROVALS_PATH.write_text(json.dumps(approvals, indent=2) + "\n")

        save_task(task)
        threading.Thread(target=execute_task, args=(task["id"],), daemon=True).start()
        self._send(202, {"id": task["id"], "status": task["status"]})


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"agent-s-worker listening on http://{HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
