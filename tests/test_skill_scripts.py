"""Tests for the shipped skill scripts.

Skills are the part of this repo an assistant actually executes, so they are
tested by running them — not by importing them.  Two failure modes matter most
and both have happened:

* a script calling an API that does not exist (``Redactor.scrub()`` instead of
  ``Redactor.redact()``) — works in the happy path, tracebacks in production
* a script that tracebacks instead of reporting, when the worker is down or the
  policy file is missing or corrupt

So: every script is run against a real temporary worker, against nothing, and
against a corrupt policy file.  No invocation may produce a traceback, and the
documented exit codes must be the real ones.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support import TempSpine, start_worker  # noqa: E402

SKILL = ROOT / "skills/agent-s-delegate/scripts"
DELEGATE = SKILL / "delegate_task.py"
HEALTH = SKILL / "worker_health.py"
INSTRUCTION = "Open Chromium and go to example.com"

TRACEBACK = "Traceback (most recent call last)"


def run_script(
    script: Path, args: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 90
) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged.pop("DAI_POLICY", None)
    merged.pop("DAI_APPROVALS", None)
    merged.pop("DAI_AGENT_S_WORKER", None)
    merged.pop("DAI_APPROVAL_TOKEN", None)
    merged.update(env or {})
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=merged,
        cwd=str(ROOT),
    )


def no_traceback(test: unittest.TestCase, proc: subprocess.CompletedProcess) -> None:
    test.assertNotIn(
        TRACEBACK,
        proc.stderr,
        f"script raised instead of reporting:\n{proc.stderr[-1500:]}",
    )


class LiveWorkerCase(unittest.TestCase):
    """Base class: a real worker on an ephemeral port, plus its spine env."""

    policy: Optional[Dict[str, Any]] = None

    def setUp(self) -> None:
        self.spine = TempSpine(policy=self.policy)
        self.service, self.runtime = start_worker(self.spine)
        self.base = self.service.base_url
        self.env = self.spine.worker_env()
        self.addCleanup(self.tearDownServices)

    def tearDownServices(self) -> None:
        self.service.stop()
        self.runtime.stop()
        self.spine.cleanup()

    def issue(self, scope: str = INSTRUCTION, **kwargs: Any) -> str:
        token, _rec = self.runtime.approvals.issue("agent_s_gui_task", scope, **kwargs)
        return token


class TestScriptHygiene(unittest.TestCase):
    """Static properties: the scripts must be runnable and honest about their API."""

    def test_scripts_exist_and_compile(self):
        for script in (DELEGATE, HEALTH):
            with self.subTest(script.name):
                self.assertTrue(script.is_file())
                ast.parse(script.read_text(encoding="utf-8"))

    def test_help_works_and_documents_exit_codes(self):
        for script in (DELEGATE, HEALTH):
            proc = run_script(script, ["--help"])
            with self.subTest(script.name):
                self.assertEqual(proc.returncode, 0, proc.stderr)
                no_traceback(self, proc)
                self.assertIn("exit codes", proc.stdout.lower())

    def test_no_args_is_a_usage_error_not_a_crash(self):
        for script in (DELEGATE, HEALTH):
            proc = run_script(script, [])
            with self.subTest(script.name):
                no_traceback(self, proc)
                # delegate requires --instruction; health runs and reports.
                self.assertIn(proc.returncode, (0, 1, 2, 4))

    def test_every_lib_import_resolves(self):
        """The guard against an invented API: every name imported from lib.dai
        must actually exist in that module."""
        import importlib

        for script in (DELEGATE, HEALTH):
            tree = ast.parse(script.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.startswith("lib.dai"):
                    continue
                module = importlib.import_module(node.module)
                for alias in node.names:
                    with self.subTest(f"{script.name}: {node.module}.{alias.name}"):
                        self.assertTrue(
                            hasattr(module, alias.name),
                            f"{script.name} imports {alias.name} from {node.module}, which does not export it",
                        )

    def test_every_lib_object_attribute_exists(self):
        """Track variables built from lib.dai classes and check the attributes
        used on them.  This is what would have caught ``Redactor.scrub()``."""
        import importlib

        for script in (DELEGATE, HEALTH):
            tree = ast.parse(script.read_text(encoding="utf-8"))
            classes: Dict[str, Any] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("lib.dai"):
                    module = importlib.import_module(node.module)
                    for alias in node.names:
                        obj = getattr(module, alias.name, None)
                        if isinstance(obj, type):
                            classes[alias.asname or alias.name] = obj

            # var name -> class, for `x = SomeClass(...)`
            typed: Dict[str, Any] = {}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)):
                    continue
                cls = classes.get(node.value.func.id)
                if cls is None:
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        typed[target.id] = cls

            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                    continue
                cls = typed.get(node.value.id)
                if cls is None:
                    continue
                with self.subTest(f"{script.name}: {node.value.id}.{node.attr}"):
                    self.assertTrue(
                        hasattr(cls, node.attr),
                        f"{script.name} calls {node.value.id}.{node.attr}() but {cls.__name__} has no such attribute",
                    )

    def test_scripts_use_only_stdlib_and_lib_dai(self):
        allowed = set(sys.stdlib_module_names) | {"lib", "tests", "__future__"}
        for script in (DELEGATE, HEALTH):
            tree = ast.parse(script.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: List[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module.split(".")[0]]
                for name in names:
                    with self.subTest(f"{script.name}: {name}"):
                        self.assertIn(name, allowed, f"{script.name} needs a third-party dep: {name}")

    def test_documented_exit_codes_are_declared_in_the_source(self):
        """The SKILL.md and the scripts must agree on the code meanings."""
        src = DELEGATE.read_text(encoding="utf-8")
        for constant in ("EXIT_OK", "EXIT_API", "EXIT_TASK_FAILED", "EXIT_WAIT_TIMEOUT", "EXIT_USAGE"):
            self.assertIn(constant, src)
        skill = (ROOT / "skills/agent-s-delegate/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Exit codes", skill)
        for code in ("| 0 |", "| 1 |", "| 2 |", "| 3 |", "| 4 |"):
            self.assertIn(code, skill, f"SKILL.md does not document exit code row {code}")


class TestWorkerHealth(LiveWorkerCase):
    """worker_health.py against a real worker, and against every broken state."""

    def test_reports_ready_worker(self):
        proc = run_script(HEALTH, ["--worker", self.base], env=self.env)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["ready"])
        self.assertEqual(doc["worker_url"], self.base)

    def test_summary_carries_the_safety_fields(self):
        proc = run_script(HEALTH, ["--worker", self.base], env=self.env)
        doc = json.loads(proc.stdout)
        summary = doc["summary"]
        for field in (
            "dry_run_default",
            "require_approval_token",
            "bind_owner_live_desktop",
            "live_capable",
            "enabled",
        ):
            self.assertIn(field, summary)
        self.assertFalse(summary["bind_owner_live_desktop"])
        self.assertTrue(summary["require_approval_token"])

    def test_explains_what_blocks_a_live_run(self):
        proc = run_script(HEALTH, ["--worker", self.base], env=self.env)
        doc = json.loads(proc.stdout)
        self.assertFalse(doc["worker"]["live_capable"])
        steps = " ".join(doc["next_steps"])
        self.assertIn("gui-agents", steps, "must name the missing package")
        self.assertIn("dry-run", steps, "must say dry-run still works")

    def test_quiet_output_is_one_json_line(self):
        proc = run_script(HEALTH, ["--worker", self.base, "--quiet"], env=self.env)
        no_traceback(self, proc)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1)
        doc = json.loads(proc.stdout)
        self.assertIn("live_capable", doc)

    def test_worker_down_is_reported_not_raised(self):
        proc = run_script(HEALTH, ["--worker", "http://127.0.0.1:1"], env=self.env)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 1)
        doc = json.loads(proc.stdout)
        self.assertFalse(doc["ok"])
        self.assertIn("unreachable", doc["worker_error"])
        self.assertIn("start-spine.sh", " ".join(doc["next_steps"]))

    def test_missing_policy_file_does_not_crash(self):
        env = dict(self.env)
        env["DAI_POLICY"] = str(self.spine.dir / "absent.json")
        proc = run_script(HEALTH, ["--worker", self.base], env=env)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 0, "a missing policy must not stop a health report")
        doc = json.loads(proc.stdout)
        self.assertIn("policy file not found", doc["note"])

    def test_corrupt_policy_file_does_not_crash(self):
        bad = self.spine.dir / "bad.json"
        bad.write_text("{not json at all", encoding="utf-8")
        env = dict(self.env)
        env["DAI_POLICY"] = str(bad)
        proc = run_script(HEALTH, ["--worker", self.base], env=env)
        no_traceback(self, proc)
        doc = json.loads(proc.stdout)
        self.assertIn("not valid JSON", doc["note"])

    def test_policy_without_agent_s_does_not_crash(self):
        thin = self.spine.dir / "thin.json"
        thin.write_text('{"version": 3}', encoding="utf-8")
        env = dict(self.env)
        env["DAI_POLICY"] = str(thin)
        proc = run_script(HEALTH, ["--worker", self.base], env=env)
        no_traceback(self, proc)
        doc = json.loads(proc.stdout)
        self.assertIn("no agent_s object", doc["note"])

    def test_reads_the_worker_url_from_policy(self):
        """No --worker flag: the policy file must supply the URL."""
        policy_path = Path(self.env["DAI_POLICY"])
        doc = json.loads(policy_path.read_text(encoding="utf-8"))
        doc.setdefault("agent_s", {})["worker_url"] = self.base
        policy_path.write_text(json.dumps(doc), encoding="utf-8")
        proc = run_script(HEALTH, [], env=self.env)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["worker_url"], self.base)

    def test_env_override_beats_policy(self):
        env = dict(self.env)
        env["DAI_AGENT_S_WORKER"] = "http://127.0.0.1:1"
        proc = run_script(HEALTH, [], env=env)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["worker_url"], "http://127.0.0.1:1")

    def test_never_prints_a_secret_from_the_environment(self):
        secret = "sk-or-v1-HEALTHCHECKSECRET123"
        env = dict(self.env)
        env["OPENROUTER_API_KEY"] = secret
        proc = run_script(HEALTH, ["--worker", self.base], env=env)
        no_traceback(self, proc)
        self.assertNotIn(secret, proc.stdout)
        self.assertNotIn(secret, proc.stderr)


class TestDelegateTask(LiveWorkerCase):
    """delegate_task.py exit codes, against a worker with no agent_s installed."""

    def delegate(self, *args: str, env: Optional[Dict[str, str]] = None):
        merged = dict(self.env)
        merged.update(env or {})
        return run_script(DELEGATE, ["--worker", self.base, *args], env=merged)

    def test_dry_run_succeeds_with_exit_zero(self):
        proc = self.delegate("--instruction", INSTRUCTION)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["status"], "dry_run_complete")
        self.assertTrue(doc["dry_run"])

    def test_dry_run_records_the_command_it_would_run(self):
        proc = self.delegate("--instruction", INSTRUCTION)
        doc = json.loads(proc.stdout)
        would_run = doc["result"]["would_run"]
        self.assertIn("agent_s", would_run[0])
        self.assertIn(INSTRUCTION, would_run)

    def test_dry_run_redacts_the_api_key(self):
        env = {"AGENT_S_API_KEY": "sk-or-v1-DELEGATESECRET99"}
        proc = self.delegate("--instruction", INSTRUCTION, env=env)
        no_traceback(self, proc)
        self.assertNotIn("DELEGATESECRET99", proc.stdout)
        self.assertIn("[REDACTED]", proc.stdout)

    def test_quiet_prints_a_compact_summary(self):
        proc = self.delegate("--instruction", INSTRUCTION, "--quiet")
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 0)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["status"], "dry_run_complete")
        self.assertNotIn("would_run", doc, "--quiet should not dump the whole record")

    def test_live_without_a_token_is_a_usage_error(self):
        proc = self.delegate("--instruction", INSTRUCTION, "--live")
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 4)
        self.assertIn("issue-approval.sh", proc.stderr)

    def test_live_with_a_valid_token_reaches_the_agent(self):
        """No agent_s binary here, so the task fails — but it was ACCEPTED, which
        is what distinguishes exit 2 from exit 1."""
        token = self.issue()
        proc = self.delegate("--instruction", INSTRUCTION, "--live", "--approval-token", token)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc["result"]["error"], "agent_s_not_installed")

    def test_token_from_the_environment_is_accepted(self):
        token = self.issue()
        proc = self.delegate("--instruction", INSTRUCTION, "--live", env={"DAI_APPROVAL_TOKEN": token})
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 2, "token should be accepted, task fails on agent_s")
        self.assertNotIn(token, proc.stdout)

    def test_unknown_token_is_an_api_refusal(self):
        proc = self.delegate("--instruction", INSTRUCTION, "--live", "--approval-token", "NOT-A-REAL-TOKEN")
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"], "unknown_approval_token")

    def test_single_use_token_cannot_be_reused(self):
        token = self.issue()
        first = self.delegate("--instruction", INSTRUCTION, "--live", "--approval-token", token)
        self.assertEqual(first.returncode, 2, "first use is accepted")
        second = self.delegate("--instruction", INSTRUCTION, "--live", "--approval-token", token)
        no_traceback(self, second)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(json.loads(second.stdout)["error"], "approval_exhausted")

    def test_scope_mismatched_token_is_refused(self):
        token = self.issue(scope="a completely different instruction")
        proc = self.delegate("--instruction", INSTRUCTION, "--live", "--approval-token", token)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"], "approval_scope_mismatch")

    def test_empty_instruction_is_a_usage_error(self):
        proc = self.delegate("--instruction", "    ")
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 4)

    def test_overlong_instruction_is_refused_locally(self):
        proc = self.delegate("--instruction", "x" * 4001)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 4, "should be caught before hitting the network")
        self.assertIn("4000", proc.stderr)

    def test_max_steps_is_forwarded_and_clamped(self):
        proc = self.delegate("--instruction", INSTRUCTION, "--max-steps", "9999")
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 0)
        doc = json.loads(proc.stdout)
        cap = self.runtime.settings()["max_steps_hard_cap"]
        self.assertEqual(doc["max_steps"], cap, "the policy hard cap must win")

    def test_worker_unreachable_is_an_api_error(self):
        proc = run_script(DELEGATE, ["--worker", "http://127.0.0.1:1", "--instruction", INSTRUCTION], env=self.env)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 1)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["error"], "connection_failed")
        self.assertIn("dai up", proc.stderr)

    def test_timeout_returns_its_own_code(self):
        """A task that is still pending when the wait expires is exit 3, not 2."""
        proc = self.delegate("--instruction", INSTRUCTION, "--timeout", "1", "--poll-interval", "0.2")
        no_traceback(self, proc)
        # The dry run normally finishes instantly, so either outcome is valid;
        # what matters is that a wait timeout, if it happens, is code 3.
        self.assertIn(proc.returncode, (0, 3), proc.stdout + proc.stderr)

    def test_invalid_timeout_is_a_usage_error(self):
        proc = self.delegate("--instruction", INSTRUCTION, "--timeout", "0")
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 4)

    def test_reads_the_worker_url_from_policy(self):
        policy_path = Path(self.env["DAI_POLICY"])
        doc = json.loads(policy_path.read_text(encoding="utf-8"))
        doc.setdefault("agent_s", {})["worker_url"] = self.base
        policy_path.write_text(json.dumps(doc), encoding="utf-8")
        proc = run_script(DELEGATE, ["--instruction", INSTRUCTION], env=self.env)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_falls_back_when_policy_is_corrupt(self):
        bad = self.spine.dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        env = dict(self.env)
        env["DAI_POLICY"] = str(bad)
        proc = run_script(DELEGATE, ["--worker", self.base, "--instruction", INSTRUCTION], env=env)
        no_traceback(self, proc)
        self.assertIn("not valid JSON", proc.stderr)
        self.assertEqual(proc.returncode, 0, "a corrupt policy must not block a working worker")


class TestDelegateAgainstDisabledWorker(LiveWorkerCase):
    """A worker switched off in policy must refuse, and the script must report it."""

    policy: ClassVar[dict] = {"agent_s": {"enabled": False, "dry_run_default": True, "require_approval_token": True}}

    def test_disabled_worker_is_an_api_refusal(self):
        proc = run_script(DELEGATE, ["--worker", self.base, "--instruction", INSTRUCTION], env=self.env)
        no_traceback(self, proc)
        self.assertEqual(proc.returncode, 1)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["error"], "agent_s_disabled_in_policy")

    def test_health_reports_the_disabled_worker(self):
        proc = run_script(HEALTH, ["--worker", self.base], env=self.env)
        no_traceback(self, proc)
        doc = json.loads(proc.stdout)
        self.assertFalse(doc["ready"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("enabled", " ".join(doc["next_steps"]))


if __name__ == "__main__":
    unittest.main()
