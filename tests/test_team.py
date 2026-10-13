"""Wah-Lah team supervisor: routing and shared operational memory tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lib.dai.team import (
    OVERSEER_MODEL,
    append_jsonl,
    build_shared_context,
    capability_status,
    classify_task,
    load_jsonl,
    make_run_record,
    redact_sensitive,
    select_team,
)


class TestTaskSelection(unittest.TestCase):
    def test_executable_sources_parse_and_cli_runs_from_another_directory(self):
        root = Path(__file__).resolve().parents[1]
        for rel in ("bin/wah-lah-team", "bin/install-wah-lah-toolchain"):
            source = (root / rel).read_text(encoding="utf-8")
            compile(source, str(root / rel), "exec")
        shell_check = subprocess.run(
            ["bash", "-n", str(root / "bin/dai-team")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(shell_check.returncode, 0, shell_check.stdout)
        with tempfile.TemporaryDirectory() as other_dir:
            result = subprocess.run(
                [sys.executable, str(root / "bin/wah-lah-team"), "--dry-run", "Fix the broken model router loop"],
                cwd=other_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("OVERSEER=DAI model=dai/auto", result.stdout)
        self.assertIn("DELEGATION=plan -> debug -> review", result.stdout)
        self.assertIn("no models called", result.stdout.lower())

    def test_debug_task_delegates_to_debug_specialist(self):
        self.assertEqual(classify_task("Fix the broken microphone and repair the router loop"), "debug")

    def test_market_research_delegates_to_research_specialist(self):
        self.assertEqual(classify_task("Research prospects and market pricing for local service companies"), "research")

    def test_implementation_delegates_to_code_specialist(self):
        self.assertEqual(classify_task("Implement an endpoint and add a regression test"), "code")

    def test_generic_goal_still_has_a_plan(self):
        self.assertEqual(select_team("Help organize the next phase", "plan"), ["plan", "review"])

    def test_nontrivial_code_requires_plan_work_and_independent_review(self):
        self.assertEqual(select_team("Build an app", "code"), ["plan", "code", "review"])

    def test_bad_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            select_team("build", "unapproved")

    def test_capability_catalog_covers_the_wah_lah_projects(self):
        rows = capability_status(Path(__file__).resolve().parents[1])
        names = {row["name"] for row in rows}
        self.assertTrue({
            "plan", "code", "research", "debug", "review", "gui",
            "repair", "voice", "codex", "openclaw", "hermes",
            "continue", "nova", "genie", "omni", "zero-gravity",
        }.issubset(names))
        cloud = next(row for row in rows if row["name"] == "zero-gravity")
        self.assertIsNone(cloud["available"], "external app availability must be checked remotely")



class TestSharedOperationalMemory(unittest.TestCase):
    def test_context_shares_outcomes_not_original_prompts(self):
        context = build_shared_context([
            {"project": "/work/dai", "mode": "code", "status": "verified", "verification": "passed"},
            {"project": "/work/nova", "mode": "debug", "status": "needs-correction", "verification": "failed", "failure_class": "router timeout"},
        ])
        self.assertIn("verified", context)
        self.assertIn("router timeout", context)
        self.assertNotIn("original prompt", context.lower())
        self.assertIn("operational memory, not model training", context.lower())

    def test_owner_notes_are_shared_with_specialists(self):
        context = build_shared_context([], "Owner preference: retain current model roster.")
        self.assertIn("retain current model roster", context)

    def test_credentials_are_redacted(self):
        value = "Authorization: Bearer abcdef123456; OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnop123456"
        safe = redact_sensitive(value)
        self.assertNotIn("abcdef123456", safe)
        self.assertNotIn("abcdefghijklmnop123456", safe)
        self.assertIn("[REDACTED]", safe)

    def test_run_record_hashes_task_instead_of_storing_task_body(self):
        task = "private customer details should not enter the learning log"
        row = make_run_record(
            task=task,
            project="/work/example",
            mode="code",
            team=["plan", "code", "review"],
            status="verified",
            verification="passed",
        )
        self.assertEqual(row["overseer_model"], OVERSEER_MODEL)
        self.assertNotIn(task, json.dumps(row))
        self.assertEqual(len(row["task_sha256"]), 64)

    def test_jsonl_learning_log_is_private_and_skips_corrupt_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "omni" / "team" / "runs.jsonl"
            append_jsonl(path, {"project": "/work/dai", "status": "verified", "key": "freellmapi-" + "a" * 48})
            with path.open("a", encoding="utf-8") as f:
                f.write("{truncated row\n")
            rows = load_jsonl(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "verified")
            self.assertIn("[REDACTED]", json.dumps(rows[0]))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
