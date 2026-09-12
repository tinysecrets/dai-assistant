"""``.env`` parsing and typed environment access.

The spine never ``source``s ``.env`` from a shell: values may contain spaces,
quotes or ``#``, and sourcing also exports every secret into every child
process.  Everything reads keys through this module instead.

Supported syntax (a deliberate, documented subset of ``python-dotenv``):

* ``KEY=value`` and ``export KEY=value``
* single/double quoted values — quotes are stripped, inner text kept verbatim
* unquoted values — a trailing `` # comment`` is stripped
* blank lines and ``#`` comment lines
* values must be on one line (no multi-line continuation)

Real environment variables always win over the file, so an operator can
override a single key for one command without editing ``.env``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

_EXPORT_PREFIX = re.compile(r"^export\s+")
_INLINE_COMMENT = re.compile(r"\s+#.*$")

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def parse_dotenv(text: str) -> Dict[str, str]:
    """Parse ``.env`` content into a dict.  Later lines win over earlier ones."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = _EXPORT_PREFIX.sub("", line, count=1).strip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = _INLINE_COMMENT.sub("", value).strip()
        out[key] = value
    return out


def load_dotenv(path: Path | str, *, override: bool = False) -> Dict[str, str]:
    """Load ``path`` into ``os.environ``.

    Missing files are not an error: the spine starts key-less and reports what
    is missing.  Returns the parsed mapping (empty when the file is absent).
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    parsed = parse_dotenv(text)
    for key, value in parsed.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed


def env_str(name: str, default: str = "", *, env: Optional[Mapping[str, str]] = None) -> str:
    source = os.environ if env is None else env
    value = source.get(name)
    if value is None or value == "":
        return default
    return value


def env_int(name: str, default: int, *, env: Optional[Mapping[str, str]] = None) -> int:
    raw = env_str(name, "", env=env)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float, *, env: Optional[Mapping[str, str]] = None) -> float:
    raw = env_str(name, "", env=env)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool, *, env: Optional[Mapping[str, str]] = None) -> bool:
    raw = env_str(name, "", env=env).strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


@dataclass
class Config:
    """Resolved runtime configuration for a spine service.

    Built once at startup from the environment (which ``load_dotenv`` has
    already merged with ``.env``).  Keeping this in one object means handlers
    never read ``os.environ`` mid-request, and tests can construct a config
    directly instead of mutating process state.
    """

    root: Path
    host: str = "127.0.0.1"
    port: int = 0
    env_path: Path = field(default_factory=lambda: Path(".env"))
    auth_token: str = ""
    max_body_bytes: int = 8 * 1024 * 1024
    request_timeout: float = 120.0
    quiet: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)

    def public_dict(self) -> Dict[str, Any]:
        """Config summary safe to expose over HTTP (never includes secrets)."""
        return {
            "host": self.host,
            "port": self.port,
            "env_path": str(self.env_path),
            "env_exists": self.env_path.exists(),
            "auth_required": bool(self.auth_token),
            "max_body_bytes": self.max_body_bytes,
            "request_timeout": self.request_timeout,
            **{k: v for k, v in self.extra.items() if not k.startswith("_")},
        }
