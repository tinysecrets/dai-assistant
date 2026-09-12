"""Test support: in-process fixtures for the spine services.

Everything runs on ephemeral ports against a stub OpenAI-compatible upstream,
with every state path redirected into a temp directory, so the suite never
touches the repo's real ``state/``, ``policy/approvals.json`` or ``.env``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import stat
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_MODULES: Dict[str, Any] = {}


def stdlib_via_find_spec(name: str) -> bool:
    """True when `name` resolves into the interpreter's own stdlib tree.

    This is the version-proof implementation: it works on Python 3.9, where
    ``sys.stdlib_module_names`` does not exist.  It resolves the module and
    checks that its file lives under the stdlib directory rather than under
    site-packages / dist-packages.

    Kept separate from ``is_stdlib_module`` so it can be tested directly on a
    modern interpreter and compared against the authoritative set — otherwise
    the code path that runs on the oldest supported Python would only ever be
    exercised in CI.
    """
    if not name:
        return False
    if name in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        # ValueError: a parent package with a bogus __path__; ImportError: the
        # module's own loader choked.  Either way it is not plain stdlib.
        return False
    if spec is None:
        return False
    origin = spec.origin or ""
    if origin in ("built-in", "frozen"):
        return True
    if not origin:
        # A namespace package has no origin; fall back to its search path.
        locations = list(getattr(spec, "submodule_search_locations", None) or [])
        origin = locations[0] if locations else ""
    if not origin:
        return False
    paths = sysconfig.get_paths()
    stdlib = paths.get("stdlib", "")
    if not stdlib or not origin.startswith(stdlib):
        return False
    # On layouts where site-packages lives *under* the stdlib prefix, exclude it
    # explicitly so a third-party install is not mistaken for stdlib.
    for key in ("purelib", "platlib"):
        extra = paths.get(key) or ""
        if extra and extra != stdlib and origin.startswith(extra):
            return False
    return "site-packages" not in origin and "dist-packages" not in origin


def is_stdlib_module(name: str) -> bool:
    """True when `name` is part of the standard library, on any supported Python.

    Uses ``sys.stdlib_module_names`` when it exists (3.10+) because it is
    authoritative and cheap, and falls back to ``stdlib_via_find_spec`` on 3.9.

    Reading that attribute directly is what broke the 3.9 CI job: it was
    evaluated in a class body, so tests/test_repo_consistency.py failed at import
    time, which dropped ~69 drift tests and reported as a mystery suite failure
    rather than as a version problem.
    """
    known = getattr(sys, "stdlib_module_names", None)
    if known is not None:
        return name in known or name in sys.builtin_module_names
    return stdlib_via_find_spec(name)


def load_service(name: str, relpath: str) -> Any:
    """Import a service module whose directory name is not a valid identifier."""
    if name in _MODULES:
        return _MODULES[name]
    path = ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _MODULES[name] = module
    return module


def router_module() -> Any:
    return load_service("dai_model_router", "services/model-router/server.py")


def worker_module() -> Any:
    return load_service("dai_agent_s_worker", "services/agent-s-worker/server.py")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --- stub upstream ---------------------------------------------------------


class StubUpstream:
    """A programmable OpenAI-compatible provider.

    ``rule("limited", status=429, body={...})`` makes any request whose model
    contains ``limited`` answer that way; everything else gets a normal chat
    completion (streamed when the client asks for it).
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.rules: List[Dict[str, Any]] = []
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def rule(
        self,
        match: str,
        *,
        status: int = 200,
        body: Optional[Dict[str, Any]] = None,
        path: str = "",
        force_json: bool = False,
    ) -> None:
        """Register a canned response for any model containing ``match``.

        ``force_json`` answers a streaming request with a plain JSON body, to
        exercise the router's "upstream ignored stream=true" path.
        """
        self.rules.append(
            {"match": match, "status": status, "body": body or {}, "path": path, "force_json": force_json}
        )

    def start(self) -> "StubUpstream":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                pass

            def _read(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw.decode() or "{}")
                except json.JSONDecodeError:
                    return {}

            def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path.endswith("/health"):
                    self._send(200, {"ok": True, "service": "stub-upstream", "ready_for_chat": True})
                    return
                if self.path.endswith("/api/tags"):
                    self._send(200, {"models": [{"name": "llama3.2:3b"}]})
                    return
                self._send(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                body = self._read()
                model = str(body.get("model") or "")
                stub.calls.append(
                    {
                        "path": self.path,
                        "model": model,
                        "stream": bool(body.get("stream")),
                        "authorization": self.headers.get("Authorization"),
                        "body_keys": sorted(body.keys()),
                        "body": body,
                    }
                )
                if "reflect" in model:
                    # A provider that echoes request data into its error body.
                    self._send(
                        500,
                        {"error": {"message": f"upstream saw header {self.headers.get('Authorization')}"}},
                    )
                    return
                for rule in stub.rules:
                    if rule["match"] in model and (not rule["path"] or rule["path"] in self.path):
                        if rule["status"] == 200 and body.get("stream") and not rule.get("force_json"):
                            self._stream(model, rule["body"].get("content", "stub ok"))
                            return
                        self._send(rule["status"], rule["body"] or {"ok": True, "model": model})
                        return
                if body.get("stream"):
                    self._stream(model, "Hello world")
                    return
                self._send(
                    200,
                    {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion",
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "Hello world"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                    },
                )

            def _stream(self, model: str, content: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                frames = [
                    {
                        "id": "c1",
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    },
                    {
                        "id": "c1",
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                    },
                    {
                        "id": "c1",
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                    },
                ]
                for frame in frames:
                    self._chunk(f"data: {json.dumps(frame)}\n\n".encode())
                self._chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def _chunk(self, data: bytes) -> None:
                self.wfile.write(b"%x\r\n%b\r\n" % (len(data), data))
                self.wfile.flush()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def calls_for(self, path_fragment: str = "") -> List[Dict[str, Any]]:
        return [c for c in self.calls if path_fragment in c["path"]]

    def reset(self) -> None:
        self.calls.clear()


# --- HTTP client helper ----------------------------------------------------


def http_request(
    method: str,
    url: str,
    body: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
    raw_body: Optional[bytes] = None,
) -> Tuple[int, Any, Dict[str, str], bytes]:
    """Return ``(status, parsed_json_or_None, headers, raw_bytes)``.

    A dropped connection (the bug this suite guards against) raises instead of
    returning, which fails the test loudly.
    """
    data = raw_body
    request_headers = dict(headers or {})
    if data is None and body is not None:
        data = json.dumps(body).encode()
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _maybe_json(raw), dict(resp.headers.items()), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, _maybe_json(raw), dict(exc.headers.items()), raw


def _maybe_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None


def get(url: str, **kwargs: Any) -> Tuple[int, Any, Dict[str, str], bytes]:
    return http_request("GET", url, **kwargs)


def post(url: str, body: Any = None, **kwargs: Any) -> Tuple[int, Any, Dict[str, str], bytes]:
    return http_request("POST", url, body=body, **kwargs)


# --- service fixtures ------------------------------------------------------


class TempSpine:
    """A temp directory holding every file the services would otherwise read."""

    def __init__(
        self,
        *,
        pool: Optional[Dict[str, Any]] = None,
        policy: Optional[Dict[str, Any]] = None,
        catalog: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="dai-test-"))
        (self.dir / "config").mkdir()
        (self.dir / "policy").mkdir()
        (self.dir / "state").mkdir()
        self.pool_path = self.dir / "config" / "rotation-pool.json"
        self.catalog_path = self.dir / "config" / "free-models.json"
        self.policy_path = self.dir / "policy" / "sovereign.json"
        self.approvals_path = self.dir / "policy" / "approvals.json"
        self.env_path = self.dir / ".env"
        self.pool_path.write_text(json.dumps(pool if pool is not None else default_pool()))
        self.catalog_path.write_text(json.dumps(catalog if catalog is not None else default_catalog()))
        self.policy_path.write_text(json.dumps(policy if policy is not None else default_policy()))
        self.env = dict(env or {})

    def write_env(self, values: Dict[str, str]) -> None:
        self.env_path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
        self.env_path.chmod(0o600)
        self.env.update(values)

    def router_env(self, upstream_url: str, **extra: str) -> Dict[str, str]:
        env = {
            "DAI_ENV": str(self.env_path),
            "DAI_POOL": str(self.pool_path),
            "DAI_CATALOG": str(self.catalog_path),
            "DAI_POLICY": str(self.policy_path),
            "DAI_APPROVALS": str(self.approvals_path),
            "DAI_ROUTER_STATE": str(self.dir / "state" / "cooldowns.json"),
            "DAI_STATS_STATE": str(self.dir / "state" / "stats.json"),
            "DAI_OPENROUTER_BASE_URL": upstream_url,
            "DAI_GROQ_BASE_URL": upstream_url,
            "DAI_CEREBRAS_BASE_URL": upstream_url,
            "DAI_OLLAMA_CLOUD_BASE_URL": upstream_url,
            "DAI_OLLAMA_LOCAL_BASE_URL": "http://127.0.0.1:1/v1",  # closed port: local is down
            "DAI_ROUTER_HOST": "127.0.0.1",
            "DAI_QUIET_LOGS": "1",
            "DAI_ROUTER_PORT": "0",
            "DAI_COOLDOWN_SECONDS": "30",
            "OPENROUTER_API_KEY": "sk-or-v1-TESTKEY-DO-NOT-LEAK-1234567890",
        }
        env.update(extra)
        return env

    def worker_env(self, **extra: str) -> Dict[str, str]:
        env = {
            "DAI_ENV": str(self.env_path),
            "DAI_POLICY": str(self.policy_path),
            "DAI_APPROVALS": str(self.approvals_path),
            "DAI_AGENT_S_STATE": str(self.dir / "state" / "tasks"),
            "DAI_AGENT_S_HOST": "127.0.0.1",
            "DAI_QUIET_LOGS": "1",
            "DAI_AGENT_S_PORT": "0",
            "DAI_AGENT_S_VENV": str(self.dir / "venv"),
            "DAI_MODEL_ROUTER": "http://127.0.0.1:1",
        }
        env.update(extra)
        return env

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def default_pool() -> Dict[str, Any]:
    return {
        "version": 1,
        "openrouter_free": ["alpha/one:free", "beta/two:free"],
        "openrouter_free_vision": ["alpha/one:free"],
        "groq_models": ["groq-fast"],
        "ollama_cloud_models": [],
        "ollama_local_models": [],
    }


def default_catalog() -> Dict[str, Any]:
    return {
        "updated": 1788102436,
        "models": [
            {
                "id": "alpha/one:free",
                "architecture": {"input_modalities": ["text", "image"]},
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "vision/extra:free",
                "architecture": {"input_modalities": ["text", "image"]},
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "paid/vision-model",
                "architecture": {"input_modalities": ["text", "image"]},
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {"id": "text/only:free", "architecture": {"input_modalities": ["text"]}, "pricing": {"prompt": "0"}},
        ],
    }


def default_policy() -> Dict[str, Any]:
    return {
        "version": 3,
        "mode": "cloud_first_free_rotation",
        "inference": {
            "default_model": "dai/auto",
            "openrouter": {"enabled": True, "paid_enabled": False},
            "groq": {"enabled": True},
            "cerebras": {"enabled": True},
            "ollama_cloud": {"enabled": True},
            "ollama_local": {"enabled": True},
        },
        "agent_s": {
            "enabled": True,
            "dry_run_default": True,
            "bind_owner_live_desktop": False,
            "require_approval_token": True,
            "display": ":99",
            "worker_url": "http://127.0.0.1:8765",
            "max_steps_default": 15,
            "max_steps_hard_cap": 20,
            "timeout_seconds": 5,
            "grounding_model": "dai/vision-auto",
            "grounding_width": 1280,
            "grounding_height": 800,
        },
    }


class ServiceFixture:
    """Runs one service in-process on an ephemeral port."""

    def __init__(self, httpd: Any) -> None:
        self.httpd = httpd
        self.port = int(httpd.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"service on port {self.port} never accepted connections")

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get(self, path: str, **kwargs: Any) -> Tuple[int, Any, Dict[str, str], bytes]:
        return http_request("GET", self.url(path), **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Tuple[int, Any, Dict[str, str], bytes]:
        return http_request("POST", self.url(path), body=body, **kwargs)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        # shutdown() blocks until serve_forever returns, so this join is about
        # being explicit that no request thread outlives the fixture.
        self._thread.join(timeout=5)


def start_router(
    spine: TempSpine, upstream: StubUpstream, env: Optional[Dict[str, str]] = None
) -> Tuple[ServiceFixture, Any]:
    module = router_module()
    merged = spine.router_env(upstream.base_url)
    merged.update(env or {})
    runtime = module.build_runtime(merged)
    config = module.build_config(runtime, merged)
    config.port = 0  # ephemeral
    httpd = module.create_server(
        config,
        module.RouterHandler,
        service=module.SERVICE,
        redactor=runtime.redactor,
        bind={"runtime": runtime, "timeout": 30},
    )
    return ServiceFixture(httpd), runtime


def start_worker(spine: TempSpine, env: Optional[Dict[str, str]] = None) -> Tuple[ServiceFixture, Any]:
    module = worker_module()
    merged = spine.worker_env()
    merged.update(env or {})
    runtime = module.build_runtime(merged)
    config = module.build_config(runtime, merged)
    config.port = 0
    module.start_workers(runtime)
    httpd = module.create_server(
        config,
        module.WorkerHandler,
        service=module.SERVICE,
        redactor=runtime.redactor,
        bind={"runtime": runtime, "timeout": 30},
    )
    return ServiceFixture(httpd), runtime


def wait_for(predicate: Any, *, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --- fake agent_s / xset binaries -----------------------------------------


def write_executable(path: Path, script: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


FAKE_AGENT_S = """#!/usr/bin/env bash
# Test double for gui-agents' agent_s CLI.
echo "agent_s starting task: $*"
mkdir -p .
printf 'fakepng' > screenshot.png
if [[ "${AGENT_S_TEST_FAIL:-0}" == "1" ]]; then echo "boom" >&2; exit 3; fi
if [[ "${AGENT_S_TEST_HANG:-0}" == "1" ]]; then sleep 60; fi
echo "agent_s done"
exit 0
"""

FAKE_AGENT_S_HANG = """#!/usr/bin/env bash
echo "agent_s starting"
sleep 60
"""

FAKE_AGENT_S_FAIL = """#!/usr/bin/env bash
echo "agent_s starting" >&2
echo "grounding model could not see the screen" >&2
exit 3
"""

FAKE_AGENT_S_LEAK = """#!/usr/bin/env bash
# Prints the api key it was handed, to prove worker output is redacted.
echo "agent_s was given --model_api_key $4"
echo "secret-in-log sk-or-v1-abcdef1234567890"
exit 0
"""

XSET_OK = """#!/usr/bin/env bash
exit 0
"""

XSET_FAIL = """#!/usr/bin/env bash
exit 1
"""


class PathPrepend:
    """Temporarily put a directory of test shims at the front of ``PATH``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._saved: Optional[str] = None

    def __enter__(self) -> "PathPrepend":
        self._saved = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.directory}:{self._saved}"
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._saved is not None:
            os.environ["PATH"] = self._saved
