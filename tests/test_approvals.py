"""Approval tokens: fail closed, single-spend, expiring."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from lib.dai.approvals import WILDCARD, ApprovalStore


class ApprovalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-approvals-"))
        self.path = self.tmp / "approvals.json"
        self.store = ApprovalStore(self.path, default_ttl=3600)

    def write_raw(self, doc: dict) -> None:
        self.path.write_text(json.dumps(doc))
        self.path.chmod(0o600)


class TestMissingFile(ApprovalTestCase):
    def test_validate_does_not_raise_when_the_file_is_absent(self) -> None:
        """Regression: a missing approvals.json used to 500 every task POST."""
        err, detail = self.store.validate("agent_s_gui_task", "whatever", "scope")
        self.assertEqual(err, "unknown_approval_token")
        self.assertTrue(detail)

    def test_issue_creates_the_file_with_tight_permissions(self) -> None:
        token, rec = self.store.issue("agent_s_gui_task", "open chromium")
        self.assertTrue(self.path.exists())
        self.assertEqual(rec["action"], "agent_s_gui_task")
        self.assertLessEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.store.validate("agent_s_gui_task", token, "open chromium")[0])


class TestIssueValidation(ApprovalTestCase):
    def test_unknown_action_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.issue("rm_rf_everything", "*")

    def test_empty_scope_rejected(self) -> None:
        """An empty scope must not silently behave like a wildcard."""
        with self.assertRaises(ValueError):
            self.store.issue("agent_s_gui_task", "")

    def test_wildcard_scope_is_explicit(self) -> None:
        token, _ = self.store.issue("agent_s_gui_task", WILDCARD)
        self.assertIsNone(self.store.validate("agent_s_gui_task", token, "anything at all")[0])

    def test_missing_token(self) -> None:
        self.assertEqual(self.store.validate("agent_s_gui_task", None, "x")[0], "missing_approval_token")
        self.assertEqual(self.store.validate("agent_s_gui_task", "", "x")[0], "missing_approval_token")

    def test_action_mismatch(self) -> None:
        token, _ = self.store.issue("agent_s_gui_task", "x")
        self.assertEqual(self.store.validate("openrouter_paid", token, "x")[0], "approval_action_mismatch")

    def test_scope_mismatch(self) -> None:
        token, _ = self.store.issue("agent_s_gui_task", "open chromium")
        self.assertEqual(
            self.store.validate("agent_s_gui_task", token, "delete everything")[0],
            "approval_scope_mismatch",
        )

    def test_scope_list(self) -> None:
        token, _ = self.store.issue("openrouter_paid", "*")
        doc = json.loads(self.path.read_text())
        doc["tokens"][token]["scope"] = ["model-a", "model-b"]
        self.write_raw(doc)
        self.assertIsNone(self.store.validate("openrouter_paid", token, "model-b")[0])
        self.assertEqual(self.store.validate("openrouter_paid", token, "model-c")[0], "approval_scope_mismatch")

    def test_legacy_empty_scope_fails_closed(self) -> None:
        """Tokens written by the old script had ``scope: ""`` — reject them."""
        self.write_raw(
            {
                "tokens": {
                    "legacy-token": {
                        "action": "agent_s_gui_task",
                        "scope": "",
                        "created_at": time.time(),
                        "spent": False,
                    }
                }
            }
        )
        self.assertEqual(
            self.store.validate("agent_s_gui_task", "legacy-token", "open chromium")[0],
            "approval_scope_mismatch",
        )


class TestTokenShape(ApprovalTestCase):
    """A token has to survive being handed to a script on a command line."""

    def test_no_issued_token_can_start_with_a_dash(self) -> None:
        """Regression: ``token_urlsafe`` draws from the base64url alphabet, so
        about 1 token in 64 began with ``-``.  Such a token cannot be passed as
        a separate argv element — argparse reads it as an option flag and
        refuses the invocation — so a perfectly valid approval surfaced as a
        usage error.  It failed in CI roughly one run in sixty.
        """
        from lib.dai.approvals import _new_token

        for _ in range(500):
            self.assertFalse(_new_token().startswith("-"))

        token, _rec = self.store.issue("agent_s_gui_task", WILDCARD)
        self.assertFalse(token.startswith("-"), token)

    def test_re_rolling_does_not_cost_entropy_or_uniqueness(self) -> None:
        """Discarding dash-prefixed candidates must not weaken the token."""
        tokens = {self.store.issue("agent_s_gui_task", WILDCARD)[0] for _ in range(60)}
        self.assertEqual(len(tokens), 60, "tokens must not collide")
        for token in tokens:
            self.assertGreaterEqual(len(token), 22, token)


class TestExpiry(ApprovalTestCase):
    def test_expired_token_rejected(self) -> None:
        token, _ = self.store.issue("agent_s_gui_task", "x", ttl_seconds=1)
        doc = json.loads(self.path.read_text())
        doc["tokens"][token]["expires_at"] = time.time() - 5
        self.write_raw(doc)
        self.assertEqual(self.store.validate("agent_s_gui_task", token, "x")[0], "approval_expired")

    def test_ttl_zero_never_expires(self) -> None:
        token, rec = self.store.issue("agent_s_gui_task", "x", ttl_seconds=0)
        self.assertIsNone(rec["expires_at"])
        self.assertIsNone(self.store.validate("agent_s_gui_task", token, "x")[0])

    def test_legacy_created_at_plus_ttl(self) -> None:
        self.write_raw(
            {
                "tokens": {
                    "t": {
                        "action": "agent_s_gui_task",
                        "scope": "x",
                        "created_at": time.time() - 7200,
                        "ttl_seconds": 3600,
                    }
                }
            }
        )
        self.assertEqual(self.store.validate("agent_s_gui_task", "t", "x")[0], "approval_expired")

    def test_describe_reports_expiry(self) -> None:
        token, _ = self.store.issue("agent_s_gui_task", "x", ttl_seconds=1)
        info = self.store.describe(token)
        assert info is not None
        self.assertFalse(info["expired"])
        doc = json.loads(self.path.read_text())
        doc["tokens"][token]["expires_at"] = time.time() - 1
        self.write_raw(doc)
        info = self.store.describe(token)
        assert info is not None
        self.assertTrue(info["expired"])


class TestSingleUse(ApprovalTestCase):
    def test_second_consume_fails(self) -> None:
        token, _ = self.store.issue("agent_s_gui_task", "x", max_uses=1)
        self.assertIsNone(self.store.consume("agent_s_gui_task", token, "x")[0])
        self.assertEqual(self.store.consume("agent_s_gui_task", token, "x")[0], "approval_exhausted")

    def test_legacy_spent_flag_is_honoured(self) -> None:
        self.write_raw(
            {"tokens": {"t": {"action": "agent_s_gui_task", "scope": "x", "spent": True, "created_at": time.time()}}}
        )
        self.assertEqual(self.store.consume("agent_s_gui_task", "t", "x")[0], "approval_exhausted")

    def test_max_uses_allows_n_spendings(self) -> None:
        token, _ = self.store.issue("openrouter_paid", "model", max_uses=3)
        for _ in range(3):
            self.assertIsNone(self.store.consume("openrouter_paid", token, "model")[0])
        self.assertEqual(self.store.consume("openrouter_paid", token, "model")[0], "approval_exhausted")

    def test_unlimited_uses(self) -> None:
        token, _ = self.store.issue("openrouter_paid", "model", max_uses=0)
        for _ in range(5):
            self.assertIsNone(self.store.consume("openrouter_paid", token, "model")[0])

    def test_concurrent_spend_of_a_single_use_token_admits_exactly_one(self) -> None:
        """The race that let two live GUI tasks share one approval."""
        token, _ = self.store.issue("agent_s_gui_task", "x", max_uses=1)
        results: list = []
        barrier = threading.Barrier(12)

        def attempt() -> None:
            barrier.wait()
            results.append(self.store.consume("agent_s_gui_task", token, "x")[0])

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(None), 1)
        self.assertEqual(results.count("approval_exhausted"), 11)
        self.assertEqual(json.loads(self.path.read_text())["tokens"][token]["used"], 1)


class TestRevokePruneList(ApprovalTestCase):
    def test_revoke(self) -> None:
        token, _ = self.store.issue("agent_s_gui_task", "x")
        self.assertTrue(self.store.revoke(token))
        self.assertFalse(self.store.revoke(token))
        self.assertEqual(self.store.validate("agent_s_gui_task", token, "x")[0], "unknown_approval_token")

    def test_prune_removes_expired_and_exhausted(self) -> None:
        good, _ = self.store.issue("agent_s_gui_task", "a", ttl_seconds=3600)
        spent, _ = self.store.issue("agent_s_gui_task", "b", ttl_seconds=3600)
        self.store.consume("agent_s_gui_task", spent, "b")
        expired, _ = self.store.issue("agent_s_gui_task", "c", ttl_seconds=1)
        doc = json.loads(self.path.read_text())
        doc["tokens"][expired]["expires_at"] = time.time() - 10
        self.write_raw(doc)
        self.assertEqual(self.store.prune(), 2)
        remaining = set(self.store.tokens())
        self.assertIn(good, remaining)
        self.assertNotIn(expired, remaining)
        self.assertNotIn(spent, remaining)

    def test_list_masks_tokens(self) -> None:
        token, _ = self.store.issue("agent_s_gui_task", "x")
        listed = self.store.list()
        self.assertEqual(len(listed), 1)
        self.assertNotIn(token, json.dumps(listed))
        self.assertEqual(listed[0]["action"], "agent_s_gui_task")

    def test_describe_unknown_token(self) -> None:
        self.assertIsNone(self.store.describe("nope"))

    def test_corrupt_file_is_survivable(self) -> None:
        self.path.write_text("{oops")
        self.assertEqual(self.store.tokens(), {})
        token, _ = self.store.issue("agent_s_gui_task", "x")
        self.assertIsNone(self.store.validate("agent_s_gui_task", token, "x")[0])


if __name__ == "__main__":
    unittest.main()
