"""Tests for the bin/ shell layer.

These cover the parts that are easy to break silently: every script must parse,
every script must be executable, option parsing must reject garbage, and the
shared helpers in bin/lib.sh must behave.  Where a script can be run safely
without services or secrets, it is run for real.

Nothing here binds a port or mutates the repo: scripts that would write are
pointed at a temporary DAI_STATE_DIR / DAI_LOG_DIR through the environment.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"

# Every executable entry point.  lib.sh is sourced, so it is checked separately.
SCRIPTS = [
    "dai",
    "doctor.sh",
    "hatch-vellum.sh",
    "import-keys.sh",
    "install-vellum-skill.sh",
    "issue-approval.sh",
    "smoke.sh",
    "start-spine.sh",
    "stop-spine.sh",
]


def run(cmd, *, env=None, cwd=ROOT, timeout=120, input_text=None):
    """Run a command, returning (returncode, stdout, stderr) as text."""
    merged = dict(os.environ)
    merged.pop("NO_COLOR", None)
    if env:
        merged.update(env)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=merged,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def terminate(proc):
    """Kill a spawned helper process and reap it, so no ResourceWarning leaks."""
    proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def bash(*args, env=None, timeout=120):
    return run(["bash", "-c", " ".join(args)], env=env, timeout=timeout)


class TestScriptHygiene(unittest.TestCase):
    """Static properties every script must have."""

    def test_all_expected_scripts_exist(self):
        for name in SCRIPTS + ["lib.sh"]:
            self.assertTrue((BIN / name).is_file(), f"missing bin/{name}")

    def test_scripts_parse_with_bash_n(self):
        for name in SCRIPTS + ["lib.sh"]:
            code, _out, err = run(["bash", "-n", str(BIN / name)])
            self.assertEqual(code, 0, f"bin/{name} failed bash -n:\n{err}")

    def test_entry_points_are_executable(self):
        for name in SCRIPTS:
            self.assertTrue(os.access(BIN / name, os.X_OK), f"bin/{name} is not executable")

    def test_lib_sh_is_sourced_not_executed(self):
        """lib.sh is a library: it must be safe to source twice and must not self-run."""
        text = (BIN / "lib.sh").read_text(encoding="utf-8")
        self.assertIn("DAI_REPO_ROOT", text)
        # Double-source guard, so sourcing from two scripts is harmless.
        self.assertIn("_DAI_LIB_SH", text)
        for fn in ("dai_ok", "dai_env_get", "dai_read_pid", "dai_start_service", "dai_check_json"):
            self.assertIn(f"{fn}()", text, f"lib.sh lost helper {fn}")

    def test_scripts_use_strict_mode(self):
        for name in SCRIPTS:
            text = (BIN / name).read_text(encoding="utf-8")
            self.assertIn("set -euo pipefail", text, f"bin/{name} lacks strict mode")

    def test_scripts_source_lib(self):
        for name in SCRIPTS:
            text = (BIN / name).read_text(encoding="utf-8")
            self.assertIn('source "$ROOT/bin/lib.sh"', text, f"bin/{name} does not source lib.sh")

    def test_no_script_sources_env(self):
        """Sourcing .env would export every secret into every child process."""
        for name in SCRIPTS + ["lib.sh"]:
            text = (BIN / name).read_text(encoding="utf-8")
            self.assertFalse(
                re.search(r"^\s*(source|\.)\s+.*\.env\b", text, re.M),
                f"bin/{name} sources .env directly",
            )

    def test_no_pkill_f_without_anchor(self):
        """`pkill -f <bare pattern>` can match the calling shell and kill it."""
        for name in SCRIPTS + ["lib.sh"]:
            text = (BIN / name).read_text(encoding="utf-8")
            for match in re.finditer(r"pkill\s+-f\s+(['\"]?)([^'\"]*)\1", text):
                pattern = match.group(2)
                self.assertTrue(
                    pattern.startswith("^") or "$ROOT" in pattern or "server.py" in pattern,
                    f"bin/{name} has an unanchored pkill -f pattern: {pattern!r}",
                )

    def test_help_flags_are_accepted(self):
        for name in SCRIPTS:
            code, out, err = run([str(BIN / name), "--help"])
            self.assertEqual(code, 0, f"bin/{name} --help failed:\n{err}")
            self.assertTrue(out.strip(), f"bin/{name} --help printed nothing")

    def test_unknown_options_are_rejected(self):
        for name in SCRIPTS:
            code, out, err = run([str(BIN / name), "--definitely-not-a-flag"])
            self.assertNotEqual(code, 0, f"bin/{name} accepted a bogus flag")
            combined = out + err
            # bin/dai is a dispatcher, so it reports an unknown *command*.
            self.assertTrue(
                "unknown option" in combined or "unknown command" in combined,
                f"bin/{name} gave no explanation: {combined[-200:]}",
            )


class TestLibHelpers(unittest.TestCase):
    """Exercise bin/lib.sh functions directly."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-shell-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.logs = self.tmp / "logs"

    def lib(self, body, env=None):
        prelude = f'source "{BIN}/lib.sh"\n'
        merged = {
            "DAI_STATE_DIR": str(self.state),
            "DAI_LOG_DIR": str(self.logs),
            "DAI_ENV_FILE": str(self.tmp / ".env"),
            "DAI_POLICY_FILE": str(ROOT / "policy/sovereign.json"),
        }
        if env:
            merged.update(env)
        return bash(prelude + body, env=merged)

    def test_repo_root_resolves(self):
        code, out, _ = self.lib('echo "$DAI_REPO_ROOT"')
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(ROOT))

    def test_env_get_reads_dotenv_without_sourcing(self):
        (self.tmp / ".env").write_text('FOO=bar\nQUOTED="hello world"\n# comment\nEMPTY=\n', encoding="utf-8")
        code, out, _ = self.lib("dai_env_get FOO; dai_env_get QUOTED; dai_env_get MISSING fallback")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), ["bar", "hello world", "fallback"])

    def test_env_get_prefers_real_environment(self):
        (self.tmp / ".env").write_text("FOO=from_file\n", encoding="utf-8")
        _code, out, _ = self.lib("dai_env_get FOO", env={"FOO": "from_env"})
        self.assertEqual(out.strip(), "from_env")

    def test_json_check_status_codes(self):
        good = self.tmp / "good.json"
        good.write_text('{"a": 1}\n', encoding="utf-8")
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")

        code, _, _ = self.lib(f'dai_check_json "{good}"')
        self.assertEqual(code, 0, "valid json should pass")

        code, _, _ = self.lib(f'dai_check_json "{bad}"')
        self.assertEqual(code, 1, "corrupt json should fail")

        code, _, _ = self.lib(f'dai_check_json "{self.tmp}/absent.json"')
        self.assertEqual(code, 1, "missing file should fail")

    def test_json_get_extracts_nested_values(self):
        code, out, _ = self.lib("""echo '{"a":{"b":[1,2,{"c":"yes"}]}}' | dai_json_get a.b.2.c""")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "yes")

    def test_json_get_reports_absent_keys_as_empty(self):
        code, out, _ = self.lib("""echo '{"a":1}' | dai_json_get nope""")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def _spawn_with_marker(self, marker):
        """Start a long-lived process whose command line contains `marker`.

        The marker has to live in the *target* process, never in the shell that
        runs the check — otherwise the check matches its own command line and
        passes for the wrong reason.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", marker],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(terminate, proc)
        return proc

    def test_read_pid_rejects_a_stale_pid_file(self):
        self.state.mkdir(parents=True, exist_ok=True)
        # A pid that is almost certainly not alive and certainly not ours.
        (self.state / "model-router.pid").write_text("999999\n", encoding="utf-8")
        code, out, _ = self.lib("dai_read_pid model-router && echo ALIVE || echo DEAD")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "DEAD")
        self.assertFalse((self.state / "model-router.pid").exists(), "stale pid file should be removed")

    def test_read_pid_rejects_a_foreign_process(self):
        """The killer guard: a recycled pid must not be treated as ours."""
        self.state.mkdir(parents=True, exist_ok=True)
        victim = self._spawn_with_marker("some-unrelated-program")
        (self.state / "model-router.pid").write_text(f"{victim.pid}\n", encoding="utf-8")
        code, out, _ = self.lib("dai_read_pid model-router && echo ALIVE || echo DEAD")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "DEAD")
        self.assertEqual(victim.poll(), None, "the unrelated process must still be alive")

    def test_read_pid_accepts_a_matching_process(self):
        self.state.mkdir(parents=True, exist_ok=True)
        marker = "services/model-router/server.py"
        ours = self._spawn_with_marker(marker)
        (self.state / "model-router.pid").write_text(f"{ours.pid}\n", encoding="utf-8")
        code, out, _ = self.lib("dai_read_pid model-router >/dev/null && echo ALIVE || echo DEAD")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "ALIVE")

    def test_read_pid_accepts_an_explicit_pattern(self):
        """stop-spine.sh uses this for the Xvfb display, which is not a service."""
        self.state.mkdir(parents=True, exist_ok=True)
        ours = self._spawn_with_marker("Xvfb :99 -screen 0 1280x800x24")
        (self.state / "xvfb.pid").write_text(f"{ours.pid}\n", encoding="utf-8")
        code, out, _ = self.lib("dai_read_pid xvfb Xvfb >/dev/null && echo ALIVE || echo DEAD")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "ALIVE")

    def test_read_pid_returns_the_pid_on_stdout(self):
        self.state.mkdir(parents=True, exist_ok=True)
        ours = self._spawn_with_marker("services/agent-s-worker/server.py")
        (self.state / "agent-s-worker.pid").write_text(f"{ours.pid}\n", encoding="utf-8")
        code, out, _ = self.lib("dai_read_pid agent-s-worker")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), str(ours.pid))

    def test_read_pid_handles_a_missing_state_dir(self):
        code, out, _ = self.lib("dai_read_pid model-router && echo ALIVE || echo DEAD")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "DEAD")

    def test_read_pid_handles_an_empty_pid_file(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "model-router.pid").write_text("\n", encoding="utf-8")
        code, out, _ = self.lib("dai_read_pid model-router && echo ALIVE || echo DEAD")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "DEAD")

    def test_port_probe_reports_a_free_port(self):
        _code, out, _ = self.lib("dai_port_in_use 1 && echo BUSY || echo FREE")
        # Port 1 is privileged and nothing should be bound to it here.
        self.assertIn("FREE", out)

    def test_wait_up_times_out_cleanly(self):
        code, out, _err = self.lib("dai_wait_up http://127.0.0.1:1 2 probe && echo UP || echo DOWN")
        self.assertEqual(code, 0)
        self.assertIn("DOWN", out)

    def test_counter_helpers_tally(self):
        _code, out, _ = self.lib(
            'dai_ok a; dai_ok b; dai_warn c; dai_fail d; echo "$DAI_OK_COUNT/$DAI_WARN_COUNT/$DAI_FAIL_COUNT"'
        )
        self.assertEqual(out.strip().splitlines()[-1], "2/1/1")

    def test_summary_exits_nonzero_on_failure(self):
        code, _out, _ = self.lib("dai_fail boom; dai_summary")
        self.assertEqual(code, 1)

    def test_summary_exits_zero_with_warnings_only(self):
        code, _out, _ = self.lib("dai_warn meh; dai_summary")
        self.assertEqual(code, 0, "warnings must not fail a run")

    def test_no_color_strips_ansi(self):
        _code, out, _ = self.lib("dai_ok green", env={"NO_COLOR": "1"})
        self.assertNotIn("\033[", out)


class TestDoctor(unittest.TestCase):
    """doctor.sh must be safe to run on a fresh clone."""

    def test_doctor_runs_without_services(self):
        _code, out, err = run([str(BIN / "doctor.sh")], timeout=180)
        combined = out + err
        self.assertIn("Repository", combined)
        self.assertIn("summary:", combined)
        # A fresh clone has no keys and nothing running: those are warnings,
        # not failures, so the doctor itself must not report a FAIL for them.
        self.assertNotIn("FAIL  .env missing", combined)
        self.assertNotIn("FAIL  model-router is not running", combined)

    def test_doctor_json_is_valid_and_sole_stdout(self):
        _code, out, _err = run([str(BIN / "doctor.sh"), "--json"], timeout=180)
        doc = json.loads(out)  # raises if stdout carries anything but JSON
        self.assertIn("ok", doc)
        self.assertIn("checks", doc)
        self.assertEqual(sorted(doc["checks"]), ["fail", "ok", "warn"])
        self.assertIn("services", doc)
        self.assertIn("model_router", doc["services"])
        self.assertIn("agent_s_worker", doc["services"])

    def test_doctor_json_ok_mirrors_exit_code(self):
        code, out, _ = run([str(BIN / "doctor.sh"), "--json"], timeout=180)
        doc = json.loads(out)
        self.assertEqual(doc["ok"], code == 0)

    def test_doctor_quiet_prints_nothing_but_summary(self):
        _code, out, _err = run([str(BIN / "doctor.sh"), "--quiet"], timeout=180)
        self.assertNotIn("== Debian AI doctor ==", out)
        # --quiet is documented as "only the summary line" — assert it really
        # prints that line, not zero lines.
        self.assertRegex(out.strip(), r"^doctor: ok=\d+ warn=\d+ fail=\d+$")

    def test_doctor_quiet_json_still_json_only(self):
        # Combined flags: --json must keep stdout pure JSON even when --quiet
        # is also given.
        _code, out, _err = run([str(BIN / "doctor.sh"), "--quiet", "--json"], timeout=180)
        doc = json.loads(out)  # raises if anything else touched stdout
        self.assertIn("checks", doc)

    def test_doctor_never_leaks_env_values(self):
        tmp = Path(tempfile.mkdtemp(prefix="dai-doc-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        secret = "sk-or-v1-SUPERSECRETVALUE123"
        (tmp / ".env").write_text(f"OPENROUTER_API_KEY={secret}\n", encoding="utf-8")
        _code, out, err = run(
            [str(BIN / "doctor.sh")],
            env={"DAI_ENV_FILE": str(tmp / ".env")},
            timeout=180,
        )
        self.assertNotIn(secret, out + err)


class TestIssueApproval(unittest.TestCase):
    """The approval CLI: it crashed outright before policy/approvals.json existed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-appr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.approvals = self.tmp / "approvals.json"
        self.env = {
            "DAI_APPROVALS_FILE": str(self.approvals),
            "DAI_STATE_DIR": str(self.tmp / "state"),
            "DAI_LOG_DIR": str(self.tmp / "logs"),
        }

    def issue(self, *args):
        return run([str(BIN / "issue-approval.sh"), *args], env=self.env)

    def test_creates_the_file_on_first_use(self):
        self.assertFalse(self.approvals.exists())
        code, _out, err = self.issue("agent_s_gui_task", "do the thing")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.approvals.is_file())
        self.assertEqual(self.approvals.stat().st_mode & 0o777, 0o600)

    def test_stdout_is_exactly_the_token(self):
        """Callers do TOKEN=$(issue-approval.sh ...); stdout must carry nothing else."""
        code, out, err = self.issue("agent_s_gui_task", "do the thing")
        self.assertEqual(code, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1, f"stdout was not a bare token: {lines}")
        self.assertGreaterEqual(len(lines[0]), 16)

    def test_rejects_a_missing_scope(self):
        code, out, err = self.issue("agent_s_gui_task")
        self.assertEqual(code, 1)
        self.assertIn("scope", (out + err).lower())

    def test_rejects_an_unknown_action(self):
        code, _out, _err = self.issue("rm_rf_everything", "*")
        self.assertEqual(code, 1)

    def test_rejects_a_missing_action(self):
        code, _out, _err = self.issue()
        self.assertEqual(code, 1)

    def test_list_is_valid_json(self):
        self.issue("agent_s_gui_task", "one")
        self.issue("openrouter_paid", "anthropic/claude-sonnet-4.5")
        code, out, err = self.issue("--list")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(len(doc), 2)
        for entry in doc:
            # Tokens are masked in listings — the full value is printed once, at issue.
            self.assertIn("…", entry["token"])

    def test_revoke_removes_the_token(self):
        _, token, _ = self.issue("agent_s_gui_task", "revoke me")
        token = token.strip()
        code, _out, err = self.issue("--revoke", token)
        self.assertEqual(code, 0, err)
        _, listed, _ = self.issue("--list")
        self.assertEqual(json.loads(listed), [])

    def test_revoke_unknown_token_fails(self):
        self.issue("agent_s_gui_task", "seed")
        code, _out, _err = self.issue("--revoke", "NOT-A-REAL-TOKEN")
        self.assertEqual(code, 1)

    def test_prune_reports_a_count(self):
        self.issue("agent_s_gui_task", "seed")
        code, out, err = self.issue("--prune")
        self.assertEqual(code, 0, err)
        self.assertIn("pruned", out + err)

    def test_ttl_and_max_uses_are_honoured(self):
        _, token, _ = self.issue("agent_s_gui_task", "scoped", "--ttl", "60", "--max-uses", "3")
        stored = json.loads(self.approvals.read_text(encoding="utf-8"))["tokens"][token.strip()]
        self.assertEqual(stored["ttl_seconds"], 60)
        self.assertEqual(stored["max_uses"], 3)

    def test_note_is_recorded(self):
        _, token, _ = self.issue("agent_s_gui_task", "scoped", "--note", "for the demo")
        stored = json.loads(self.approvals.read_text(encoding="utf-8"))["tokens"][token.strip()]
        self.assertEqual(stored["note"], "for the demo")


