"""Unit tests for DAI source snapshot preflight."""

import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dai.execution import ExecutionClient


class SnapshotPreflightTests(unittest.TestCase):
    @patch("lib.dai.execution.repository_manifest")
    def test_snapshot_refuses_to_write_when_required_sources_are_missing(self, manifest):
        manifest.return_value = ([], {"included_files": 0, "included_bytes": 0})
        client = ExecutionClient("omega")
        with patch.object(client, "write_files") as write_files:
            with self.assertRaisesRegex(ValueError, "missing required verification files"):
                client.snapshot_build_verify(Path("."), "preflight-test")
            write_files.assert_not_called()


if __name__ == "__main__":
    unittest.main()
