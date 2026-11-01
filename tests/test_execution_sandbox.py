"""Tests for DAI's role-separated execution API client."""

import unittest
from unittest.mock import patch

from lib.dai.execution import ExecutionClient


class ExecutionClientTests(unittest.TestCase):
    def test_roles_are_restricted(self):
        with self.assertRaises(ValueError):
            ExecutionClient("root")

    def test_safe_project_slug(self):
        self.assertEqual(ExecutionClient._project("project-01"), "project-01")
        for value in ("../escape", "/etc", "a/b", "..", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ExecutionClient._project(value)

    def test_sentinel_cannot_write_or_build(self):
        c = ExecutionClient("sentinel")
        with self.assertRaises(PermissionError):
            c.write_files("demo", [{"path": "a.py", "content": "x=1"}])
        with self.assertRaises(PermissionError):
            c.run("demo", ["python3", "-V"])

    def test_omega_cannot_verify(self):
        with self.assertRaises(PermissionError):
            ExecutionClient("omega").verify("demo", [["python3", "-V"]])

    @patch.object(ExecutionClient, "request")
    def test_write_targets_project_file_route(self, request):
        request.return_value = {"ok": True}
        result = ExecutionClient("omega").write_files("demo", [{"path": "app.py", "content": "x=1"}])
        self.assertTrue(result["ok"])
        request.assert_called_once_with(
            "POST", "/v1/projects/demo/files", {"files": [{"path": "app.py", "content": "x=1"}], "overwrite": False}
        )

    @patch.object(ExecutionClient, "verify")
    @patch.object(ExecutionClient, "run")
    @patch.object(ExecutionClient, "write_files")
    def test_build_must_pass_before_sentinel(self, write, run, verify):
        write.return_value = {"ok": True}
        run.return_value = {"ok": False, "exit_code": 2}
        result = ExecutionClient("omega").write_build_verify(
            "demo",
            [{"path": "app.py", "content": "x=1"}],
            ["python3", "-m", "compileall", "-q", "."],
            [["python3", "-m", "unittest"]],
        )
        self.assertEqual(result["state"], "BUILD_FAILED")
        verify.assert_not_called()

    @patch.object(ExecutionClient, "verify")
    @patch.object(ExecutionClient, "run")
    @patch.object(ExecutionClient, "write_files")
    def test_failed_sentinel_never_completes(self, write, run, verify):
        write.return_value = {"ok": True}
        run.return_value = {"ok": True, "exit_code": 0}
        verify.return_value = {"ok": False, "verdict": "FAIL"}
        result = ExecutionClient("omega").write_build_verify(
            "demo",
            [{"path": "app.py", "content": "x=1"}],
            ["python3", "-m", "compileall", "-q", "."],
            [["python3", "-m", "unittest"]],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "VERIFICATION_FAILED")

    @patch.object(ExecutionClient, "verify")
    @patch.object(ExecutionClient, "run")
    @patch.object(ExecutionClient, "write_files")
    def test_completed_requires_actual_sentinel_pass(self, write, run, verify):
        write.return_value = {"ok": True}
        run.return_value = {"ok": True, "exit_code": 0}
        verify.return_value = {"ok": True, "verdict": "PASS"}
        result = ExecutionClient("omega").write_build_verify(
            "demo",
            [{"path": "app.py", "content": "x=1"}],
            ["python3", "-m", "compileall", "-q", "."],
            [["python3", "-m", "unittest"]],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "COMPLETED")
        verify.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