class TestInstallVellumSkill(unittest.TestCase):
    """Skill installation must validate SKILL.md and never guess a destination."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-skill-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_list_shows_validated_skills(self):
        code, out, err = run([str(BIN / "install-vellum-skill.sh"), "--list"])
        self.assertEqual(code, 0, err)
        self.assertIn("agent-s-delegate", out + err)
        self.assertIn("english-to-code", out + err)

    def test_unknown_skill_is_rejected(self):
        code, out, err = run([str(BIN / "install-vellum-skill.sh"), "no-such-skill"])
        self.assertEqual(code, 1)
        self.assertIn("skill not found", (out + err).lower())

    def test_install_into_explicit_dest(self):
        dest = self.tmp / "ws" / "skills"
        code, _out, err = run([str(BIN / "install-vellum-skill.sh"), "--dest", str(dest), "agent-s-delegate"])
        self.assertEqual(code, 0, err)
        self.assertTrue((dest / "agent-s-delegate" / "SKILL.md").is_file())
        self.assertTrue((dest / "agent-s-delegate" / "scripts" / "delegate_task.py").is_file())

    def test_link_mode_creates_a_symlink(self):
        dest = self.tmp / "ws-link" / "skills"
        code, _out, err = run([str(BIN / "install-vellum-skill.sh"), "--link", "--dest", str(dest), "english-to-code"])
        self.assertEqual(code, 0, err)
        target = dest / "english-to-code"
        self.assertTrue(target.is_symlink())
        self.assertEqual(Path(os.readlink(target)).resolve(), (ROOT / "skills/english-to-code").resolve())

    def test_reinstall_replaces_the_previous_copy(self):
        dest = self.tmp / "ws-re" / "skills"
        run([str(BIN / "install-vellum-skill.sh"), "--dest", str(dest), "english-to-code"])
        stale = dest / "english-to-code" / "LEFTOVER.txt"
        stale.write_text("stale", encoding="utf-8")
        code, _out, err = run([str(BIN / "install-vellum-skill.sh"), "--dest", str(dest), "english-to-code"])
        self.assertEqual(code, 0, err)
        self.assertFalse(stale.exists(), "a reinstall must not leave stale files behind")

    def test_a_skill_without_frontmatter_is_rejected(self):
        """Guard against shipping a SKILL.md Vellum cannot load."""
        skills = self.tmp / "skills"
        (skills / "broken").mkdir(parents=True)
        (skills / "broken" / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
        fake_root = self.tmp / "root"
        shutil.copytree(BIN, fake_root / "bin")
        shutil.copytree(skills, fake_root / "skills")
        shutil.copytree(ROOT / "lib", fake_root / "lib")
        code, out, err = run([str(fake_root / "bin" / "install-vellum-skill.sh"), "broken"], cwd=fake_root)
        self.assertEqual(code, 1)
        self.assertIn("frontmatter", (out + err).lower())


class TestImportKeys(unittest.TestCase):
    """Key import must never print a secret and never clobber a real value."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-keys-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_file = self.tmp / ".env"
        self.env = {
            "DAI_ENV_FILE": str(self.env_file),
            "HOME": str(self.tmp),  # keeps the real ~/.config installs out of reach
            "DAI_STATE_DIR": str(self.tmp / "state"),
            "DAI_LOG_DIR": str(self.tmp / "logs"),
        }

    def test_dry_run_writes_nothing(self):
        code, out, err = run([str(BIN / "import-keys.sh"), "--dry-run"], env=self.env)
        self.assertEqual(code, 0, err)
        self.assertFalse(self.env_file.exists(), "a dry run must not create .env")
        self.assertIn("dry run", (out + err).lower())

    def test_creates_env_from_example(self):
        code, _out, err = run([str(BIN / "import-keys.sh")], env=self.env)
        self.assertEqual(code, 0, err)
        self.assertTrue(self.env_file.is_file())
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("OPENROUTER_API_KEY", text)

    def test_reports_key_names_only(self):
        _code, out, err = run([str(BIN / "import-keys.sh"), "--dry-run"], env=self.env)
        combined = out + err
        self.assertIn("OPENROUTER_API_KEY", combined)
        self.assertIn("values are never printed", combined.lower())

    def test_keeps_an_existing_value(self):
        self.env_file.write_text("OPENROUTER_API_KEY=sk-or-v1-MINE\n", encoding="utf-8")
        code, out, err = run([str(BIN / "import-keys.sh")], env=self.env)
        self.assertEqual(code, 0, err)
        self.assertIn("OPENROUTER_API_KEY=sk-or-v1-MINE", self.env_file.read_text(encoding="utf-8"))
        self.assertIn("keeping", (out + err).lower())

    def test_imports_from_the_environment_when_asked(self):
        secret = "sk-or-v1-FROMENVIRONMENT"
        code, out, err = run(
            [str(BIN / "import-keys.sh"), "--from-env"],
            env={**self.env, "OPENROUTER_API_KEY": secret},
        )
        self.assertEqual(code, 0, err)
        self.assertIn(secret, self.env_file.read_text(encoding="utf-8"))
        self.assertNotIn(secret, out + err)

    def test_ignores_placeholders_from_the_environment(self):
        code, _out, err = run(
            [str(BIN / "import-keys.sh"), "--from-env"],
            env={**self.env, "OPENROUTER_API_KEY": "your-key-here"},
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("your-key-here", self.env_file.read_text(encoding="utf-8"))

    def test_preserves_comments_and_layout(self):
        code, _out, err = run([str(BIN / "import-keys.sh")], env=self.env)
        self.assertEqual(code, 0, err)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("# Debian AI Assistant", text)
        self.assertTrue(text.endswith("\n"))


class TestDaiDispatcher(unittest.TestCase):
    """bin/dai is a thin wrapper; it must route and explain itself."""

    def test_help_lists_every_subcommand(self):
        code, out, err = run([str(BIN / "dai"), "help"])
        self.assertEqual(code, 0, err)
        for cmd in ("doctor", "up", "down", "status", "smoke", "keys", "approve", "models", "chat", "version"):
            self.assertIn(cmd, out, f"dai help does not mention '{cmd}'")

    def test_unknown_command_is_rejected(self):
        code, out, err = run([str(BIN / "dai"), "frobnicate"])
        self.assertEqual(code, 1)
        self.assertIn("unknown command", (out + err).lower())

    def test_version_reports_every_component(self):
        code, out, err = run([str(BIN / "dai"), "version"])
        self.assertEqual(code, 0, err)
        self.assertIn("model-router", out)
        self.assertIn("agent-s-worker", out)
        self.assertIn("lib/dai", out)
        self.assertIn("python", out.lower())

    def test_no_arguments_shows_help(self):
        code, out, _err = run([str(BIN / "dai")])
        self.assertEqual(code, 0)
        self.assertIn("dai doctor", out)

    def test_models_without_a_router_fails_helpfully(self):
        code, out, err = run(
            [str(BIN / "dai"), "models"],
            env={"DAI_ROUTER_PORT": "1", "DAI_ROUTER_HOST": "127.0.0.1"},
            timeout=60,
        )
        self.assertEqual(code, 1)
        self.assertIn("not running", (out + err).lower())


class TestStopSpine(unittest.TestCase):
    """stop-spine must be idempotent and must never kill a foreign process.

    These run against a *copied* repo root, not the real one.  stop-spine's
    orphan fallback matches "$ROOT/services/<name>/server.py" by absolute path,
    which no environment variable can redirect — so running it from the real
    tree kills a live spine.  smoke.sh runs this suite while the spine is up to
    perform its live checks, which means a non-hermetic version of this test
    took the spine down mid-run and then failed the checks that needed it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-stop-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "root"
        shutil.copytree(BIN, self.root / "bin")
        self.env = {
            "DAI_STATE_DIR": str(self.tmp / "state"),
            "DAI_LOG_DIR": str(self.tmp / "logs"),
        }

    def stop(self, *args, env=None):
        merged = dict(self.env)
        merged.update(env or {})
        return run([str(self.root / "bin" / "stop-spine.sh"), *args], env=merged, cwd=self.root, timeout=60)

    def test_stop_with_nothing_running(self):
        code, out, err = self.stop()
        self.assertEqual(code, 0, err)
        self.assertIn("nothing was running", out + err)

    def test_stop_is_idempotent(self):
        for _ in range(2):
            code, _out, err = self.stop()
            self.assertEqual(code, 0, err)

    def test_stop_ignores_a_foreign_pid_file(self):
        """A pid file pointing at an unrelated live process must be dropped, not killed."""
        state = Path(self.env["DAI_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(terminate, victim)
        (state / "model-router.pid").write_text(f"{victim.pid}\n", encoding="utf-8")
        code, _out, err = self.stop()
        self.assertEqual(code, 0, err)
        self.assertEqual(victim.poll(), None, "stop-spine killed an unrelated process")

    def test_orphan_cleanup_is_scoped_to_this_checkout(self):
        """A hand-started service here is cleaned up; the same service in
        another checkout is left alone.

        The orphan pattern is anchored to this root's absolute path precisely so
        that a second clone running its own spine cannot be stopped by the
        first.  This is the behaviour that makes the fallback safe, so it is
        worth asserting both directions rather than only "it does not crash".
        """
        if shutil.which("python3") is None:
            self.skipTest("python3 is not on PATH")

        def fake_service(path):
            """A stand-in service whose cmdline matches the orphan pattern."""
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            # argv must be exactly "python3 <path>" — an absolute interpreter
            # path would not match the anchored pattern, which is the point.
            proc = subprocess.Popen(["python3", str(path)])
            self.addCleanup(terminate, proc)
            return proc

        mine = fake_service(self.root / "services" / "model-router" / "server.py")
        theirs = fake_service(self.tmp / "other-checkout" / "services" / "model-router" / "server.py")

        def started(proc):
            for _ in range(50):
                if proc.poll() is not None:
                    return False
                time.sleep(0.1)
            return True

        self.assertTrue(started(mine), "the stand-in orphan exited immediately")
        self.assertTrue(started(theirs), "the stand-in foreign service exited immediately")

        code, out, err = self.stop()
        self.assertEqual(code, 0, err)
        self.assertIn("orphaned", out + err)

        def exited(proc, seconds=10):
            for _ in range(seconds * 10):
                if proc.poll() is not None:
                    return True
                time.sleep(0.1)
            return False

        self.assertTrue(exited(mine), "a hand-started service from this root was not stopped")
        self.assertIsNone(theirs.poll(), "stop-spine killed a service belonging to another checkout")


class TestSmokeScript(unittest.TestCase):
    """smoke.sh option handling.

    The suite-running path is deliberately NOT exercised here: smoke.sh runs
    this file, so running smoke.sh from here would nest suites.  smoke.sh sets
    DAI_INSIDE_SMOKE to make that safe, and we skip rather than rely on it.
    """

    RECURSIVE = os.environ.get("DAI_INSIDE_SMOKE") == "1"

    def test_json_flag_emits_parseable_json(self):
        if self.RECURSIVE:
            self.skipTest("already running inside smoke.sh")
        # --live-only avoids the nested suite; with no spine up it finishes fast.
        _code, out, _err = run(
            [str(BIN / "smoke.sh"), "--live-only", "--json"],
            env={"DAI_ROUTER_PORT": "1", "DAI_AGENT_S_PORT": "1"},
            timeout=180,
        )
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        self.assertTrue(lines, "smoke --json printed nothing")
        doc = json.loads(lines[-1])
        self.assertEqual(sorted(doc), ["failed", "ok", "passed"])
        self.assertIsInstance(doc["ok"], bool)
        # Nothing was running, but "not running" is a warning, not a failure.
        self.assertTrue(doc["ok"], f"unexpected failures: {out}")

    def test_json_flag_reflects_a_failure(self):
        if self.RECURSIVE:
            self.skipTest("already running inside smoke.sh")
        # Narrow discovery so this does not re-run the whole suite inside itself.
        code, out, _err = run(
            [str(BIN / "smoke.sh"), "--tests-only", "--json"],
            env={"DAI_SMOKE_TEST_PATTERN": "test_env.py"},
            timeout=300,
        )
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        doc = json.loads(lines[-1])
        self.assertTrue(doc["ok"], f"unexpected failures: {out[-400:]}")
        self.assertEqual(doc["ok"], code == 0)
        self.assertGreater(doc["passed"], 0)

    def test_help_is_accepted(self):
        code, out, err = run([str(BIN / "smoke.sh"), "--help"])
        self.assertEqual(code, 0, err)
        self.assertIn("smoke", out.lower())

    def test_unknown_option_is_rejected(self):
        code, out, err = run([str(BIN / "smoke.sh"), "--nope"])
        self.assertEqual(code, 1)
        self.assertIn("unknown option", (out + err).lower())


if __name__ == "__main__":
    unittest.main()
