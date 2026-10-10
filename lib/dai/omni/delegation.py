#!/usr/bin/env python3
"""
Omni Delegation - Spawn and manage sub-agents for complex tasks.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make lib.dai import optional for when this module is loaded outside the dai-assistant project
try:
    from lib.dai import env as dai_env
    HAS_DAI_ENV = True
except ImportError:
    HAS_DAI_ENV = False
    dai_env = None


class OmniDelegator:
    """Manages sub-agents for complex tasks."""

    def __init__(self, dai_root: Path):
        self.dai_root = Path(dai_root).resolve()
        self.state_dir = self.dai_root / "state/omni"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.agents_file = self.state_dir / "agents.json"
        self._agents: Dict[str, Dict] = {}
        self._load_agents()

    def _load_agents(self):
        """Load agent state from disk."""
        if self.agents_file.exists():
            try:
                self._agents = json.loads(self.agents_file.read_text())
            except Exception:
                self._agents = {}
        else:
            self._agents = {}

    def _save_agents(self):
        """Save agent state to disk."""
        self.agents_file.write_text(json.dumps(self._agents, indent=2))

    def delegate(self, role: str, goal: str, context: str = "") -> str:
        """Spawn a sub-agent for the given role and goal."""
        agent_id = f"{role}-{uuid.uuid4().hex[:8]}"
        agent = {
            "id": agent_id,
            "role": role,
            "goal": goal,
            "context": context,
            "status": "spawning",
            "created_at": time.time(),
            "updated_at": time.time(),
            "result": None,
            "logs": [],
            "pid": None,
            "log_file": None,
        }
        self._agents[agent_id] = agent
        self._save_agents()

        # Spawn the actual process based on role
        pid, log_file = self._spawn_agent(role, goal, context)
        if pid is not None:
            agent["pid"] = pid
            agent["log_file"] = str(log_file) if log_file else None
            agent["status"] = "running"
            self._log(f"Delegated {role} agent {agent_id} (PID {pid}) for goal: {goal[:50]}")
        else:
            # Fallback to just recording intent
            agent["status"] = "spawned"
            self._log(f"Delegated {role} agent {agent_id} (intent only) for goal: {goal[:50]}")
        self._save_agents()
        return agent_id

    def _spawn_agent(self, role: str, goal: str, context: str) -> tuple[Optional[Path], Optional[Path]]:
        """Fail closed: record intent only until one-shot approval is wired to live delegation."""
        return None, None

    def _find_opencode(self) -> Optional[Path]:
        """Find the opencode binary in PATH."""
        import shutil
        opencode = shutil.which("opencode")
        if opencode:
            return Path(opencode)
        # Also check common locations (derived from the current user, not baked
        # to one machine's layout)
        for path in [
            str(Path.home() / ".npm-global" / "bin" / "opencode"),
            "/usr/local/bin/opencode",
        ]:
            if Path(path).exists():
                return Path(path)
        return None

    def list_agents(self) -> List[Dict]:
        """List all agents (including completed ones)."""
        return list(self._agents.values())

    def get_agent(self, agent_id: str) -> Optional[Dict]:
        """Get a specific agent by ID."""
        return self._agents.get(agent_id)

    def update_agent_status(self, agent_id: str, status: str, result: Any = None):
        """Update an agent's status and result."""
        if agent_id in self._agents:
            self._agents[agent_id]["status"] = status
            self._agents[agent_id]["updated_at"] = time.time()
            if result is not None:
                self._agents[agent_id]["result"] = result
            self._save_agents()

    def add_agent_log(self, agent_id: str, message: str):
        """Add a log entry to an agent."""
        if agent_id in self._agents:
            self._agents[agent_id]["logs"].append({
                "timestamp": time.time(),
                "message": message
            })
            self._save_agents()

    def complete_agent(self, agent_id: str, result: Any):
        """Mark an agent as completed with a result."""
        self.update_agent_status(agent_id, "completed", result)

    def fail_agent(self, agent_id: str, error: str):
        """Mark an agent as failed."""
        self.update_agent_status(agent_id, "failed", {"error": error})

    def cancel_agent(self, agent_id: str):
        """Cancel an agent."""
        self.update_agent_status(agent_id, "cancelled")

    def _log(self, message: str):
        """Log a message to the delegation log."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"
        log_file = self.dai_root / "logs" / "delegation.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(log_line)
        print(log_line.strip())

    # Optional: method to check and update status of running agents
    def poll_agent_status(self, agent_id: str):
        """Update the status of a running agent by checking its process."""
        agent = self._agents.get(agent_id)
        if not agent or agent["status"] not in ("spawning", "running"):
            return
        pid = agent.get("pid")
        if pid is None:
            return
        try:
            # Check if process still exists
            os.kill(pid, 0)
            # Still running
        except OSError:
            # Process has exited
            # Determine exit code? We'll just mark as completed for now
            # In a real implementation, we'd wait and get exit code
            self.update_agent_status(agent_id, "completed", {"note": "process exited"})
            self._log(f"Agent {agent_id} (PID {pid}) completed.")


# Default factory function
def create_delegator(dai_root: Path = None) -> OmniDelegator:
    if dai_root is None:
        dai_root = Path(__file__).resolve().parents[3]
    return OmniDelegator(dai_root)

[executed on device: dhakidd (ddcb9545-ee9e-4325-9877-68cdc77690f4)]
