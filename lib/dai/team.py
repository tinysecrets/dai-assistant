"""Wah-Lah team routing and shared operational learning primitives.

The model provider is selected by the existing DAI router. This module only
coordinates roles, produces project-safe context, and records verifiable run
outcomes; it does not change model weights or replace the owner's model roster.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

OVERSEER_MODEL = "dai/auto"
DEFAULT_PARALLELISM = 4
MAX_SHARED_RUNS = 8

# Specialist names intentionally map to existing workspace assets. The
# orchestrator reports a missing worker instead of silently claiming delegation.
CAPABILITIES: Dict[str, Dict[str, str]] = {
    "plan": {
        "role": "decompose the goal, dependencies, acceptance criteria",
        "entry": "lib/dai/omni/plan_agent.py",
        "kind": "router-agent",
    },
    "code": {
        "role": "implement code/config changes in the selected project",
        "entry": "opencode",
        "kind": "tool-agent",
    },
    "research": {
        "role": "gather outside evidence and synthesize a research briefing",
        "entry": "lib/dai/omni/research_agent.py",
        "kind": "router-agent",
    },
    "debug": {
        "role": "collect host/service evidence and diagnose failures",
        "entry": "lib/dai/omni/debug_agent.py",
        "kind": "router-agent",
    },
    "review": {
        "role": "independently review output, risks, evidence and missing checks",
        "entry": "lib/dai/omni/review_agent.py",
        "kind": "router-agent",
    },
    "gui": {
        "role": "delegate one bounded visual/browser task to Agent S worker",
        "entry": "skills/agent-s-delegate/scripts/delegate_task.py",
        "kind": "approval-gated-worker",
    },
    "repair": {
        "role": "diagnose and repair recognized workstation service issues",
        "entry": "bin/dai-repair",
        "kind": "repair-runbook",
    },
    "voice": {
        "role": "transcribe, reason, speak and validate a voice round trip",
        "entry": "bin/dai-voice",
        "kind": "voice-runbook",
    },
    "codex": {
        "role": "code, patch and repository execution client",
        "entry": "~/.local/bin/codex",
        "kind": "local-cli",
    },
    "openclaw": {
        "role": "interactive owner workbench and tool agent",
        "entry": "~/.npm-global/bin/openclaw",
        "kind": "local-cli",
    },
    "hermes": {
        "role": "alternate agent runtime and tool workflow",
        "entry": "~/.local/bin/hermes",
        "kind": "local-cli",
    },
    "continue": {
        "role": "editor chat, edit and autocomplete integration",
        "entry": "~/.continue/config.yaml",
        "kind": "local-config",
    },
    "nova": {
        "role": "owner-facing assistant with shared gateway and local fallback",
        "entry": "~/nova-2.0/nova/agents/core/nova_mind.py",
        "kind": "local-file",
    },
    "genie": {
        "role": "desktop assistant and command center interface",
        "entry": "~/.config/genie/genie.py",
        "kind": "local-file",
    },
    "omni": {
        "role": "periodic health healing, memory and sub-agent coordination",
        "entry": "bin/omni-daemon.py",
        "kind": "local-script",
    },
    "zero-gravity": {
        "role": "cloud revenue-problem discovery and advisor application",
        "entry": "tinysecrets/zero-gravity on Vercel",
        "kind": "external-check",
    },
}


def redact_sensitive(text: str) -> str:
    """Remove common credential shapes before artifacts or team memory are written."""
    value = str(text)
    patterns = (
        (r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]"),
        (r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]"),
        (r"(?i)(freellmapi-)[a-f0-9]{24,}", r"\1[REDACTED]"),
        (r"(?i)(sk-or-v1-)[a-z0-9]{16,}", r"\1[REDACTED]"),
        (r"(?i)(gsk_)[a-z0-9_-]{12,}", r"\1[REDACTED]"),
    )
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


def classify_task(task: str) -> str:
    """Pick a specialist using intent, not model/provider identities."""
    text = task.lower()
    research = re.compile(
        r"\b(research|prospect|market|find (?:out|sources|business|customers?)|"
        r"compare|latest|current|competitor|pricing|evidence|investigate options|"
        r"legal requirement|regulation|trends|source check)\b"
    )
    debug = re.compile(
        r"\b(debug|fix|repair|broken|error|crash|failed|not working|"
        r"health check|troubleshoot|why (?:is|does|did)|diagnose|regression|"
        r"mic|audio|connection|service down|self.heal|loop)\b"
    )
    gui = re.compile(
        r"\b(open (?:the )?(?:browser|chromium|firefox|window)|click|mouse|keyboard|"
        r"desktop task|screen interaction|take a screenshot|navigate (?:to|through)|"
        r"visible interface|gui automation)\b"
    )
    voice = re.compile(
        r"\b(speak aloud|say aloud|read aloud|voice round.trip|push.to.talk|"
        r"transcribe|record audio|listen and respond|text.to.speech|speech.to.text)\b"
    )
    code = re.compile(
        r"\b(implement|build|create|write code|code|refactor|add (?:a|an|the|new)|"
        r"edit|patch|test suite|endpoint|script|automation|integrate|wire up|"
        r"deploy|website|application|repository|repo)\b"
    )
    if gui.search(text):
        return "gui"
    if voice.search(text):
        return "voice"
    if debug.search(text):
        return "debug"
    if research.search(text):
        return "research"
    if code.search(text):
        return "code"
    return "plan"


def select_team(task: str, requested_mode: str = "auto") -> List[str]:
    """A non-trivial task always gets an overseer plan and an independent review."""
    mode = classify_task(task) if requested_mode == "auto" else requested_mode
    if mode not in {"auto", "plan", "code", "research", "debug", "gui", "voice"}:
        raise ValueError(f"unsupported task mode: {requested_mode}")
    primary = classify_task(task) if mode == "auto" else mode
    team = ["plan"]
    if primary != "plan":
        team.append(primary)
    # A diagnostic request becomes diagnose -> execute when the owner explicitly
    # asks for a repair/fix. Pure diagnosis remains read-only.
    if primary == "debug" and re.search(r"\b(fix|repair|remediate|restore|recover|restart|correct|unblock)\b", task, re.I):
        team.append("code")
    team.append("review")
    return team


def task_digest(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8", errors="replace")).hexdigest()


def load_jsonl(path: Path, limit: int = MAX_SHARED_RUNS) -> List[Dict[str, Any]]:
    """Load the most recent valid records; corrupt/truncated rows are skipped."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    results: List[Dict[str, Any]] = []
    for line in lines[-max(1, limit * 3):]:
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            results.append(row)
    return results[-limit:]


