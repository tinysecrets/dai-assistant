import tempfile
import unittest
from pathlib import Path

import common


class CommonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        common.BASE = Path(self.temp.name) / "sandbox"
        common.PROJECTS = common.BASE / "projects"
        common.STATE = common.BASE / "state"
        common.TOKEN_FILE = common.STATE / "api.token"
        common.AUDIT_FILE = common.STATE / "audit.jsonl"
        self.original_store_limit = common.MAX_STORE_BYTES
        self.original_store_files = common.MAX_STORE_FILES
        self.original_tree_limit = common.MAX_TREE_BYTES
        self.addCleanup(self.restore_limits)
        common.ensure_layout()

    def restore_limits(self):
        common.MAX_STORE_BYTES = self.original_store_limit
        common.MAX_STORE_FILES = self.original_store_files
        common.MAX_TREE_BYTES = self.original_tree_limit

    def test_project_must_exist_when_create_false(self):
        with self.assertRaises(common.SandboxError):
            common.project_path("missing", create=False)

    def test_write_creates_files_and_hashes(self):
        result = common.write_files(
            "demo",
            [
                {"path": "src/app.py", "content": "print('hello')\n"},
                {"path": "README.md", "content": "# Demo\n"},
            ],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["files"]), 2)
        self.assertEqual((common.PROJECTS / "demo/src/app.py").read_text(), "print('hello')\n")
        self.assertEqual((common.PROJECTS / "demo/src/app.py").stat().st_mode & 0o777, 0o644)

    def test_existing_file_is_not_overwritten_by_default(self):
        common.write_files("demo", [{"path": "app.py", "content": "one"}])
        with self.assertRaises(common.SandboxError):
            common.write_files("demo", [{"path": "app.py", "content": "two"}])
        self.assertEqual((common.PROJECTS / "demo/app.py").read_text(), "one")

    def test_executable_mode_is_preserved_and_default_is_non_executable(self):
        result = common.write_files(
            "modes",
            [
                {"path": "bin/run", "content": "#!/bin/sh\necho ok\n", "executable": True},
                {"path": "src/app.py", "content": "print(1)\n"},
            ],
        )
        modes = {row["path"]: row["executable"] for row in result["files"]}
        self.assertTrue(modes["bin/run"])
        self.assertEqual((common.PROJECTS / "modes/bin/run").stat().st_mode & 0o777, 0o755)
        self.assertEqual((common.PROJECTS / "modes/src/app.py").stat().st_mode & 0o777, 0o644)

    def test_executable_metadata_must_be_boolean(self):
        with self.assertRaises(common.SandboxError):
            common.write_files("bad-mode", [{"path": "run.sh", "content": "echo no\n", "executable": "yes"}])

    def test_explicit_overwrite_is_atomic(self):
        common.write_files("demo", [{"path": "app.py", "content": "one"}])
        result = common.write_files("demo", [{"path": "app.py", "content": "two"}], overwrite=True)
        self.assertEqual(result["files"][0]["action"], "replaced")
        self.assertEqual((common.PROJECTS / "demo/app.py").read_text(), "two")

    def test_batch_preflight_prevents_partial_write_on_conflict(self):
        common.write_files("demo", [{"path": "exists.py", "content": "original"}])
        with self.assertRaises(common.SandboxError):
            common.write_files(
                "demo",
                [
                    {"path": "new.py", "content": "new"},
                    {"path": "exists.py", "content": "replacement"},
                ],
            )
        self.assertFalse((common.PROJECTS / "demo/new.py").exists())
        self.assertEqual((common.PROJECTS / "demo/exists.py").read_text(), "original")

    def test_manifest_cannot_make_a_file_and_parent_with_same_name(self):
        with self.assertRaises(common.SandboxError):
            common.write_files(
                "demo",
                [
                    {"path": "src", "content": "file"},
                    {"path": "src/app.py", "content": "child"},
                ],
            )

    def test_parent_symlink_is_rejected(self):
        root = common.project_path("demo")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            common.write_files("demo", [{"path": "link/escape.txt", "content": "no"}])
        self.assertFalse((outside / "escape.txt").exists())

    def test_traversal_and_live_secrets_are_rejected(self):
        for path in (
            "../escape",
            "/etc/passwd",
            "a/../../b",
            ".env",
            ".env.local",
            ".ssh/key",
            "id_rsa",
            "tls/server.pem",
        ):
            with self.subTest(path=path), self.assertRaises(common.SandboxError):
                common.safe_relpath(path)

    def test_safe_environment_template_is_allowed(self):
        self.assertEqual(common.safe_relpath(".env.example"), (".env.example",))

    def test_tree_digest_rejects_symlinks(self):
        root = common.project_path("demo")
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("outside")
        (root / "link").symlink_to(outside)
        with self.assertRaises(common.SandboxError):
            common.tree_digest(root)

    def test_api_token_is_owner_only(self):
        token = common.ensure_api_token()
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(common.TOKEN_FILE.stat().st_mode & 0o777, 0o600)

    def test_audit_never_contains_source_content(self):
        common.write_files("demo", [{"path": "app.py", "content": "UNIQUE_SOURCE_SECRET"}])
        audit_text = common.AUDIT_FILE.read_text()
        self.assertIn("app.py", audit_text)
        self.assertNotIn("UNIQUE_SOURCE_SECRET", audit_text)

    def test_project_cumulative_size_limit_is_enforced(self):
        common.MAX_TREE_BYTES = 5
        with self.assertRaises(common.SandboxError):
            common.write_files("too-large", [{"path": "app.py", "content": "123456"}])
        self.assertFalse((common.PROJECTS / "too-large/app.py").exists())

    def test_global_store_byte_quota_is_enforced(self):
        common.MAX_STORE_BYTES = 5
        common.write_files("first", [{"path": "app.py", "content": "12345"}])
        with self.assertRaises(common.SandboxError):
            common.write_files("second", [{"path": "app.py", "content": "x"}])
        self.assertFalse((common.PROJECTS / "second/app.py").exists())

    def test_overwrite_quota_uses_size_delta(self):
        common.MAX_STORE_BYTES = 5
        common.write_files("demo", [{"path": "app.py", "content": "12345"}])
        common.write_files("demo", [{"path": "app.py", "content": "123"}], overwrite=True)
        self.assertEqual((common.PROJECTS / "demo/app.py").read_text(), "123")


if __name__ == "__main__":
    unittest.main()
