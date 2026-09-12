"""Secret redaction for anything that leaves the process (logs, errors, HTTP).

The previous implementation leaked: ``redact("sk-or-v1-abc...")`` produced
``sk-or-v1-abcdef12345678=[REDACTED]`` because the key-shaped pattern consumed
the secret and the trailing ``\\S+`` backtracked into it, leaving all but the
last character in the output.

Two layers now:

1. **Registered values** — the services know their real keys, so exact values
   are replaced first.  This catches any format, including keys that match no
   known pattern.
2. **Patterns** — labelled assignments (``api_key=...``, ``Authorization: ...``)
   and well-known token shapes (``sk-``, ``gsk_``, ``ghp_``, ``AIza``, ...).

Both replace the *whole* secret; nothing is left partially visible.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Pattern

REDACTED = "[REDACTED]"

# Label followed by ``=``/``:`` and a value: keep the label, drop the value.
# The label may be preceded by other word characters (``GROQ_API_KEY``).
#
# An explicit separator is required on purpose.  Allowing a bare space would
# turn ordinary prose ("no secrets here") into "no secrets=[REDACTED]", which
# makes logs useless; real secrets are caught by the shape rules and by the
# registered-value layer below instead.
_LABELED = re.compile(
    r"""(?ix)
    (
        [A-Za-z0-9_]*
        (?:
            api[_-]?key | access[_-]?token | refresh[_-]?token | auth(?:orization)? |
            token | secret(?:[_-]?key)? | pass(?:word|phrase|wd)? | credential |
            private[_-]?key | session[_-]?token | x-api-key | proxy-authorization
        )
    )
    \s* [:=>]{1,2} \s*
    ( (?:bearer|basic)\s+ )?
    ( " [^"]* " | ' [^']* ' | [^\s,;:"'}\]\)]+ )
    """,
)

# ``Bearer <token>`` / ``Basic <token>`` with no separator.  The value must
# contain a digit so ordinary prose after the word "bearer" is left alone.
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=-]*\d[A-Za-z0-9._~+/=-]{7,})")

# Well-known token shapes, matched whole so nothing survives.
_TOKEN_SHAPES: List[Pattern[str]] = [
    re.compile(r"\bsk-(?:or-v1-|ant-|proj-|svc-)?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bgsk_[A-Za-z0-9]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),  # JWT
    re.compile(r"\b(?:csk|hf)_[A-Za-z0-9]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


_REDACTED_STEM = REDACTED[:-1]  # "[REDACTED" — the value charset excludes "]"


def _sub_labeled(match: "re.Match[str]") -> str:
    value = match.group(3)
    if _REDACTED_STEM in value:  # already scrubbed by a shape/bearer rule
        return match.group(0)
    return f"{match.group(1)}={REDACTED}"


def _sub_bearer(match: "re.Match[str]") -> str:
    if _REDACTED_STEM in match.group(2):
        return match.group(0)
    return f"{match.group(1)} {REDACTED}"

_MIN_REGISTERED_LEN = 8


class Redactor:
    """Reusable redactor with a registry of known secret values."""

    def __init__(self, secrets: Optional[Iterable[str]] = None) -> None:
        self._secrets: List[str] = []
        for value in secrets or ():
            self.register(value)

    def register(self, value: Optional[str]) -> None:
        """Register a secret value for exact-match redaction.

        Short values are ignored: redacting every occurrence of e.g. ``"abc"``
        would mangle ordinary text.
        """
        if not value:
            return
        value = value.strip().strip("\"'")
        if len(value) < _MIN_REGISTERED_LEN or value in self._secrets:
            return
        self._secrets.append(value)
        # Longest first so a prefix of another secret cannot survive.
        self._secrets.sort(key=len, reverse=True)

    @property
    def registered(self) -> List[str]:
        return list(self._secrets)

    def redact(self, text: Optional[str]) -> str:
        if not text:
            return ""
        out = str(text)
        for secret in self._secrets:
            if secret in out:
                out = out.replace(secret, REDACTED)
        for pattern in _TOKEN_SHAPES:
            out = pattern.sub(REDACTED, out)
        out = _BEARER.sub(_sub_bearer, out)
        out = _LABELED.sub(_sub_labeled, out)
        return out

    def __call__(self, text: Optional[str]) -> str:
        return self.redact(text)


_DEFAULT = Redactor()


def redact(text: Optional[str]) -> str:
    """Redact using the module-level default registry."""
    return _DEFAULT.redact(text)


def register_secret(value: Optional[str]) -> None:
    """Add a secret to the module-level default registry."""
    _DEFAULT.register(value)


def default_redactor() -> Redactor:
    return _DEFAULT
