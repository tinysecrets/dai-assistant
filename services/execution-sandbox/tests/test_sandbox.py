import shutil
import tempfile
import unittest
from pathlib import Path

import common
from sandbox import run_project
from sentinel import verify_project


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is required")
class SandboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        common.BASE = Path(self.temp.name) / "sandbox"
        common.PROJECTS = common.BASE / "projects"
        common.STATE = common.BASE / "state"
        common.TOKEN_FILE = common.STATE / "api.token"
        common.AUDIT_FILE = common.STATE / "audit.jsonl"
        common.ensure_layout()
        common.write_files(
            "demo",
            [
                {"path": "src/app.py", "content": "def answer():\n    return 42\n"},
                {
                    "path": "tests/test_app.py",
                    "content": "import unittest\nfrom src.app import answer\nclass TestApp(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 42)\n",
                },
            ],
        )

    def test_run_has_no_host_home_or_network_and_cannot_persist_changes(self):
        code = (
            "import os,socket,pathlib; "
            "assert not pathlib.Path('/home/justin').exists(); "
            "assert not pathlib.Path('/data').exists(); "
            "assert pathlib.Path('/proc/1').exists(); assert pathlib.Path('/proc/self/fd').exists(); "
            "assert socket.socket().connect_ex(('1.1.1.1',53)) != 0; "
            "pathlib.Path('/tmp/inside').write_text('ok'); "
            "pathlib.Path('ephemeral.txt').write_text('not persistent'); "
            "print('ISOLATED_OK')"
        )
        result = run_project("demo", ["python3", "-c", code], timeout_seconds=20)
        self.assertTrue(result["ok"], result)
        self.assertIn("ISOLATED_OK", result["stdout"])
        self.assertTrue(result["source_unchanged"])
        self.assertFalse((common.PROJECTS / "demo/ephemeral.txt").exists())

    def test_sentinel_pass_requires_files_and_zero_exit(self):
        result = verify_project(
            "demo",
            [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]],
            required_files=["src/app.py", "tests/test_app.py"],
            timeout_seconds=30,
        )
        self.assertEqual(result["verdict"], "PASS", result)
        self.assertTrue(result["ok"])

    def test_sentinel_hard_fails_nonzero_exit(self):
        result = verify_project(
            "demo",
            [["python3", "-c", "raise SystemExit(7)"]],
            required_files=["src/app.py"],
            timeout_seconds=20,
        )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertFalse(result["ok"])
        self.assertEqual(result["tests"][0]["exit_code"], 7)

    def test_timeout_is_a_failure(self):
        result = run_project("demo", ["python3", "-c", "import time; time.sleep(5)"], timeout_seconds=1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])


if __name__ == "__main__":
    unittest.main()
