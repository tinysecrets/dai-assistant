#!/usr/bin/env python3
"""Agent S worker — a bounded GUI task queue.

Accepts one bounded instruction at a time and runs ``agent_s`` (``gui-agents``)
on a **dedicated** X display, never the owner's live desktop.  Dry-run is the
safe default: it validates policy, resolves the exact command line and records
the task without touching a mouse.

Safety flags come from ``policy/sovereign.json`` and cannot be weakened by
environment variables.  Host-specific details (display, model endpoints,
geometry, timeouts) can be overridden in ``.env`` — see ``docs/CONFIG.md`` for
the precedence table and the reasoning.

Endpoints are documented in ``docs/API.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.dai import (  # noqa: E402
    Config,
    HttpError,
    JsonHandler,
    Redactor,
    ServiceInfo,
    create_server,
    env_bool,
    env_int,
    env_str,
    load_dotenv,
    parse_dotenv,
    serve_forever,
)
from lib.dai.approvals import ApprovalStore  # noqa: E402
from lib.dai.httpclient import request_json  # noqa: E402
from lib.dai.jsonio import AtomicJsonStore, CachedJsonFile, atomic_write_json, load_json  # noqa: E402

SERVICE = ServiceInfo("agent-s-worker", "1.1", "docs/API.md")
VERSION = SERVICE.version

SECRET_ENV_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

# Statuses a task can hold.
QUEUED = "queued"
RUNNING = "running"
DRY_RUN_COMPLETE = "dry_run_complete"
SUCCEEDED = "succeeded"
FAILED = "failed"
BLOCKED = "blocked"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
TERMINAL = (DRY_RUN_COMPLETE, SUCCEEDED, FAILED, BLOCKED, TIMEOUT, CANCELLED, INTERRUPTED)

# Knobs that only the policy file may set: an environment variable must never
# be able to switch on live desktop control or drop the approval requirement.
SAFETY_KEYS = ("enabled", "dry_run_default", "bind_owner_live_desktop", "require_approval_token", "max_steps_hard_cap")

DEFAULT_DISPLAY = ":99"
DEFAULT_MAX_STEPS = 15
DEFAULT_MAX_STEPS_CAP = 20
DEFAULT_TIMEOUT = 600


def _cast(value: Any, kind: str) -> Any:
    try:
        if kind == "int":
            return int(value)
        if kind == "bool":
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        return str(value)
    except (TypeError, ValueError):
        return None


@dataclass
class WorkerRuntime:
    """Resolved worker state: policy, paths, queue and running-task registry."""

    root: Path
    env_path: Path
    policy_file: CachedJsonFile
    approvals: ApprovalStore
    state_dir: Path
    redactor: Redactor
    env_map: Optional[Mapping[str, str]] = None
    max_queue: int = 32
    max_tasks_kept: int = 200
    concurrency: int = 1
    started_at: float = field(default_factory=time.time)
    _queue: "queue.Queue[str]" = field(init=False)
    _tasks: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # live in-flight info
    _lock: "threading.RLock" = field(default_factory=threading.RLock)
    _stop: threading.Event = field(default_factory=threading.Event)
    _workers: List[threading.Thread] = field(default_factory=list)
    _xvfb: Optional[subprocess.Popen] = field(default=None)  # type: ignore[type-arg]

    def __post_init__(self) -> None:
        self._queue = queue.Queue(maxsize=self.max_queue)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # --- environment / policy --------------------------------------------
    def env(self) -> Mapping[str, str]:
        return self.env_map if self.env_map is not None else os.environ

    def policy(self) -> Dict[str, Any]:
        doc = self.policy_file.get()
        return doc if isinstance(doc, dict) else {}

    def agent_s_policy(self) -> Dict[str, Any]:
        value = self.policy().get("agent_s")
        return value if isinstance(value, dict) else {}

    def setting(self, key: str, env_var: str, default: Any, kind: str = "str") -> Any:
        """Resolve one setting.

        Safety keys: policy file only.  Everything else: ``.env`` first (host
        specific), then the policy file, then the built-in default.
        """
        policy_value = self.agent_s_policy().get(key)
        if key in SAFETY_KEYS:
            source = default if policy_value is None else policy_value
        else:
            env_value = (self.env().get(env_var) or "").strip()
            source = env_value if env_value else (default if policy_value is None else policy_value)
        cast = _cast(source, kind)
        return default if cast is None else cast

    def settings(self) -> Dict[str, Any]:
        router = (self.env().get("DAI_MODEL_ROUTER") or "http://127.0.0.1:11435").rstrip("/")
        cap = int(self.setting("max_steps_hard_cap", "DAI_AGENT_S_MAX_STEPS_CAP", DEFAULT_MAX_STEPS_CAP, "int"))
        cap = max(1, min(cap, 100))
        steps = int(self.setting("max_steps_default", "DAI_AGENT_S_MAX_STEPS", DEFAULT_MAX_STEPS, "int"))
        provider = self.setting("provider", "AGENT_S_PROVIDER", "openai")
        ground_provider = self.setting("ground_provider", "AGENT_S_GROUND_PROVIDER", "openai")
        return {
            "enabled": bool(self.setting("enabled", "DAI_AGENT_S_ENABLED", False, "bool")),
            "dry_run_default": bool(self.setting("dry_run_default", "DAI_AGENT_S_DRY_RUN", True, "bool")),
            "bind_owner_live_desktop": bool(self.setting("bind_owner_live_desktop", "", False, "bool")),
            "require_approval_token": bool(self.setting("require_approval_token", "", True, "bool")),
            "display": self.setting("display", "AGENT_S_DISPLAY", DEFAULT_DISPLAY),
            "geometry": self.setting("geometry", "AGENT_S_DISPLAY_GEOMETRY", "1280x800x24"),
            "provider": provider,
            "model": self.setting("model", "AGENT_S_MODEL", "dai/auto"),
            "model_url": self.setting("model_url", "AGENT_S_MODEL_URL", f"{router}/v1" if provider == "openai" else ""),
            "ground_provider": ground_provider,
            "ground_model": self.setting("grounding_model", "AGENT_S_GROUND_MODEL", "dai/vision-auto"),
            "ground_url": self.setting(
                "ground_url", "AGENT_S_GROUND_URL", f"{router}/v1" if ground_provider == "openai" else ""
            ),
            "grounding_width": int(self.setting("grounding_width", "AGENT_S_GROUNDING_WIDTH", 1280, "int")),
            "grounding_height": int(self.setting("grounding_height", "AGENT_S_GROUNDING_HEIGHT", 800, "int")),
            "task_timeout_seconds": int(self.setting("timeout_seconds", "AGENT_S_TIMEOUT", DEFAULT_TIMEOUT, "int")),
            "max_steps_default": max(1, min(steps, cap)),
            "max_steps_hard_cap": cap,
            # expanduser so /v1/settings and --check show the path that
            # agent_s_binary() actually searches, not a literal "~".
            "venv": str(Path(str(self.setting(
                "venv_path", "DAI_AGENT_S_VENV", str(Path.home() / ".local" / "agent-s-venv")
            ))).expanduser()),
            "extra_args": self.env().get("AGENT_S_EXTRA_ARGS", "").strip(),
            "allow_xvfb": str(self.env().get("AGENT_S_ALLOW_XVFB", "1")).strip().lower() in ("1", "true", "yes", "on"),
            "router_url": router,
        }

    def config_errors(self) -> List[str]:
        return [self.policy_file.error] if self.policy_file.error else []

    # --- agent_s discovery ------------------------------------------------
    def agent_s_binary(self) -> Optional[str]:
        # expanduser so the policy file can say "~/.local/agent-s-venv".
        venv = Path(str(self.settings()["venv"])).expanduser()
        for candidate in (venv / "bin" / "agent_s", venv / "bin" / "agent-s"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return shutil.which("agent_s") or shutil.which("agent-s")

    # --- display ----------------------------------------------------------
    def display_ready(self, display: str) -> bool:
        """True when an X display answers.  Never raises, never starts anything."""
        if not display:
            return False
        normalized = display if display.startswith(":") else f":{display}"
        if not shutil.which("xset"):
            # Cannot probe without x11-utils; trust DISPLAY when it matches.
            return os.environ.get("DISPLAY", "") == normalized
        try:
            probe = subprocess.run(
                ["xset", "-display", normalized, "q"], capture_output=True, text=True, timeout=5, check=False
            )
            return probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def ensure_display(self, display: str, geometry: str) -> Tuple[bool, str]:
        """Make sure ``display`` exists, starting Xvfb when that is allowed."""
        normalized = display if display.startswith(":") else f":{display}"
        if self.display_ready(normalized):
            return True, f"display {normalized} already running"
        if not self.settings()["allow_xvfb"]:
            return False, f"display {normalized} is down and AGENT_S_ALLOW_XVFB is off"
        if not shutil.which("Xvfb"):
            return False, f"display {normalized} is down and Xvfb is not installed (apt install xvfb)"
        if not shutil.which("xset"):
            return False, f"display {normalized} is down and xset is not installed (apt install x11-utils)"
        log_path = self.state_dir / "xvfb.log"
        try:
            log_handle = log_path.open("ab")
        except OSError as exc:
            return False, f"cannot write Xvfb log: {exc}"
        try:
            proc = subprocess.Popen(
                ["Xvfb", normalized, "-screen", "0", geometry],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            log_handle.close()
            return False, f"cannot start Xvfb: {exc}"
        finally:
            log_handle.close()
        with self._lock:
            self._xvfb = proc
        for _ in range(40):
            if proc.poll() is not None:
                return False, f"Xvfb exited immediately (code {proc.returncode}); see {log_path}"
            if self.display_ready(normalized):
                return True, f"started Xvfb on {normalized} ({geometry})"
            time.sleep(0.25)
        return False, f"Xvfb started but {normalized} never answered; see {log_path}"

    # --- task persistence -------------------------------------------------
    def task_path(self, task_id: str) -> Path:
        return self.state_dir / f"{task_id}.json"

    def save_task(self, task: Dict[str, Any]) -> None:
        atomic_write_json(self.task_path(str(task["id"])), task, mode=0o600)
        self.prune_tasks()

    def load_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        data = load_json(self.task_path(task_id))
        return data if isinstance(data, dict) else None

    def list_tasks(self, *, limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
        tasks: List[Dict[str, Any]] = []
        for path in self.state_dir.glob("*.json"):
            if path.name in ("xvfb.log",):
                continue
            data = load_json(path)
            if not isinstance(data, dict) or "id" not in data:
                continue
            if status and data.get("status") != status:
                continue
            tasks.append(data)
        tasks.sort(key=lambda t: float(t.get("created_at") or 0.0), reverse=True)
        return tasks[: max(1, min(limit, 500))]

    def prune_tasks(self) -> int:
        """Keep the newest ``max_tasks_kept`` records so ``state/`` cannot grow forever."""
        limit = max(10, self.max_tasks_kept)
        # Task files are named <uuid>.json; 4 hyphens distinguishes them from
        # anything else that may live in the state dir.
        paths = sorted(
            (p for p in self.state_dir.glob("*.json") if p.stem.count("-") == 4),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for path in paths[limit:]:
            task_id = path.stem
            with self._lock:
                if task_id in self._tasks:  # never delete an in-flight task
                    continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    # --- queue ------------------------------------------------------------
    def register_running(self, task_id: str, info: Dict[str, Any]) -> None:
        with self._lock:
            self._tasks[task_id] = info

    def running_info(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._tasks.get(task_id)

    def clear_running(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def running_ids(self) -> List[str]:
        with self._lock:
            return list(self._tasks)

    def submit(self, task_id: str) -> bool:
        try:
            self._queue.put_nowait(task_id)
            return True
        except queue.Full:
            return False

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def stop(self) -> None:
        """Shut down: interrupt in-flight tasks, then release workers and Xvfb."""
        self._stop.set()
        for task_id, info in list(self._tasks.items()):
            event = info.get("cancel_event")
            if isinstance(event, threading.Event):
                event.set()
            proc = info.get("process")
            if proc is not None and proc.poll() is None:
                _terminate(proc)
            # Leave an honest record instead of a task stuck in "running".
            task = self.load_task(task_id)
            if task and task.get("status") in (QUEUED, RUNNING):
                task["status"] = INTERRUPTED
                task["finished_at"] = time.time()
                task["result"] = {
                    "status": INTERRUPTED,
                    "error": "worker_shutdown",
                    "detail": "The worker stopped while this task was in flight.",
                    "actions": [],
                    "artifacts": [],
                }
                self.save_task(task)
        for _ in self._workers:
            try:
                self._queue.put_nowait("")
            except queue.Full:
                pass
        proc = self._xvfb
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass


def build_runtime(env: Optional[Mapping[str, str]] = None, *, root: Path = ROOT) -> WorkerRuntime:
    source: Mapping[str, str] = os.environ if env is None else env
    env_path = Path(str(source.get("DAI_ENV") or (root / ".env")))
    redactor = Redactor()
    for key, value in source.items():
        if value and len(str(value)) >= 8 and any(part in key.upper() for part in SECRET_ENV_PARTS):
            redactor.register(str(value))
    try:
        for key, value in parse_dotenv(env_path.read_text(encoding="utf-8")).items():
            if len(value) >= 8 and any(part in key.upper() for part in SECRET_ENV_PARTS):
                redactor.register(value)
    except (OSError, UnicodeDecodeError):
        pass

    return WorkerRuntime(
        root=root,
        env_path=env_path,
        policy_file=CachedJsonFile(Path(str(source.get("DAI_POLICY") or (root / "policy" / "sovereign.json"))), {}),
        approvals=ApprovalStore(Path(str(source.get("DAI_APPROVALS") or (root / "policy" / "approvals.json")))),
        state_dir=Path(str(source.get("DAI_AGENT_S_STATE") or (root / "state" / "agent-s-tasks"))),
        redactor=redactor,
        env_map=None if env is None else dict(env),
        # queue.Queue(maxsize=0) is *unbounded*: clamp so the queue really is
        # bounded, as the module docstring promises.
        max_queue=max(1, env_int("DAI_AGENT_S_MAX_QUEUE", 32, env=source)),
        max_tasks_kept=env_int("DAI_AGENT_S_MAX_TASKS", 200, env=source),
        concurrency=max(1, env_int("DAI_AGENT_S_CONCURRENCY", 1, env=source)),
    )


def build_config(runtime: WorkerRuntime, env: Optional[Mapping[str, str]] = None) -> Config:
    source: Mapping[str, str] = os.environ if env is None else env
    return Config(
        root=runtime.root,
        host=env_str("DAI_AGENT_S_HOST", "127.0.0.1", env=source),
        port=env_int("DAI_AGENT_S_PORT", 8765, env=source),
        env_path=runtime.env_path,
        auth_token=env_str("DAI_AGENT_S_TOKEN", "", env=source),
        max_body_bytes=env_int("DAI_MAX_BODY_BYTES", 1024 * 1024, env=source),
        request_timeout=float(runtime.settings()["task_timeout_seconds"]),
        # Honoured by the router too; a worker that logged every request while
        # the router stayed silent was an inconsistency, not a choice.
        quiet=env_bool("DAI_QUIET_LOGS", False, env=source),
        extra={"state_dir": str(runtime.state_dir), "client_timeout": env_int("DAI_CLIENT_TIMEOUT", 180, env=source)},
    )


# --- execution ------------------------------------------------------------


def build_command(runtime: WorkerRuntime, task: Dict[str, Any], settings: Mapping[str, Any], binary: str) -> List[str]:
    """The exact ``agent_s`` invocation a live run would use.

    Returned for dry-runs too, so the operator can verify flags before
    enabling live GUI control.
    """
    router_token = (runtime.env().get("DAI_ROUTER_TOKEN") or "").strip()
    api_key = (runtime.env().get("AGENT_S_API_KEY") or router_token or "dai-local").strip()
    ground_key = (runtime.env().get("AGENT_S_GROUND_API_KEY") or api_key).strip()
    max_steps = max(1, min(int(task.get("max_steps") or settings["max_steps_default"]), int(settings["max_steps_hard_cap"])))

    cmd = [
        binary,
        "--provider", str(settings["provider"]),
        "--model", str(settings["model"]),
        "--model_url", str(settings["model_url"]),
        "--model_api_key", api_key,
        "--ground_provider", str(settings["ground_provider"]),
        "--ground_url", str(settings["ground_url"]),
        "--ground_api_key", ground_key,
        "--ground_model", str(settings["ground_model"]),
        "--grounding_width", str(int(settings["grounding_width"])),
        "--grounding_height", str(int(settings["grounding_height"])),
        "--max_trajectory_length", str(max_steps),
        "--task", str(task["instruction"]),
    ]
    extra = str(settings.get("extra_args") or "")
    if extra:
        cmd.extend(shlex.split(extra))
    return cmd


def redacted_command(cmd: List[str], redactor: Redactor) -> List[str]:
    out: List[str] = []
    skip_next = False
    for part in cmd:
        if skip_next:
            out.append("[REDACTED]")
            skip_next = False
            continue
        out.append(part)
        if part in ("--model_api_key", "--ground_api_key"):
            skip_next = True
    return [redactor.redact(p) for p in out]


def router_ready(runtime: WorkerRuntime, settings: Mapping[str, Any]) -> Tuple[bool, str]:
    """Fail fast when inference is unreachable — the old behaviour was a 600 s timeout."""
    url = str(settings.get("model_url") or "")
    if not url:
        return False, "no model_url configured (set AGENT_S_MODEL_URL or DAI_MODEL_ROUTER)"
    base = url.replace("/v1", "") if url.endswith("/v1") else url
    code, payload = request_json("GET", f"{base}/health", timeout=5.0, redactor=runtime.redactor)
    if code == 200 and isinstance(payload, dict):
        if payload.get("ready_for_chat") is False:
            return False, f"model-router is up but not ready_for_chat: {payload.get('notes') or payload.get('keys_needed')}"
        return True, "model-router ready"
    return False, f"model-router not reachable at {base}/health (status {code})"


def run_agent_s(runtime: WorkerRuntime, task: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a live GUI task.  Every failure mode returns a structured result."""
    settings = runtime.settings()
    if not settings["enabled"]:
        return {"status": BLOCKED, "error": "agent_s_disabled_in_policy", "actions": [], "artifacts": []}
    if settings["bind_owner_live_desktop"]:
        return {"status": BLOCKED, "error": "live_desktop_forbidden_until_repolicy", "actions": [], "artifacts": []}

    binary = runtime.agent_s_binary()
    if not binary:
        return {
            "status": FAILED,
            "error": "agent_s_not_installed",
            "detail": f"gui-agents not found (looked in {settings['venv']}/bin and PATH). "
            "Install with: python3 -m venv ~/.local/agent-s-venv && ~/.local/agent-s-venv/bin/pip install gui-agents",
            "actions": [],
            "artifacts": [],
        }

    display = str(settings["display"])
    display_ok, display_note = runtime.ensure_display(display, str(settings["geometry"]))
    if not display_ok:
        return {
            "status": BLOCKED,
            "error": "display_unavailable",
            "detail": display_note,
            "display": display,
            "actions": [],
            "artifacts": [],
        }

    reachable, router_note = router_ready(runtime, settings)
    if not reachable:
        return {
            "status": BLOCKED,
            "error": "model_router_not_ready",
            "detail": router_note,
            "actions": [],
            "artifacts": [],
        }

    artifacts_dir = runtime.state_dir / "artifacts" / str(task["id"])
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_command(runtime, task, settings, binary)

    env = dict(os.environ)
    env["DISPLAY"] = display
    env["PATH"] = f"{Path(binary).parent}:{env.get('PATH', '/usr/bin:/bin')}"
    env.setdefault("XDG_RUNTIME_DIR", f"/tmp/dai-runtime-{os.getuid()}")
    Path(env["XDG_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)

    stdout_path = artifacts_dir / "agent_s.stdout.log"
    stderr_path = artifacts_dir / "agent_s.stderr.log"
    timeout_s = int(settings["task_timeout_seconds"])
    started = time.monotonic()

    info = runtime.running_info(str(task["id"])) or {}
    try:
        with stdout_path.open("wb") as out_fh, stderr_path.open("wb") as err_fh:
            proc = subprocess.Popen(
                cmd, stdout=out_fh, stderr=err_fh, env=env, cwd=str(artifacts_dir), start_new_session=True
            )
    except OSError as exc:
        return {
            "status": FAILED,
            "error": "agent_s_launch_failed",
            "detail": runtime.redactor.redact(str(exc)),
            "command": redacted_command(cmd, runtime.redactor),
            "actions": [],
            "artifacts": [],
        }

    info["process"] = proc
    info["started_at"] = started
    runtime.register_running(str(task["id"]), info)
    cancel_event = info.get("cancel_event")

    # Wait with a deadline, honouring cancellation.  Owner cancel wins over a
    # timeout so the record says what actually happened.
    returncode: Optional[int] = None
    cancelled = False
    timed_out = False
    try:
        while True:
            if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
                cancelled = True
                _terminate(proc)
                returncode = proc.returncode
                break
            try:
                returncode = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - started > timeout_s:
                    timed_out = True
                    _terminate(proc)
                    returncode = proc.returncode
                    break
    finally:
        runtime.clear_running(str(task["id"]))

    duration_ms = (time.monotonic() - started) * 1000.0
    artifacts = sorted(
        str(p) for p in artifacts_dir.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
    )
    tail = _tail(stdout_path, 1500) + _tail(stderr_path, 1500)
    base = {
        "command": redacted_command(cmd, runtime.redactor),
        "display": display,
        "display_note": display_note,
        "router_note": router_note,
        "grounding_model": str(settings["ground_model"]),
        "model": str(settings["model"]),
        "duration_ms": round(duration_ms, 1),
        "artifacts": artifacts,
        "logs": {"stdout": str(stdout_path), "stderr": str(stderr_path)},
        "log_tail": runtime.redactor.redact(tail),
        # agent_s does not emit a machine-readable action log; the transcript
        # in logs.stdout is the record.  Claiming actions we did not parse
        # would be a lie, so this stays empty.
        "actions": [],
    }
    if cancelled:
        return {"status": CANCELLED, "error": "cancelled_by_owner", "exit_code": returncode, **base}
    if timed_out:
        return {
            "status": TIMEOUT,
            "error": "agent_s_timeout",
            "detail": f"exceeded {timeout_s}s; killed the process group",
            "exit_code": returncode,
            **base,
        }
    base["exit_code"] = returncode
    if returncode == 0:
        return {"status": SUCCEEDED, "error": None, "summary": f"agent_s completed: {task['instruction']}", **base}
    return {
        "status": FAILED,
        "error": "agent_s_nonzero_exit",
        "summary": f"agent_s exited {returncode}: {task['instruction']}",
        "detail": "See log_tail; a grounding model that cannot see the screen is the usual cause.",
        **base,
    }


def _terminate(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
    """SIGTERM the whole process group, escalating to SIGKILL."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (OSError, ProcessLookupError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _tail(path: Path, limit: int) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def dry_run_result(runtime: WorkerRuntime, task: Dict[str, Any]) -> Dict[str, Any]:
    """What a live run would do, without touching input devices."""
    settings = runtime.settings()
    binary = runtime.agent_s_binary()
    cmd = build_command(runtime, task, settings, binary or "agent_s")
    checks = {
        "policy_enabled": bool(settings["enabled"]),
        "agent_s_installed": bool(binary),
        "agent_s_path": binary,
        "display": str(settings["display"]),
        "display_ready": runtime.display_ready(str(settings["display"])),
    }
    reachable, router_note = router_ready(runtime, settings)
    checks["model_router_ready"] = reachable
    checks["model_router_note"] = router_note
    blockers = [name for name, ok in checks.items() if ok is False]
    return {
        "status": DRY_RUN_COMPLETE,
        "actions": [],
        "artifacts": [],
        "screenshots_allowed": False,
        "live_owner_desktop": False,
        "summary": f"Dry-run accepted on display {settings['display']}: {task['instruction']}",
        "would_run": redacted_command(cmd, runtime.redactor),
        "checks": checks,
        "live_blockers": blockers,
        "hint": "Re-run with --live and an approval token once live_blockers is empty: "
        "bin/issue-approval.sh agent_s_gui_task \"<exact instruction>\"",
    }


def execute_task(runtime: WorkerRuntime, task_id: str) -> None:
    task = runtime.load_task(task_id)
    if not task:
        return
    if task.get("status") == CANCELLED:
        return

    settings = runtime.settings()
    task["status"] = RUNNING
    task["started_at"] = time.time()
    runtime.save_task(task)
    runtime.register_running(task_id, {"cancel_event": threading.Event(), "started_at": time.time()})

    try:
        if task.get("dry_run", True):
            result = dry_run_result(runtime, task)
        else:
            result = run_agent_s(runtime, task)
    except Exception as exc:  # noqa: BLE001 - a task must never kill the worker
        result = {
            "status": FAILED,
            "error": "worker_exception",
            "detail": runtime.redactor.redact(str(exc)),
            "actions": [],
            "artifacts": [],
        }
    finally:
        runtime.clear_running(task_id)

    task["status"] = str(result.get("status") or FAILED)
    task["result"] = result
    task["finished_at"] = time.time()
    runtime.save_task(task)


def worker_loop(runtime: WorkerRuntime) -> None:
    """Serial consumer: one GUI task at a time, because the display is exclusive."""
    while not runtime._stop.is_set():
        try:
            task_id = runtime._queue.get(timeout=0.25)
        except queue.Empty:
            continue
        if not task_id:
            continue
        try:
            execute_task(runtime, task_id)
        finally:
            runtime._queue.task_done()


def start_workers(runtime: WorkerRuntime) -> None:
    for index in range(runtime.concurrency):
        thread = threading.Thread(target=worker_loop, args=(runtime,), name=f"agent-s-worker-{index}", daemon=True)
        thread.start()
        runtime._workers.append(thread)


# --- HTTP -----------------------------------------------------------------

UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def valid_task_id(value: str) -> bool:
    return bool(re.match(UUID_RE, value or ""))


class WorkerHandler(JsonHandler):
    runtime: WorkerRuntime = None  # type: ignore[assignment]

    @property
    def rt(self) -> WorkerRuntime:
        return type(self).runtime

    def dispatch(self, method: str, path: str, query: Dict[str, str]) -> None:
        if method in ("GET", "HEAD"):
            self.dispatch_get(path, query)
        elif method == "POST":
            self.dispatch_post(path, query)
        else:
            raise HttpError(405, "method_not_allowed", f"{method} is not supported for {path}", headers={"Allow": "GET, HEAD, POST"})

    # --- GET --------------------------------------------------------------
    def dispatch_get(self, path: str, query: Mapping[str, str]) -> None:
        rt = self.rt
        if path in ("/", "/health"):
            self.send_json(200, self.health_payload())
            return
        if path == "/version":
            self.send_json(200, SERVICE.as_dict())
            return
        if path == "/ready":
            payload = self.health_payload()
            self.send_json(200 if payload["ready"] else 503, payload)
            return
        if path == "/v1/tasks":
            self.check_auth()
            limit = max(1, min(int(query.get("limit") or 50), 500))
            tasks = rt.list_tasks(limit=limit, status=(query.get("status") or None))
            self.send_json(
                200,
                {
                    "object": "list",
                    "count": len(tasks),
                    "queue_depth": rt.queue_depth(),
                    "running": rt.running_ids(),
                    "data": [summary(t) for t in tasks],
                },
            )
            return
        if path == "/v1/settings":
            self.check_auth()
            self.send_json(200, self.settings_payload())
            return
        if path.startswith("/v1/tasks/"):
            self.check_auth()
            remainder = path[len("/v1/tasks/") :]
            task_id, _, sub = remainder.partition("/")
            if not valid_task_id(task_id):
                raise HttpError(400, "invalid_task_id", "Task ids are UUIDs returned by POST /v1/tasks.")
            task = rt.load_task(task_id)
            if not task:
                raise HttpError(404, "not_found", f"No task {task_id}")
            if not sub:
                self.send_json(200, task)
                return
            if sub.startswith("artifacts/"):
                self.send_artifact(task, sub[len("artifacts/") :])
                return
            raise HttpError(404, "not_found", f"No sub-resource '{sub}' for a task")
        raise HttpError(404, "not_found", f"No route for GET {path}. Try /health or /v1/tasks")

    def send_artifact(self, task: Dict[str, Any], name: str) -> None:
        """Serve one recorded artifact.  The path must already be in the task record."""
        allowed = {Path(str(p)).name: str(p) for p in ((task.get("result") or {}).get("artifacts") or [])}
        if name not in allowed:
            raise HttpError(404, "not_found", f"Artifact '{name}' is not part of this task's record")
        path = Path(allowed[name])
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise HttpError(410, "artifact_gone", f"Artifact file is unreadable: {exc}") from None
        suffix = path.suffix.lower().lstrip(".")
        content_type = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(suffix, "png")
        body = data
        self._response_status = 200
        self._start(
            200,
            {
                "Content-Type": f"image/{content_type}",
                "Content-Length": str(len(body)),
                "Content-Disposition": f'inline; filename="{path.name}"',
                "Cache-Control": "no-store",
            },
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def health_payload(self) -> Dict[str, Any]:
        rt = self.rt
        settings = rt.settings()
        binary = rt.agent_s_binary()
        display_ok = runtime_display_probe(rt, settings)
        live_capable = bool(settings["enabled"] and binary and display_ok and not settings["bind_owner_live_desktop"])
        return {
            "ok": True,
            "service": SERVICE.name,
            "version": VERSION,
            "enabled": bool(settings["enabled"]),
            "dry_run_default": bool(settings["dry_run_default"]),
            "display": str(settings["display"]),
            "display_ready": display_ok,
            "bind_owner_live_desktop": bool(settings["bind_owner_live_desktop"]),
            "require_approval_token": bool(settings["require_approval_token"]),
            "agent_s_installed": bool(binary),
            "agent_s_path": binary,
            "live_capable": live_capable,
            "grounding_model": str(settings["ground_model"]),
            "model": str(settings["model"]),
            "model_url": str(settings["model_url"]),
            "queue_depth": rt.queue_depth(),
            "queue_max": rt.max_queue,
            "running": rt.running_ids(),
            "concurrency": rt.concurrency,
            "approvals_file": str(rt.approvals.path),
            "approvals_present": rt.approvals.path.exists(),
            "uptime_seconds": round(time.time() - rt.started_at, 1),
            "config_errors": rt.config_errors(),
            # Honest readiness: dry-run always works, live needs the pieces above.
            "ready": bool(settings["enabled"]) and (bool(settings["dry_run_default"]) or live_capable),
        }

    def settings_payload(self) -> Dict[str, Any]:
        rt = self.rt
        settings = dict(rt.settings())
        return {
            "settings": settings,
            "safety_keys_policy_only": list(SAFETY_KEYS),
            "policy_path": str(rt.policy_file.path),
            "env_file": str(rt.env_path),
            "state_dir": str(rt.state_dir),
            "config_errors": rt.config_errors(),
        }

    # --- POST -------------------------------------------------------------
    def dispatch_post(self, path: str, query: Mapping[str, str]) -> None:
        rt = self.rt
        if path == "/v1/tasks":
            self.create_task()
            return
        if path.startswith("/v1/tasks/") and path.endswith("/cancel"):
            self.check_auth()
            task_id = path[len("/v1/tasks/") : -len("/cancel")]
            if not valid_task_id(task_id):
                raise HttpError(400, "invalid_task_id", "Task ids are UUIDs returned by POST /v1/tasks.")
            self.cancel_task(task_id)
            return
        raise HttpError(404, "not_found", f"No route for POST {path}")

    def create_task(self) -> None:
        rt = self.rt
        self.check_auth()
        body = self.read_json(required=True)
        settings = rt.settings()

        instruction = str(body.get("instruction") or "").strip()
        if not instruction:
            raise HttpError(400, "instruction_required", "Send {'instruction': '<one bounded task>'}.")
        if len(instruction) > 4000:
            raise HttpError(400, "instruction_too_long", "Keep the instruction under 4000 characters.")
        if not settings["enabled"]:
            raise HttpError(
                403,
                "agent_s_disabled_in_policy",
                "policy/sovereign.json has agent_s.enabled=false; the worker is not accepting tasks.",
            )
        if settings["bind_owner_live_desktop"]:
            raise HttpError(403, "live_desktop_forbidden_until_repolicy", "Binding the owner's live desktop is forbidden.")

        if rt.queue_depth() >= rt.max_queue:
            raise HttpError(
                429,
                "queue_full",
                f"Task queue is at capacity ({rt.max_queue}); wait for running tasks to finish.",
                extra={"queue_depth": rt.queue_depth(), "running": rt.running_ids()},
            )

        dry_run = bool(body.get("dry_run", settings["dry_run_default"]))
        max_steps_raw = body.get("max_steps")
        try:
            max_steps = int(max_steps_raw) if max_steps_raw is not None else int(settings["max_steps_default"])
        except (TypeError, ValueError):
            raise HttpError(400, "invalid_max_steps", "'max_steps' must be an integer.") from None
        max_steps = max(1, min(max_steps, int(settings["max_steps_hard_cap"])))

        task: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "instruction": instruction,
            "max_steps": max_steps,
            "dry_run": dry_run,
            "created_at": time.time(),
            "status": QUEUED,
            "result": None,
        }

        if not dry_run:
            token = body.get("approval_token")
            if settings["require_approval_token"]:
                err, detail = rt.approvals.consume("agent_s_gui_task", str(token) if token else None, instruction)
                if err:
                    raise HttpError(403, err, detail or "Live GUI tasks need a valid approval token.", extra={"task_id": task["id"]})
            task["approval"] = {"token_used": True, "action": "agent_s_gui_task"}

        rt.save_task(task)
        if not rt.submit(task["id"]):
            task["status"] = FAILED
            task["result"] = {
                "status": FAILED,
                "error": "queue_full",
                "detail": f"Task queue is at capacity ({rt.max_queue}); wait for running tasks to finish.",
                "actions": [],
                "artifacts": [],
            }
            rt.save_task(task)
            raise HttpError(429, "queue_full", "Too many pending tasks; retry shortly.", extra={"id": task["id"]})

        self.send_json(202, {"id": task["id"], "status": task["status"], "dry_run": dry_run})

    def cancel_task(self, task_id: str) -> None:
        rt = self.rt
        task = rt.load_task(task_id)
        if not task:
            raise HttpError(404, "not_found", f"No task {task_id}")
        if task.get("status") in TERMINAL:
            self.send_json(200, {"id": task_id, "status": task.get("status"), "cancelled": False, "detail": "already finished"})
            return
        info = rt.running_info(task_id)
        if info is None:
            task["status"] = CANCELLED
            task["result"] = {
                "status": CANCELLED,
                "error": "cancelled_before_start",
                "actions": [],
                "artifacts": [],
            }
            task["finished_at"] = time.time()
            rt.save_task(task)
            self.send_json(200, {"id": task_id, "status": CANCELLED, "cancelled": True})
            return
        event = info.get("cancel_event")
        if isinstance(event, threading.Event):
            event.set()
        self.send_json(202, {"id": task_id, "status": task.get("status"), "cancelling": True})


def runtime_display_probe(rt: WorkerRuntime, settings: Mapping[str, Any]) -> bool:
    """Cheap display check for ``/health`` (never starts Xvfb)."""
    return rt.display_ready(str(settings["display"]))


def summary(task: Mapping[str, Any]) -> Dict[str, Any]:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    return {
        "id": task.get("id"),
        "instruction": task.get("instruction"),
        "status": task.get("status"),
        "dry_run": task.get("dry_run"),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "error": result.get("error"),
        "summary": result.get("summary"),
        "artifacts": len(result.get("artifacts") or []),
    }


def check_configuration(runtime: WorkerRuntime) -> Dict[str, Any]:
    """Validate configuration without serving.  Used by ``--check`` and doctor.

    Two categories, deliberately kept apart:

    * ``problems`` — configuration or safety faults.  ``--check`` exits 1.
    * ``warnings`` — optional runtime dependencies that are absent.

    A fresh clone legitimately has no ``agent_s`` binary and no X display, and
    dry-run still works, so those must never fail a configuration check (or
    CI).  Reporting them as problems forced every caller to re-classify the
    strings afterwards.
    """
    problems: List[str] = []
    warnings: List[str] = []
    settings = runtime.settings()
    policy = runtime.policy()
    if not policy:
        problems.append(f"policy empty or unreadable: {runtime.policy_file.path}")
    if not isinstance(policy.get("agent_s"), dict):
        problems.append("policy has no 'agent_s' object; built-in defaults are in use")
    if settings["bind_owner_live_desktop"]:
        problems.append("agent_s.bind_owner_live_desktop is true — live desktop control is forbidden by design")
    if not settings["dry_run_default"]:
        problems.append("agent_s.dry_run_default is false: tasks are LIVE unless they ask for a dry run")
    problems.extend(runtime.config_errors())

    if not runtime.agent_s_binary():
        warnings.append(
            f"agent_s binary not found (venv: {settings['venv']}) — live GUI tasks will be "
            "refused with a reason; dry-run still works"
        )
    if not runtime.display_ready(str(settings["display"])):
        warnings.append(
            f"display {settings['display']} is not answering (start-spine.sh runs Xvfb when installed)"
        )

    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "settings": settings,
        "approvals_file": str(runtime.approvals.path),
        "approvals_present": runtime.approvals.path.exists(),
        "state_dir": str(runtime.state_dir),
        "queued_tasks": len(runtime.list_tasks(limit=500, status=QUEUED)),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-s-worker", description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    parser.add_argument("--print-config", action="store_true", help="print resolved (secret-free) config and exit")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{SERVICE.name} {VERSION}")
        return 0

    load_dotenv(Path(os.environ.get("DAI_ENV", str(ROOT / ".env"))))
    runtime = build_runtime()
    config = build_config(runtime)

    if args.check:
        report = check_configuration(runtime)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if args.print_config:
        print(json.dumps({"config": config.public_dict(), **runtime.settings()}, indent=2, default=str))
        return 0

    httpd = create_server(
        config,
        WorkerHandler,
        service=SERVICE,
        redactor=runtime.redactor,
        bind={"runtime": runtime, "timeout": config.extra.get("client_timeout", 180)},
    )
    start_workers(runtime)
    settings = runtime.settings()
    banner = "\n".join(
        [
            f"agent-s-worker {VERSION} listening on http://{config.host}:{config.port}",
            f"enabled={settings['enabled']} dry_run_default={settings['dry_run_default']} display={settings['display']}",
            f"agent_s={runtime.agent_s_binary() or 'NOT INSTALLED'} queue_max={runtime.max_queue} concurrency={runtime.concurrency}",
        ]
    )
    report = check_configuration(runtime)
    for problem in report["problems"]:
        banner += f"\nPROBLEM: {problem}"
    for warning in report["warnings"]:
        banner += f"\nWARNING: {warning}"
    serve_forever(httpd, on_shutdown=runtime.stop, banner=banner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
