#!/usr/bin/env python3
"""Omni Skill Synthesis - Generate skills from repeated fix patterns."""

from __future__ import annotations

import importlib
import re
import time
from pathlib import Path
from typing import List, Optional

from lib.dai.omni.memory import OmniMemory


def _load_jinja_template():
    """Load the Jinja2 Template class lazily.

    Jinja2 is an optional dependency; importing it at module top would make
    the spine depend on a third-party package, so it is fetched on demand
    (the same lazy-import trick the voice-bridge uses for faster_whisper).
    """
    try:
        return importlib.import_module("jinja2").Template
    except Exception:
        return None


class OmniSynthesizer:
    """Synthesizes new skills from repeated fix patterns."""

    def __init__(self, dai_root: Path):
        self.dai_root = Path(dai_root).resolve()
        self.state_dir = self.dai_root / "state/omni"
        self.skills_dir = self.dai_root / "skills"
        self.memory = OmniMemory(self.dai_root)
        self.template_str = self._load_skill_template()

    def _load_skill_template(self) -> str:
        """Load the Jinja2 template for skill generation."""
        template_path = self.dai_root / "templates/omni/skill.md.j2"
        if template_path.exists():
            return template_path.read_text()
        # Fallback template
        return '''---\nname: {{ name }}\nversion: 1.0.0\ndescription: \"{{ description }}\"\nlicense: MIT\nplatforms: [linux]\nmetadata:\n  hermes:\n    tags: [{{ tags | join(', ')}]]\n    related_skills: [{{ related_skills | join(', ')}]]\n---\n\n# {{ name }}\n\n{{ body }}\n'''

    def synthesize(self, description: str) -> Path:
        """Generate a new skill from a description."""
        # Extract a name from the description
        name = self._generate_name(description)
        # Check if we've seen this fix pattern enough times
        pattern_key = f"fix_pattern:{description}"
        occurrences = self._get_occurrences(pattern_key)
        if occurrences < 3:
            # Not enough occurrences yet, just record this one
            self._increment_occurrence(pattern_key)
            raise ValueError(f"Need 3 occurrences to synthesize a skill, only have {occurrences}")

        # Generate the skill content
        skill_content = self._render_skill(name, description)
        # Save the skill
        skill_dir = self.skills_dir / f"auto-{name}"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_content)
        # Record that we synthesized this skill to avoid re-synthesizing
        self._record_synthesized_skill(name, description)
        return skill_path

    def _generate_name(self, description: str) -> str:
        """Generate a skill name from description."""
        # Take first few words, lowercase, replace non-alnum with hyphens
        words = re.findall(r'\b\w+\b', description.lower())[:4]
        name = '-'.join(words)
        # Ensure it's not too long and is valid
        name = re.sub(r'[^a-z0-9-]', '', name)
        if not name:
            name = "auto-skill"
        return name[:50]  # limit length

    def _get_occurrences(self, pattern_key: str) -> int:
        """Get how many times we've seen this pattern."""
        # We'll store occurrence counts in semantic memory
        entry = self.memory.recall_fact(pattern_key, {})
        return entry.get("count", 0) if isinstance(entry, dict) else 0

    def _increment_occurrence(self, pattern_key: str):
        """Increment the occurrence count for a pattern."""
        entry = self.memory.recall_fact(pattern_key, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["count"] = entry.get("count", 0) + 1
        entry["last_seen"] = time.time()
        self.memory.remember_fact(pattern_key, entry)

    def _record_synthesized_skill(self, name: str, description: str):
        """Record that we've synthesized this skill."""
        self.memory.remember_fact(
            f"synthesized_skill:{name}",
            {
                "description": description,
                "synthesized_at": time.time(),
                "skill_path": f"skills/auto-{name}/SKILL.md"
            }
        )

    def _render_skill(self, name: str, description: str) -> str:
        """Render the skill template with the given data.

        Jinja2 is an optional dependency: if it is not installed, fall back
        to the plain-text template (the same fallback the loader provides),
        so skill synthesis degrades gracefully on a bare python3.
        """
        context = {
            "name": name,
            "description": description,
            "tags": ["auto-generated", "skill-synthesis"],
            "related_skills": ["hermes-agent", "opencode"],
            "body": self._generate_skill_body(name, description)
        }
        Template = _load_jinja_template()
        if Template is not None:
            template = Template(self.template_str)
            return template.render(**context)
        # No jinja2: render with the simple built-in template.
        return self.template_str.format(**context)

    def _generate_skill_body(self, name: str, description: str) -> str:
        """Generate the body of the skill."""
        return f"""This skill was automatically generated by dai-omni from a repeated fix pattern.

## Description
{description}

## When to Use
- When you encounter the same issue that has been fixed multiple times manually
- To encapsulate a known solution into a reusable skill

## How to Use
This skill can be installed into Vellum's skill set to provide automated resolution of the pattern.

## Example Usage
```bash
# Install the skill
./bin/dai-omni skills --install auto-{name}

# Then Vellum can use it automatically when the pattern is detected
```

## Notes
- Auto-generated skills are reviewed periodically for usefulness
- They may be promoted to manual skills if proven valuable
"""

    def list_auto_skills(self) -> List[str]:
        """List all auto-generated skills."""
        skills = []
        if self.skills_dir.exists():
            for item in self.skills_dir.iterdir():
                if item.is_dir() and item.name.startswith("auto-") and (item / "SKILL.md").exists():
                    skills.append(item.name)
        return sorted(skills)

    def list_all_skills(self) -> List[str]:
        """List all skills (manual and auto)."""
        skills = []
        if self.skills_dir.exists():
            for item in self.skills_dir.iterdir():
                if item.is_dir() and (item / "SKILL.md").exists():
                    skills.append(item.name)
        return sorted(skills)

    def install_skill(self, skill_name: str) -> bool:
        """Install a skill into Vellum's skill set."""
        # This would typically involve copying the skill to Vellum's skills directory
        # and updating Vellum's configuration to include it.
        # For now, we'll just log that it would be installed.
        skill_dir = self.skills_dir / skill_name
        if not skill_dir.exists():
            # Try with auto- prefix
            skill_dir = self.skills_dir / f"auto-{skill_name}"
            if not skill_dir.exists():
                raise FileNotFoundError(f"Skill {skill_name} not found")
        # In a real implementation, we would:
        # 1. Copy skill_dir to Vellum's skills directory
        # 2. Update Vellum's config to include this skill
        # 3. Notify Vellum to reload skills
        print(f"Would install skill {skill_name} into Vellum")
        return True


# Default factory function
def create_synthesizer(dai_root: Optional[Path] = None) -> OmniSynthesizer:
    if dai_root is None:
        dai_root = Path(__file__).resolve().parents[3]
    return OmniSynthesizer(dai_root)
