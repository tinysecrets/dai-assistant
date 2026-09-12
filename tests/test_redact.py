"""Redaction must never leak a secret — not even a prefix of one."""

from __future__ import annotations

import unittest

from lib.dai.redact import REDACTED, Redactor, redact

SECRET = "sk-or-v1-abcdef1234567890"
FRAGMENTS = ("abcdef1234567890", "sk-or-v1-abcdef", "1234567890", "abcdef123")


class TestPatternRedaction(unittest.TestCase):
    def test_bare_key_is_fully_redacted(self) -> None:
        """Regression: the old regex left ``sk-or-v1-abcdef12345678=[REDACTED]``."""
        out = redact(f"prefix {SECRET} suffix")
        self.assertIn(REDACTED, out)
        for fragment in FRAGMENTS:
            self.assertNotIn(fragment, out)

    def test_bearer_header(self) -> None:
        out = redact(f"Authorization: Bearer {SECRET}")
        for fragment in FRAGMENTS:
            self.assertNotIn(fragment, out)

    def test_labeled_assignment(self) -> None:
        for text in (
            "OPENROUTER_API_KEY=super-secret-value",
            "api_key: super-secret-value",
            "GROQ_API_KEY='super-secret-value'",
            'token="super-secret-value"',
            "password=hunter2hunter2",
        ):
            with self.subTest(text=text):
                self.assertNotIn("super-secret-value", redact(text))
                self.assertNotIn("hunter2hunter2", redact(text))

    def test_known_token_shapes(self) -> None:
        shapes = {
            "gsk_AbC123XyZ987654321": "gsk_",
            "ghp_1234567890abcdefghijklmnopqrstuv": "ghp_",
            "AIza1234567890abcdefghijklmn": "AIza",
            "xoxb-1234567890-abcdefghijklmnop": "xoxb-",
            "hf_abcdefghijklmnopqrstuv": "hf_",
        }
        for token, prefix in shapes.items():
            with self.subTest(token=prefix):
                out = redact(f"found {token} in output")
                self.assertNotIn(token, out)
                self.assertNotIn(token[6:], out)

    def test_jwt(self) -> None:
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij1234567890"
        self.assertNotIn(token, redact(f"auth {token}"))

    def test_bearer_scheme_with_token(self) -> None:
        out = redact("Authorization: Bearer abcdef1234567890xyz")
        self.assertNotIn("abcdef1234567890xyz", out)

    def test_already_redacted_header_is_not_double_mangled(self) -> None:
        out = redact(f"Authorization: Bearer {SECRET}")
        self.assertNotIn(f"{REDACTED}{REDACTED}", out)
        for fragment in FRAGMENTS:
            self.assertNotIn(fragment, out)

    def test_unknown_shape_behind_a_label_is_caught(self) -> None:
        self.assertNotIn("zzz-unknown-format", redact("OLLAMA_API_KEY=zzz-unknown-format"))

    def test_private_key_block(self) -> None:
        text = "-----BEGIN PRIVATE KEY-----\nMIIEvQQ\nsecretline\n-----END PRIVATE KEY-----"
        self.assertNotIn("MIIEvQQ", redact(text))


class TestRegisteredSecrets(unittest.TestCase):
    def test_registered_value_redacted_regardless_of_shape(self) -> None:
        odd = "aGVsbG8td29ybGQ-not-a-known-shape"
        r = Redactor([odd])
        out = r.redact(f"leaked {odd} here")
        self.assertNotIn(odd, out)
        self.assertIn(REDACTED, out)

    def test_longest_registered_secret_wins(self) -> None:
        short = "ABCDEFGHIJ1234"
        long = f"{short}5678"
        r = Redactor([short, long])
        out = r.redact(f"value {long}")
        self.assertNotIn(short, out)

    def test_short_values_are_not_registered(self) -> None:
        """Registering ``"abc"`` would mangle ordinary prose."""
        r = Redactor(["abc"])
        self.assertEqual(r.registered, [])
        self.assertEqual(r.redact("abc def"), "abc def")

    def test_quotes_are_stripped_before_registering(self) -> None:
        r = Redactor()
        r.register('"sk-or-v1-abcdef1234567890"')
        self.assertNotIn("abcdef1234567890", r.redact("key=sk-or-v1-abcdef1234567890"))


class TestNoFalsePositives(unittest.TestCase):
    def test_prose_is_untouched(self) -> None:
        for text in (
            "no secrets here, just prose about operating rates",
            "the bearer of bad news arrived",
            "token bucket rate limiting was discussed",
            "authorization is handled by the policy gate",
            "credentials rotation happens nightly",
            "Failed to operate on the resource",
            "the generate endpoint returned 500",
            "temperature must be between 0 and 2",
            "model-router listening on http://127.0.0.1:11435",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_empty_and_none(self) -> None:
        self.assertEqual(redact(""), "")
        self.assertEqual(redact(None), "")

    def test_idempotent(self) -> None:
        once = redact(f"key {SECRET}")
        self.assertEqual(redact(once), once)


if __name__ == "__main__":
    unittest.main()
