"""Safety regressions for the Omni integration layer."""

import tempfile
import unittest
from pathlib import Path

from lib.dai.dashboard import get_omni_memory_stats
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

    def test_dashboard_memory_stats_does_not_create_missing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stats = get_omni_memory_stats(root)
            self.assertFalse((root / "state" / "omni").exists())
            self.assertEqual(stats["episodic_events"], 0)

    def test_dashboard_memory_stats_does_not_change_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state" / "omni"
            (state / "vectors").mkdir(parents=True)
            (state / "episodic.jsonl").write_text('{"id":"a"}\\n{"id":"b"}\\n', encoding="utf-8")
            (state / "semantic.json").write_text('{"fact":{}}', encoding="utf-8")
            (state / "procedural.json").write_text('{"skill":{}}', encoding="utf-8")
            (state / "vectors" / "a.npy").write_bytes(b"vector")
            files = list(state.rglob("*"))
            for path in files:
                if path.is_file():
                    path.chmod(0o644)
            before = {path: path.stat().st_mode & 0o777 for path in files if path.is_file()}
            stats = get_omni_memory_stats(root)
            after = {path: path.stat().st_mode & 0o777 for path in files if path.is_file()}
            self.assertEqual(stats["episodic_events"], 2)
            self.assertEqual(stats["semantic_facts"], 1)
            self.assertEqual(stats["procedural_skills"], 1)
            self.assertEqual(stats["vector_embeddings"], 1)
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
