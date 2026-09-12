"""``.env`` parsing and typed environment access."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from lib.dai.env import Config, env_bool, env_float, env_int, env_str, load_dotenv, parse_dotenv


class TestParseDotenv(unittest.TestCase):
    def test_basic_shapes(self) -> None:
        text = "\n".join(
            [
                "# a comment",
                "",
                "PLAIN=value",
                "  SPACED  =  value  ",
                "export EXPORTED=value",
                'DOUBLE="quoted value"',
                "SINGLE='single quoted'",
                "QUOTED_HASH=\"keep # this\"",
                "UNQUOTED_HASH=value # strip this",
                "EMPTY=",
                "NUMBER=11435",
                "URL=https://localhost/debian-ai",
            ]
        )
        parsed = parse_dotenv(text)
        self.assertEqual(parsed["PLAIN"], "value")
        self.assertEqual(parsed["SPACED"], "value")
        self.assertEqual(parsed["EXPORTED"], "value")
        self.assertEqual(parsed["DOUBLE"], "quoted value")
        self.assertEqual(parsed["SINGLE"], "single quoted")
        self.assertEqual(parsed["QUOTED_HASH"], "keep # this")
        self.assertEqual(parsed["UNQUOTED_HASH"], "value")
        self.assertEqual(parsed["EMPTY"], "")
        self.assertEqual(parsed["NUMBER"], "11435")
        self.assertEqual(parsed["URL"], "https://localhost/debian-ai")

    def test_values_with_spaces_survive(self) -> None:
        """The reason the spine never ``source``s ``.env`` from a shell."""
        self.assertEqual(parse_dotenv('TITLE="Debian AI Assistant"')["TITLE"], "Debian AI Assistant")

    def test_invalid_keys_are_skipped(self) -> None:
        parsed = parse_dotenv("\n".join(["no-equals-line", "1NOPE=x", "BAD KEY=y", "=orphan", "OK=1"]))
        self.assertEqual(parsed, {"OK": "1"})

    def test_later_line_wins(self) -> None:
        self.assertEqual(parse_dotenv("A=1\nA=2")["A"], "2")

    def test_value_containing_equals(self) -> None:
        self.assertEqual(parse_dotenv("TOKEN=abc=def==")["TOKEN"], "abc=def==")


class TestLoadDotenv(unittest.TestCase):
    def test_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(load_dotenv(Path(tempfile.gettempdir()) / "definitely-missing.env"), {})

    def test_real_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("DAI_TEST_VAR=from_file\n")
            os.environ["DAI_TEST_VAR"] = "from_env"
            try:
                load_dotenv(path)
                self.assertEqual(os.environ["DAI_TEST_VAR"], "from_env")
                load_dotenv(path, override=True)
                self.assertEqual(os.environ["DAI_TEST_VAR"], "from_file")
            finally:
                os.environ.pop("DAI_TEST_VAR", None)


class TestTypedAccessors(unittest.TestCase):
    env = {"S": "text", "I": "42", "BAD_I": "not-a-number", "F": "1.5", "BAD_F": "x",
           "T": "true", "F_FALSE": "off", "EMPTY": ""}

    def test_env_str(self) -> None:
        self.assertEqual(env_str("S", "d", env=self.env), "text")
        self.assertEqual(env_str("MISSING", "d", env=self.env), "d")
        self.assertEqual(env_str("EMPTY", "d", env=self.env), "d")

    def test_env_int(self) -> None:
        self.assertEqual(env_int("I", 0, env=self.env), 42)
        self.assertEqual(env_int("BAD_I", 7, env=self.env), 7)
        self.assertEqual(env_int("MISSING", 7, env=self.env), 7)

    def test_env_float(self) -> None:
        self.assertEqual(env_float("F", 0.0, env=self.env), 1.5)
        self.assertEqual(env_float("BAD_F", 2.5, env=self.env), 2.5)

    def test_env_bool(self) -> None:
        for truthy in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(env_bool("V", False, env={"V": truthy}), truthy)
        for falsey in ("0", "false", "no", "off"):
            self.assertFalse(env_bool("V", True, env={"V": falsey}), falsey)
        self.assertTrue(env_bool("V", True, env={"V": "garbage"}))

    def test_env_bool_empty_value_means_unset(self) -> None:
        """``KEY=`` in .env is "not configured", not "false"."""
        self.assertTrue(env_bool("V", True, env={"V": ""}))
        self.assertFalse(env_bool("V", False, env={"V": ""}))
        self.assertFalse(env_bool("MISSING", False, env=self.env))


class TestConfig(unittest.TestCase):
    def test_public_dict_never_includes_the_token(self) -> None:
        config = Config(root=Path("/tmp"), host="127.0.0.1", port=1, auth_token="super-secret-token")
        public = config.public_dict()
        self.assertNotIn("super-secret-token", str(public))
        self.assertTrue(public["auth_required"])


if __name__ == "__main__":
    unittest.main()