def build_shared_context(
    records: Sequence[Mapping[str, Any]],
    owner_notes: str = "",
    max_chars: int = 4800,
) -> str:
    """Share compact, evidence-qualified outcomes and lessons without replaying prompts."""
    sections: List[str] = []
    if owner_notes.strip():
        sections.append("OWNER-MAINTAINED SHARED NOTES:\n" + redact_sensitive(owner_notes.strip())[:1800])
    if records:
        lines = ["RECENT TEAM OUTCOMES (operational memory, not model training):"]
        for row in records[-MAX_SHARED_RUNS:]:
            project = redact_sensitive(str(row.get("project", "unknown")))[:100]
            mode = str(row.get("mode", "unknown"))[:30]
            status = str(row.get("status", "unknown"))[:30]
            verification = str(row.get("verification", "not-run"))[:100]
            issue = redact_sensitive(str(row.get("failure_class", "")))[:160]
            line = f"- project={project}; mode={mode}; outcome={status}; verification={verification}"
            if issue:
                line += f"; last_blocker={issue}"
            lesson = row.get("verified_lesson")
            if status == "verified" and isinstance(lesson, str) and lesson.strip():
                line += "; verified_lesson=" + redact_sensitive(lesson.strip())[:240]
            lines.append(line)
        sections.append("\n".join(lines))
    context = "\n\n".join(sections)
    return context[-max_chars:]

def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    """Append one redacted record under a private directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    clean = redact_sensitive(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (clean + "\n").encode("utf-8"))
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def capability_status(root: Path) -> List[Dict[str, Any]]:
    """Return availability without attempting network calls or reading secrets."""
    output: List[Dict[str, Any]] = []
    for name, item in CAPABILITIES.items():
        entry = item["entry"]
        kind = item["kind"]
        if kind == "external-check":
            available = None  # Cloud status needs the appropriate Vercel/GitHub connector.
        elif kind == "tool-agent":
            path = shutil.which(entry) or str(Path.home() / ".npm-global" / "bin" / entry)
            available = bool(shutil.which(entry) or Path(path).is_file())
        elif kind in {"local-cli", "local-config", "local-file"}:
            available = Path(entry).expanduser().exists()
        else:
            available = (root / entry).exists()
        output.append({
            "name": name,
            "role": item["role"],
            "entry": entry,
            "kind": item["kind"],
            "available": available,
        })
    return output


def make_run_record(
    *,
    task: str,
    project: str,
    mode: str,
    team: Sequence[str],
    status: str,
    verification: str,
    failure_class: str = "",
    artifacts: Optional[Sequence[str]] = None,
    verified_lesson: str = "",
) -> Dict[str, Any]:
    return {
        "ts": int(time.time()),
        "task_sha256": task_digest(task),
        "project": project,
        "mode": mode,
        "team": list(team),
        "overseer_model": OVERSEER_MODEL,
        "status": status,
        "verification": verification,
        "failure_class": redact_sensitive(failure_class)[:220],
        "verified_lesson": redact_sensitive(verified_lesson.strip())[:240] if verified_lesson.strip() and status == "verified" else "",
        "artifacts": list(artifacts or []),
    }
