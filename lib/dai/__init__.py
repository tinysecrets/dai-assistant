"""Shared internals for the Debian AI Assistant spine.

Used by the two in-repo services (``services/model-router``,
``services/agent-s-worker``).  Vellum skills under ``skills/`` are copied out
of this tree when they are installed, so they deliberately stay
self-contained and must NOT import from here.

Everything is stdlib-only: the spine has to run on a bare Debian install
with nothing but ``python3``.
"""

from __future__ import annotations

from .env import Config, env_bool, env_float, env_int, env_str, load_dotenv, parse_dotenv
from .httpclient import HttpResponse, open_stream, request_json
from .httpserver import HttpError, JsonHandler, ServiceInfo, ServiceStartupError, create_server, serve_forever
from .jsonio import AtomicJsonStore, CachedJsonFile, atomic_write_json, load_json, save_json
from .redact import REDACTED, Redactor, redact

__all__ = [
    "AtomicJsonStore",
    "CachedJsonFile",
    "Config",
    "HttpError",
    "HttpResponse",
    "JsonHandler",
    "REDACTED",
    "Redactor",
    "ServiceInfo",
    "atomic_write_json",
    "create_server",
    "env_bool",
    "env_float",
    "env_int",
    "env_str",
    "load_dotenv",
    "load_json",
    "open_stream",
    "parse_dotenv",
    "redact",
    "request_json",
    "save_json",
    "serve_forever",
    "ServiceStartupError",
]

__version__ = "1.0.0"
