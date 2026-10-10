"""Safety regressions for the Omni integration layer."""

import tempfile
import unittest
from pathlib import Path

from lib.dai.omni.delegation import OmniDelegator
from lib.dai.omni.healing import OmniHealer
from lib.dai.omni.memory import OmniMemory


class TestOmniSafety(unittest.TestCase):
    def test_healing_never_executes_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "must-not-exist"
            healer = OmniHealer(Path(tmp))
            result = healer.execute_fix(
                {"commands": [f"touch {marker}"], "steps": ["touch marker"]},
                dry_run=False,
            )
            self.assertTrue(result.get("dry_run"))
            self.assertFalse(marker.exists())
            self.assertIn("would_run", result)

    def test_delegation_records_intent_without_spawning_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            delegator = OmniDelegator(Path(tmp))
            self.assertEqual(delegator._spawn_agent("code", "do a task", ""), (None, None))

    def test_memory_state_directory_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = OmniMemory(Path(tmp))
            self.assertEqual(memory.state_dir.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
