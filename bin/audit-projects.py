#!/usr/bin/env python3
import os
import json
import subprocess
import sys

REAL_PATHS = {
    "bigagi": "~/workspace/projects/experiments/bigagi",
    "Agent-S": "~/workspace/repos/inspect/Agent-S",
    "agent-team": "~/workspace/projects/active/agent-team",
    "agent-vibes": "~/workspace/projects/active/agent-vibes",
    "Ai_Station": "~/workspace/projects/experiments/Ai_Station",
    "bytebot": "~/workspace/projects/active/bytebot",
    "CamoPocket": "~/workspace/projects/active/CamoPocket",
    "Genie-1.5": "~/workspace/projects/active/Genie-1.5",
    "genie-sidekick": "~/workspace/projects/active/genie-sidekick",
    "real_Genie": "~/workspace/projects/active/real_Genie",
    "hermes": "~/hermes",
    "nova-sovereign": "~/workspace/projects/active/nova-sovereign",
    "realwah-lah.com": "~/workspace/projects/active/realwah-lah.com",
    "OpenClaw": "~/.openclaw",
    "vellum": "~/workspace/repos/inspect/vellum-assistant",
}

def update_integrations():
    reg_path = os.path.expanduser("~/dai-assistant/config/integrations.json")
    if not os.path.exists(reg_path):
        return
    try:
        with open(reg_path, "r", encoding="utf-8") as f:
            reg = json.load(f)
        for p in reg.get("projects", []):
            name = p.get("name")
            if name in REAL_PATHS:
                p["path"] = REAL_PATHS[name]
        with open(reg_path, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=2)
        print("✓ Updated ~/dai-assistant/config/integrations.json with verified paths\n")
    except Exception as e:
        print(f"Notice: could not update integrations.json ({e})\n")

def check_project(name, raw_path):
    p = os.path.expanduser(raw_path)
    if not os.path.exists(p):
        print(f"❌ {name:<14} | MISSING | {raw_path}")
        return

    stack = []
    deps = []
    env = []
    git_str = "-"

    # Git
    try:
        git_dir = os.path.join(p, ".git")
        if os.path.exists(git_dir):
            branch = subprocess.check_output(
                ["git", "-C", p, "branch", "--show-current"],
                stderr=subprocess.DEVNULL, timeout=2
            ).decode().strip() or "detached"
            dirty = subprocess.check_output(
                ["git", "-C", p, "status", "--porcelain"],
                stderr=subprocess.DEVNULL, timeout=2
            ).decode().strip()
            git_str = branch + ("*" if dirty else "")
    except Exception:
        git_str = "git"

    # Env files
    for ef in [".env", ".env.local", ".env.production", ".env.example"]:
        if os.path.exists(os.path.join(p, ef)):
            env.append(ef)

    # Node / JS
    pkg_path = os.path.join(p, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
            d = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in d:
                stack.append("Next.js")
            elif "react" in d:
                stack.append("React")
            elif "express" in d:
                stack.append("Express")
            else:
                stack.append("Node")
        except Exception:
            stack.append("Node")

        has_nm = os.path.exists(os.path.join(p, "node_modules"))
        deps.append("npm:ok" if has_nm else "npm:NEEDS-INSTALL")

    # Python
    has_py = os.path.exists(os.path.join(p, "pyproject.toml")) or os.path.exists(os.path.join(p, "requirements.txt"))
    if has_py:
        stack.append("Python")
        has_venv = any(os.path.exists(os.path.join(p, d)) for d in [".venv", "venv"])
        deps.append("venv:ok" if has_venv else "venv:NEEDS-INSTALL")

    # Docker
    if os.path.exists(os.path.join(p, "docker-compose.yml")) or os.path.exists(os.path.join(p, "Dockerfile")):
        stack.append("Docker")

    stack_str = "+".join(stack) if stack else "Static/Data"
    deps_str = ", ".join(deps) if deps else "-"
    env_str = "/".join(env) if env else "none"

    print(f"✅ {name:<14} | {stack_str:<14} | {deps_str:<18} | {env_str:<16} | {git_str}")

def main():
    update_integrations()
    header = f"{'PROJECT':<16} | {'STACK':<14} | {'DEPENDENCIES':<18} | {'ENV FILES':<16} | {'GIT'}"
    print(header)
    print("-" * len(header))
    for name, raw_path in REAL_PATHS.items():
        try:
            check_project(name, raw_path)
        except Exception as e:
            print(f"⚠️ {name:<14} | Error checking: {e}")

if __name__ == "__main__":
    main()
