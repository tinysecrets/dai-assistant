#!/usr/bin/env python3
"""Omni Healing Engine - Detection, diagnosis, and fixing of issues."""

from __future__ import annotations

import importlib
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional


class OmniHealer:
    """Self-healing engine for dai-assistant spine."""

    def __init__(self, dai_root: Path):
        self.dai_root = Path(dai_root).resolve()
        self.state_dir = self.dai_root / "state/omni"
        self.logs_dir = self.dai_root / "logs"
        self.memory = None  # Will be set by caller if needed
        self.healing_interval = 60  # seconds
        policy_path = self.dai_root / "policy/sovereign.json"
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8")).get("autonomy", {})
            limits = policy.get("limits", {})
        except Exception:
            policy = {}
            limits = {}
        self.max_steps = max(1, int(limits.get("max_steps", 8)))
        self.max_recovery_attempts = max(1, int(limits.get("max_recovery_attempts_per_issue", 2)))
        self.cooldown_seconds = max(0, int(limits.get("cooldown_seconds", 300)))
        self.max_concurrent_fixes = 2
        self.auto_restart_services = False
        self.synthesize_skills_after = 3  # occurrences before synthesizing a skill
        self.fix_history: Dict[str, List[Dict]] = defaultdict(list)  # issue_type -> fixes

    def set_memory(self, memory):
        self.memory = memory

    # ===== Main Healing Loop =====

    def run_once(self, dry_run: bool = True) -> List[Dict]:
        """Run a single healing pass and return list of actions taken."""
        actions = []

        # 1. Check service health
        health = self.check_all_services()
        issues = self.detect_issues(health)

        steps_used = 0
        for issue in issues:
            if len(actions) >= self.max_concurrent_fixes:
                self._log(f"Stopping pass - max_concurrent_fixes={self.max_concurrent_fixes} reached")
                break
            issue_type = issue["type"]
            # Check if we've tried to fix this recently (avoid tight loops)
            recent_fixes = self.fix_history.get(issue_type, [])
            now = time.time()
            recent_fixes = [f for f in recent_fixes if now - f["timestamp"] < self.cooldown_seconds]
            if len(recent_fixes) >= self.max_recovery_attempts:
                # Too many recent fixes, skip to avoid thrashing
                self._log(f"Skipping {issue_type} - too many recent fixes")
                continue

            # 2. Diagnose
            diagnosis = self.diagnose_issue(issue, health)

            # 3. Plan fix
            fix_plan = self.plan_fix(diagnosis)
            command_steps = len([c for c in fix_plan.get("commands", []) if c.strip() and not c.strip().startswith("#")])
            if steps_used + command_steps > self.max_steps:
                self._log(f"Skipping {issue_type} - circuit breaker max_steps={self.max_steps} reached")
                continue
            steps_used += command_steps

            # 4. Execute fix (with dry-run option if needed)
            result = self.execute_fix(fix_plan, dry_run=True)

            # 5. Verify fix
            verified = self.verify_fix(fix_plan, result)

            # 6. Learn from this fix
            self.learn_from_fix(issue, diagnosis, fix_plan, result, verified)

            actions.append({
                "issue": issue,
                "diagnosis": diagnosis,
                "fix_plan": fix_plan,
                "result": result,
                "verified": verified
            })

            # Record fix in history
            self.fix_history[issue_type].append({
                "timestamp": now,
                "fix": fix_plan,
                "verified": verified
            })

        return actions

    def run_continuous(self):
        """Run healing loop continuously."""
        print(f"Starting continuous healing loop (interval: {self.healing_interval}s)")
        try:
            while True:
                start = time.time()
                actions = self.run_once()
                if actions:
                    print(f"Healing pass: {len(actions)} action plans reviewed (dry-run; no changes made)")
                else:
                    print("Healing pass: no issues detected")
                elapsed = time.time() - start
                sleep_time = max(0, self.healing_interval - elapsed)
                time.sleep(sleep_time)
        except KeyboardInterrupt:
            print("\nHealing loop stopped")

    # ===== Health Checking =====

    def check_all_services(self) -> Dict[str, Any]:
        """Check health of all dai-assistant services."""
        health = {
            "timestamp": time.time(),
            "services": {},
            "system": {},
            "overall": "healthy"
        }

        # Model Router
        health["services"]["model-router"] = self._check_router()

        # Agent S Worker
        health["services"]["agent-s-worker"] = self._check_worker()

        # Voice Bridge
        health["services"]["voice-bridge"] = self._check_voice()

        # System metrics
        health["system"] = self._check_system()

        # Determine overall health
        unhealthy = [name for name, svc in health["services"].items() if svc.get("status") != "healthy"]
        if unhealthy:
            health["overall"] = f"degraded: {', '.join(unhealthy)}"
        else:
            health["overall"] = "healthy"

        return health

    def _http_get_json(self, url: str, timeout: float = 2.0) -> Dict[str, Any]:
        """GET a JSON endpoint. Returns {} on any failure.

        ``requests`` is an optional dependency; when it is absent the call
        falls back to stdlib ``urllib`` so the spine keeps working on a bare
        Debian python3.  Both are loaded lazily via importlib (never as a
        top-level ``import``), which is how the spine stays stdlib-only at
        import time — the same trick the voice-bridge uses for
        faster_whisper/piper.
        """
        try:
            try:
                requests = importlib.import_module("requests")
                r = requests.get(url, timeout=timeout)
                if r.ok:
                    return r.json()
                return {"status": "unhealthy", "error": f"HTTP {r.status_code}"}
            except ImportError:
                import urllib.request
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            return {"status": "unreachable", "error": str(exc)}

    def _check_router(self) -> Dict[str, Any]:
        """Check model-router health."""
        data = self._http_get_json("http://127.0.0.1:11435/health")
        if data.get("status") == "unreachable":
            return data
        return {
            "status": "healthy" if data.get("ok") else "unhealthy",
            "ready_for_chat": data.get("ready_for_chat", False),
            "version": data.get("version"),
            "uptime": data.get("uptime_seconds", 0)
        }

    def _check_worker(self) -> Dict[str, Any]:
        """Check agent-s-worker health."""
        data = self._http_get_json("http://127.0.0.1:8765/health")
        if data.get("status") == "unreachable":
            return data
        return {
            "status": "healthy" if data.get("ok") else "unhealthy",
            "enabled": data.get("enabled", False),
            "dry_run_default": data.get("dry_run_default", True),
            "live_capable": data.get("live_capable", False),
            "display_ready": data.get("display_ready", False),
            "queue_depth": data.get("queue_depth", 0),
            "uptime": data.get("uptime_seconds", 0)
        }

    def _check_voice(self) -> Dict[str, Any]:
        """Check voice-bridge health."""
        data = self._http_get_json("http://127.0.0.1:8766/health")
        if data.get("status") == "unreachable":
            return data
        return {
            "status": "healthy" if data.get("ok") else "unhealthy",
            "whisper_loaded": data.get("whisper_loaded", False),
            "voice_loaded": data.get("voice_loaded", False),
            "uptime": data.get("uptime_seconds", 0)
        }

    def _check_system(self) -> Dict[str, Any]:
        """Check system resources (memory, disk, etc.).

        ``psutil`` is an optional dependency: when it is not installed the
        metrics come back as zero rather than raising, so the spine keeps
        working on a bare Debian python3 with no install step.  It is loaded
        lazily via importlib (never a top-level ``import``) so the module
        stays stdlib-only at import time.
        """
        try:
            if importlib.util.find_spec("psutil") is None:
                return {
                    "memory_percent": 0,
                    "memory_available_gb": 0,
                    "disk_percent": 0,
                    "disk_free_gb": 0,
                    "cpu_percent": 0
                }
            psutil = importlib.import_module("psutil")
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            return {
                "memory_percent": mem.percent,
                "memory_available_gb": mem.available / (1024**3),
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free / (1024**3),
                "cpu_percent": psutil.cpu_percent(interval=0.1)
            }
        except Exception as e:
            return {"error": str(e)}

    # ===== Issue Detection =====

    def detect_issues(self, health: Dict[str, Any]) -> List[Dict]:
        """Analyze health data and return list of issues to fix."""
        issues = []

        # Service unreachable
        for svc_name, svc in health["services"].items():
            if svc.get("status") == "unreachable":
                issues.append({
                    "type": "service_unreachable",
                    "service": svc_name,
                    "severity": "high",
                    "detail": svc.get("error", "Unknown error")
                })
            elif svc.get("status") == "unhealthy":
                issues.append({
                    "type": "service_unhealthy",
                    "service": svc_name,
                    "severity": "medium",
                    "detail": svc.get("error", "Service reported unhealthy")
                })

        # Router not ready for chat
        router = health["services"].get("model-router", {})
        if router.get("status") == "healthy" and not router.get("ready_for_chat"):
            issues.append({
                "type": "router_not_ready",
                "severity": "medium",
                "detail": "Model router is healthy but not ready for chat (no provider keys?)"
            })

        # Worker not enabled or not live capable
        worker = health["services"].get("agent-s-worker", {})
        if worker.get("status") == "healthy":
            if not worker.get("enabled"):
                issues.append({
                    "type": "worker_disabled",
                    "severity": "medium",
                    "detail": "Agent S worker is disabled in policy or .env"
                })
            if not worker.get("live_capable"):
                issues.append({
                    "type": "worker_not_live_capable",
                    "severity": "low",
                    "detail": "Worker reports not live capable (likely missing Xvfb or agent_s installed)"
                })

        # System resource issues
        sys = health.get("system", {})
        if sys.get("memory_percent", 0) > 90:
            issues.append({
                "type": "high_memory_usage",
                "severity": "high",
                "detail": f"Memory usage at {sys['memory_percent']}%"
            })
        if sys.get("disk_percent", 0) > 90:
            issues.append({
                "type": "low_disk_space",
                "severity": "high",
                "detail": f"Disk usage at {sys['disk_percent']}%"
            })
        if sys.get("cpu_percent", 0) > 95:
            issues.append({
                "type": "high_cpu_usage",
                "severity": "medium",
                "detail": f"CPU usage at {sys['cpu_percent']}%"
            })

        return issues

    # ===== Diagnosis =====

    def diagnose_issue(self, issue: Dict, health: Dict) -> Dict:
        """Diagnose the root cause of an issue."""
        issue_type = issue["type"]
        diagnosis = {"issue_type": issue_type, "possible_causes": [], "recommended_fix": None}

        if issue_type == "service_unreachable":
            svc = issue["service"]
            diagnosis["possible_causes"] = [
                f"{svc} process crashed",
                f"{svc} failed to start",
                f"Port conflict on {svc}'s port",
                f"Configuration error preventing {svc} from starting"
            ]
            diagnosis["recommended_fix"] = f"restart_{svc}"

        elif issue_type == "service_unhealthy":
            svc = issue["service"]
            diagnosis["possible_causes"] = [
                f"{svc} started but encountered internal error",
                f"{svc} missing dependencies",
                f"{svc} configuration invalid"
            ]
            diagnosis["recommended_fix"] = f"restart_{svc}_with_logs"

        elif issue_type == "router_not_ready":
            diagnosis["possible_causes"] = [
                "No provider keys configured in .env",
                "All providers have hit rate limits or are unhealthy",
                "Local Ollama not running (if configured as fallback)"
            ]
            diagnosis["recommended_fix"] = "check_and_add_provider_keys"

        elif issue_type == "worker_disabled":
            diagnosis["possible_causes"] = [
                "DAI_AGENT_S_ENABLED=false in .env",
                "policy.sovereign.json agent_s.enabled = false",
                "Worker failed to start and was auto-disabled"
            ]
            diagnosis["recommended_fix"] = "enable_worker"

        elif issue_type == "worker_not_live_capable":
            diagnosis["possible_causes"] = [
                "Xvfb not installed or not running",
                "agent-s not installed in venv",
                "Display :99 not ready"
            ]
            diagnosis["recommended_fix"] = "ensure_xvfb_and_agent_s"

        elif issue_type == "high_memory_usage":
            diagnosis["possible_cases"] = [
                "Memory leak in one of the services",
                "Too many concurrent tasks",
                "Large models loaded into memory",
                "Vector store or cache not being cleared"
            ]
            diagnosis["recommended_fix"] = "reduce_memory_usage"

        elif issue_type == "low_disk_space":
            diagnosis["possible_causes"] = [
                "Log files not rotated",
                "Old sessions or snapshots not cleaned up",
                "Vector store or cache taking too much space",
                "Docker images or build artifacts"
            ]
            diagnosis["recommended_fix"] = "cleanup_disk_space"

        elif issue_type == "high_cpu_usage":
            diagnosis["possible_causes"] = [
                "Infinite loop in a service",
                "Too many concurrent processes",
                "Model inference taking too long (stuck?)",
                "Busy waiting or polling too frequently"
            ]
            diagnosis["recommended_fix"] = "investigate_high_cpu"

        else:
            diagnosis["possible_causes"] = ["Unknown issue type"]
            diagnosis["recommended_fix"] = "manual_investigation"

        return diagnosis

    # ===== Fix Planning =====

    def plan_fix(self, diagnosis: Dict) -> Dict:
        """Create a fix plan from diagnosis."""
        issue_type = diagnosis["issue_type"]
        recovery_action = diagnosis.get("recommended_fix") or issue_type
        fix_plan = {
            "issue_type": issue_type,
            "recovery_action": recovery_action,
            "steps": [],
            "approval_required": False,
            "dry_run_first": True,
            "commands": [],
            "verification": []
        }

        if recovery_action.startswith("restart_"):
            svc = recovery_action.replace("restart_", "").replace("_with_logs", "")
            fix_plan["steps"] = [
                f"Stop {svc} service",
                "Wait for clean shutdown",
                f"Start {svc} service",
                f"Wait for {svc} to report healthy"
            ]
            fix_plan["commands"] = [
                f"./bin/dai-restart-service {svc}.service"
            ]
            fix_plan["verification"] = [
                f"Check {svc} health endpoint",
                f"Verify {svc} reports healthy status"
            ]

        elif issue_type == "enable_worker":
            fix_plan["steps"] = [
                "Set DAI_AGENT_S_ENABLED=true in .env",
                "Restart agent-s-worker service"
            ]
            fix_plan["approval_required"] = False  # changing .env is allowed
            fix_plan["commands"] = [
                "sed -i 's/DAI_AGENT_S_ENABLED=.*/DAI_AGENT_S_ENABLED=true/' $DAI_ROOT/.env",
                "./bin/stop-spine.sh --quiet",
                "./bin/start-spine.sh"
            ]
            fix_plan["verification"] = [
                "Check worker health endpoint shows enabled=true"
            ]

        elif issue_type == "ensure_xvfb_and_agent_s":
            fix_plan["steps"] = [
                "Install Xvfb if missing",
                "Ensure agent-s is installed in venv",
                "Start Xvfb on :99",
                "Verify display is ready"
            ]
            fix_plan["approval_required"] = False
            fix_plan["commands"] = [
                "which Xvfb || sudo apt-get install -y xvfb",
                "$DAI_ROOT/.local/agent-s-venv/bin/pip install gui-agents  # or however agent-s is installed",
                "Xvfb :99 -screen 0 1280x800x24 > $DAI_ROOT/logs/xvfb.log 2>&1 &",
                "echo $! > $DAI_ROOT/state/agent-s-tasks/xvfb.pid",
                "sleep 2 && xset -display :99 q >/dev/null 2>&1 && echo 'Display ready' || echo 'Display not ready'"
            ]
            fix_plan["verification"] = [
                "Check worker health endpoint shows live_capable=true and display_ready=true"
            ]

        elif issue_type == "check_and_add_provider_keys":
            fix_plan["steps"] = [
                "Check .env for provider keys",
                "If missing, prompt user to add one (cannot auto-add for security)",
                "Restart model-router to pick up new keys"
            ]
            fix_plan["approval_required"] = True  # because we might be asking user to edit .env
            fix_plan["commands"] = [
                "grep -E '^(OPENROUTER_API_KEY|TOGETHER_API_KEY|FIREWORKS_API_KEY|XAI_API_KEY)' $DAI_ROOT/.env || echo 'No provider keys found in .env'",
                "echo 'Please add a provider key to $DAI_ROOT/.env (see KEYS.md)'",
                "./bin/stop-spine.sh --quiet",
                "./bin/start-spine.sh"
            ]
            fix_plan["verification"] = [
                "Check router health endpoint shows ready_for_chat=true"
            ]

        elif issue_type == "reduce_memory_usage":
            fix_plan["steps"] = [
                "Identify memory-heavy processes",
                "Restart services to free memory (temporary fix)",
                "Consider reducing model size or concurrency"
            ]
            fix_plan["approval_required"] = self.auto_restart_services
            fix_plan["commands"] = [
                "ps aux --sort=-%mem | head -10",
                "./bin/stop-spine.sh --quiet",
                "./bin/start-spine.sh"
            ]
            fix_plan["verification"] = [
                "Check system memory usage is below 80%"
            ]

        elif issue_type == "cleanup_disk_space":
            fix_plan["steps"] = [
                "Rotate and compress old logs",
                "Clean up old sessions and snapshots",
                "Prune vector store if too large",
                "Remove old Docker images and build cache"
            ]
            fix_plan["approval_required"] = False
            fix_plan["commands"] = [
                "find $DAI_ROOT/logs -name '*.log' -mtime +7 -exec gzip {} \\;",
                "find $DAI_ROOT/state -name '*.json' -mtime +30 -delete",
                "find $DAI_ROOT/state/omni/vectors -name '*.npy' -mtime +30 -delete",
                "docker system prune -f  # if docker is used"
            ]
            fix_plan["verification"] = [
                "Check disk usage is below 80%"
            ]

        elif issue_type == "investigate_high_cpu":
            fix_plan["steps"] = [
                "Identify CPU-heavy processes",
                "Check for infinite loops or stuck tasks",
                "Restart services if needed"
            ]
            fix_plan["approval_required"] = self.auto_restart_services
            fix_plan["commands"] = [
                "ps aux --sort=-%cpu | head -10",
                "top -b -n 1",
                "./bin/stop-spine.sh --quiet",
                "./bin/start-spine.sh"
            ]
            fix_plan["verification"] = [
                "Check CPU usage is below 80%"
            ]

        else:
            fix_plan["steps"] = ["Manual investigation required"]
            fix_plan["approval_required"] = True
            fix_plan["commands"] = ["echo 'Please investigate manually'"]
            fix_plan["verification"] = []

        return fix_plan

    # ===== Fix Execution =====

    def execute_fix(self, fix_plan: Dict, dry_run: bool = True) -> Dict:
        """Fail closed: autonomous healing is dry-run only until approval-token integration exists."""
        return self._dry_run_fix(fix_plan)

    def _dry_run_fix(self, fix_plan: Dict) -> Dict:
        """Return what would be done without actually doing it."""
        return {
            "fix_plan": fix_plan,
            "dry_run": True,
            "would_run": fix_plan.get("commands", []),
            "steps": fix_plan.get("steps", []),
            "message": "This is a dry-run. No changes were made."
        }

    # ===== Fix Verification =====

    def verify_fix(self, fix_plan: Dict, result: Dict) -> bool:
        """Verify that a fix resolved the issue."""
        if not result.get("success", False):
            return False

        # Re-check the specific issue that was addressed
        issue_type = fix_plan["issue_type"]
        recovery_action = fix_plan.get("recovery_action", issue_type)
        health = self.check_all_services()

        if recovery_action.startswith("restart_"):
            svc = recovery_action.replace("restart_", "").replace("_with_logs", "")
            svc_health = health["services"].get(svc, {})
            return svc_health.get("status") == "healthy"

        elif issue_type == "enable_worker":
            worker = health["services"].get("agent-s-worker", {})
            return worker.get("enabled") is True

        elif issue_type == "worker_not_live_capable":
            worker = health["services"].get("agent-s-worker", {})
            return worker.get("live_capable") is True and worker.get("display_ready") is True

        elif issue_type == "check_and_add_provider_keys":
            router = health["services"].get("model-router", {})
            return router.get("ready_for_chat") is True

        elif issue_type in ["reduce_memory_usage", "cleanup_disk_space", "investigate_high_cpu"]:
            sys = health.get("system", {})
            if issue_type == "reduce_memory_usage":
                return sys.get("memory_percent", 100) < 80
            elif issue_type == "cleanup_disk_space":
                return sys.get("disk_percent", 100) < 80
            elif issue_type == "investigate_high_cpu":
                return sys.get("cpu_percent", 100) < 80

        return True  # Default to assuming it worked if we got here

    # ===== Learning =====

    def learn_from_fix(self, issue: Dict, diagnosis: Dict, fix_plan: Dict, result: Dict, verified: bool):
        """Learn from this fix to improve future handling."""
        if self.memory is None:
            return

        # Log the episode
        self.memory.log_event(
            event_type="healing_fix",
            detail=f"Fixed {issue['type']} on {issue.get('service', 'unknown')}",
            outcome="success" if verified else "failure",
            context={
                "issue": issue,
                "diagnosis": diagnosis,
                "fix_plan": fix_plan,
                "result": result
            },
            tags=["healing", issue["type"], "fixed" if verified else "failed"]
        )

        # If this is a successful fix, we might want to update procedural memory
        if verified and fix_plan.get("steps"):
            # Check if we've seen this fix pattern before
            # For now, just log that we succeeded
            pass

        # If we've seen this issue multiple times, consider synthesizing a skill
        # We'd need to track occurrences; for now, skip

    # ===== Logging =====

    def _log(self, message: str):
        """Log a message to the healing log."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"
        log_file = self.logs_dir / "healing.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(log_line)
        print(log_line.strip())


# Default factory function for easy instantiation
def create_healer(dai_root: Optional[Path] = None) -> OmniHealer:
    if dai_root is None:
        dai_root = Path(__file__).resolve().parents[3]
    return OmniHealer(dai_root)
