#!/usr/bin/env python3
"""Debian AI model-router — cloud-first free top-model rotation.

OpenAI-compatible API on :11435.  Rotates across OpenRouter ``:free``, Groq,
Cerebras and Ollama Cloud, with local Ollama last.  Keys live only in ``.env``
and are registered with the redactor so they cannot reach a log line or an
error body.

Starts with zero keys: ``/health`` reports what is missing and chat answers
503 with an actionable message instead of dropping the connection.

Endpoints are documented in ``docs/API.md``.  Run ``--check`` to validate the
configuration without serving.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib import parse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.dai import (
    Config,
    HttpError,
    JsonHandler,
    Redactor,
    ServiceInfo,
    create_server,
    env_bool,
    env_float,
    env_int,
    env_str,
    load_dotenv,
    parse_dotenv,
    serve_forever,
)
from lib.dai.approvals import ApprovalStore
from lib.dai.httpclient import iter_sse_lines, open_stream, request_json
from lib.dai.jsonio import AtomicJsonStore, CachedJsonFile
from lib.dai.routing import (
    PROVIDERS,
    Candidate,
    Failure,
    Plan,
    classify_failure,
    is_success,
    keys_needed,
    plan_candidates,
    provider_status,
    rotate_index,
)
from lib.dai.stats import StatsCollector

SERVICE = ServiceInfo("model-router", "1.1", "docs/API.md")
VERSION = SERVICE.version

# Environment variables whose values are always redacted, whatever shape they
# have — a key that matches no known token pattern must still never be logged.
SECRET_ENV_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

# Body fields the router consumes and must not forward upstream.
DAI_BODY_KEYS = ("dai_approval_token", "dai_hint")
DAI_TOKEN_HEADERS = ("x-dai-approval-token",)

UNSUPPORTED_PATHS: Dict[str, str] = {
    "/v1/embeddings": "Vellum embeds locally (ONNX) by default; the router does not proxy embeddings.",
    "/v1/images/generations": "Image generation is not part of the spine.",
    "/v1/moderations": "Moderation is handled upstream by the provider.",
}

# Audio is relayed to the local voice bridge (free/offline STT + TTS).
AUDIO_PATHS = ("/v1/audio/transcriptions", "/v1/audio/speech")


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def build_redactor(env: Mapping[str, str], env_path: Path) -> Redactor:
    """Seed the redactor with every secret-looking value we can see."""
    redactor = Redactor()
    for key, value in env.items():
        if value and len(str(value)) >= 8 and any(part in key.upper() for part in SECRET_ENV_PARTS):
            redactor.register(str(value))
    try:  # values that live in .env but were never exported
        for key, value in parse_dotenv(env_path.read_text(encoding="utf-8")).items():
            if len(value) >= 8 and any(part in key.upper() for part in SECRET_ENV_PARTS):
                redactor.register(value)
    except (OSError, UnicodeDecodeError):
        pass
    return redactor


@dataclass
class RouterRuntime:
    """Everything the handler needs that is not per-request."""

    root: Path
    env_path: Path
    pool_file: CachedJsonFile
    catalog_file: CachedJsonFile
    policy_file: CachedJsonFile
    cooldown_store: AtomicJsonStore
    approvals: ApprovalStore
    stats: StatsCollector
    redactor: Redactor
    env_map: Optional[Mapping[str, str]] = None
    cooldown_seconds: float = 90.0
    short_cooldown: float = 30.0
    provider_cooldown: float = 600.0
    request_timeout: float = 120.0
    stream_timeout: float = 600.0
    strict_models: bool = False
    voice_bridge_url: str = "http://127.0.0.1:8766"
    started_at: float = field(default_factory=time.time)
    _cooldowns: Dict[str, float] = field(default_factory=dict)
    _rr: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- environment ------------------------------------------------------
    def env(self) -> Mapping[str, str]:
        """Effective environment.  ``env_map`` lets tests inject one."""
        return self.env_map if self.env_map is not None else os.environ

    # --- config files -----------------------------------------------------
    def pool(self) -> Dict[str, Any]:
        return self.pool_file.as_dict()

    def catalog(self) -> Dict[str, Any]:
        return self.catalog_file.as_dict()

    def policy(self) -> Dict[str, Any]:
        value = self.policy_file.get()
        return value if isinstance(value, dict) else {}

    def inference_policy(self) -> Dict[str, Any]:
        value = self.policy().get("inference")
        return value if isinstance(value, dict) else {}

    def provider_enabled(self, name: str) -> bool:
        """A provider is on unless the policy explicitly switches it off."""
        if self.inference_policy().get("free_cloud_only") is True and name != "openrouter":
            return False
        cfg = self.inference_policy().get(name)
        return not (isinstance(cfg, dict) and cfg.get("enabled") is False)

    def enabled_providers(self) -> List[str]:
        return [name for name in PROVIDERS if self.provider_enabled(name)]

    def paid_openrouter_enabled(self) -> bool:
        if self.inference_policy().get("free_cloud_only") is True:
            return False
        cfg = self.inference_policy().get("openrouter")
        return bool(isinstance(cfg, dict) and cfg.get("paid_enabled"))

    def default_model(self) -> str:
        return str(self.inference_policy().get("default_model") or "dai/auto")

    def config_errors(self) -> List[str]:
        return [f.error for f in (self.pool_file, self.catalog_file, self.policy_file) if f.error]

    # --- cooldowns --------------------------------------------------------
    def load_cooldowns(self) -> None:
        data = self.cooldown_store.read()
        raw = data.get("cooldowns") if isinstance(data, dict) else None
        now = time.time()
        with self._lock:
            self._cooldowns = {str(k): float(v) for k, v in (raw or {}).items() if _is_number(v) and float(v) > now}

    def cooldowns(self) -> Dict[str, float]:
        now = time.time()
        with self._lock:
            if any(v <= now for v in self._cooldowns.values()):
                self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > now}
            return dict(self._cooldowns)

    def is_cooled(self, key: str) -> bool:
        """Whether a candidate/provider key is currently cooling down."""
        if not key:
            return False
        with self._lock:
            return self._cooldowns.get(key, 0.0) > time.time()

    def set_cooldown(self, key: str, seconds: float) -> None:
        if seconds <= 0 or not key:
            return
        now = time.time()
        with self._lock:
            self._cooldowns[key] = now + seconds
            snapshot = {k: v for k, v in self._cooldowns.items() if v > now}

        def mutate(doc: Any) -> Dict[str, Any]:
            document = doc if isinstance(doc, dict) else {}
            document["cooldowns"] = snapshot
            document["updated_at"] = time.time()
            return document

        self.cooldown_store.update(mutate)

    def clear_cooldowns(self) -> int:
        with self._lock:
            count = len(self._cooldowns)
            self._cooldowns = {}
        self.cooldown_store.write({"cooldowns": {}, "updated_at": time.time()})
        return count

    def next_rr(self) -> int:
        with self._lock:
            self._rr = rotate_index(self._rr)
            return self._rr

    # --- planning ---------------------------------------------------------
    def plan(self, *, task_hint: str = "", preferred: Optional[str] = None) -> Plan:
        return plan_candidates(
            pool=self.pool(),
            catalog=self.catalog(),
            env=self.env(),
            cooldowns=self.cooldowns(),
            now=time.time(),
            rr_index=self.next_rr(),
            task_hint=task_hint,
            preferred=preferred,
            strict_models=self.strict_models,
            enabled_providers=self.enabled_providers(),
            free_cloud_only=self.inference_policy().get("free_cloud_only") is True,
        )

    def providers_configured(self) -> Dict[str, bool]:
        status = provider_status(self.env())
        return {name: bool(status.get(name) and self.provider_enabled(name)) for name in PROVIDERS}

    def local_ollama_up(self) -> bool:
        base = PROVIDERS["ollama_local"].base_url(self.env())
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        code, _ = request_json("GET", f"{base}/api/tags", timeout=2.0, redactor=self.redactor)
        return code == 200

    def voice_bridge_up(self) -> bool:
        parts = parse.urlsplit(self.voice_bridge_url)
        if not parts.netloc:
            return False
        code, _ = request_json("GET", f"{parts.scheme}://{parts.netloc}/health", timeout=1.5, redactor=self.redactor)
        return code == 200

    def health(self, *, probe_local: bool = True) -> Dict[str, Any]:
        status = self.providers_configured()
        configured = [k for k, v in status.items() if v and k != "ollama_local"]
        local_up = bool(status.get("ollama_local")) and probe_local and self.local_ollama_up()
        plan = self.plan()
        return {
            "ok": True,
            "service": SERVICE.name,
            "version": VERSION,
            "mode": self.policy().get("mode", "cloud_first_free_rotation"),
            "ready_for_chat": bool(configured) or local_up,
            "providers_configured": status,
            "providers_enabled": self.enabled_providers(),
            "keys_needed": keys_needed(self.env()),
            "local_ollama": local_up,
            "voice_bridge_url": self.voice_bridge_url,
            "voice_bridge_up": self.voice_bridge_up() if probe_local else None,
            "openrouter_paid_enabled": self.paid_openrouter_enabled(),
            "default_model": self.default_model(),
            "candidates_available": len(plan.candidates),
            "cooldowns_active": len(self.cooldowns()),
            "streaming": True,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "pool": str(self.pool_file.path),
            "config_errors": self.config_errors(),
            "notes": plan.notes,
        }


def build_runtime(env: Optional[Mapping[str, str]] = None, *, root: Path = ROOT) -> RouterRuntime:
    """Resolve paths and settings, load persisted state."""
    source: Mapping[str, str] = os.environ if env is None else env

    def get(name: str, default: str) -> str:
        return str(source.get(name) or default)

    env_path = Path(get("DAI_ENV", str(root / ".env")))
    redactor = build_redactor(source, env_path)
    runtime = RouterRuntime(
        root=root,
        env_path=env_path,
        pool_file=CachedJsonFile(Path(get("DAI_POOL", str(root / "config" / "rotation-pool.json"))), {}),
        catalog_file=CachedJsonFile(Path(get("DAI_CATALOG", str(root / "config" / "free-models.json"))), {}),
        policy_file=CachedJsonFile(Path(get("DAI_POLICY", str(root / "policy" / "sovereign.json"))), {}),
        cooldown_store=AtomicJsonStore(
            Path(get("DAI_ROUTER_STATE", str(root / "state" / "router-cooldowns.json"))), {"cooldowns": {}}
        ),
        approvals=ApprovalStore(Path(get("DAI_APPROVALS", str(root / "policy" / "approvals.json")))),
        stats=StatsCollector(Path(get("DAI_STATS_STATE", str(root / "state" / "router-stats.json")))),
        redactor=redactor,
        env_map=None if env is None else dict(env),
        cooldown_seconds=env_float("DAI_COOLDOWN_SECONDS", 90.0, env=source),
        short_cooldown=env_float("DAI_SHORT_COOLDOWN_SECONDS", 30.0, env=source),
        provider_cooldown=env_float("DAI_PROVIDER_COOLDOWN_SECONDS", 600.0, env=source),
        request_timeout=env_float("DAI_REQUEST_TIMEOUT", 120.0, env=source),
        stream_timeout=env_float("DAI_STREAM_TIMEOUT", 600.0, env=source),
        strict_models=env_bool("DAI_STRICT_MODELS", False, env=source),
        voice_bridge_url=str(get("DAI_VOICE_BRIDGE_URL", "http://127.0.0.1:8766")),
    )
    runtime.load_cooldowns()
    for cached in (runtime.pool_file, runtime.catalog_file, runtime.policy_file):
        cached.get()  # surface a bad file at startup rather than on request #1
    return runtime


def build_config(runtime: RouterRuntime, env: Optional[Mapping[str, str]] = None) -> Config:
    source: Mapping[str, str] = os.environ if env is None else env
    return Config(
        root=runtime.root,
        host=env_str("DAI_ROUTER_HOST", "127.0.0.1", env=source),
        port=env_int("DAI_ROUTER_PORT", 11435, env=source),
        env_path=runtime.env_path,
        auth_token=env_str("DAI_ROUTER_TOKEN", "", env=source),
        max_body_bytes=env_int("DAI_MAX_BODY_BYTES", 8 * 1024 * 1024, env=source),
        request_timeout=runtime.request_timeout,
        quiet=env_bool("DAI_QUIET_LOGS", False, env=source),
        extra={
            "client_timeout": env_int("DAI_CLIENT_TIMEOUT", 180, env=source),
            "pool_path": str(runtime.pool_file.path),
            "policy_path": str(runtime.policy_file.path),
        },
    )


Attempt = Tuple[str, Optional[Failure]]  # ("served" | "client_error" | "failed", failure)


class RouterHandler(JsonHandler):
    runtime: RouterRuntime = None  # type: ignore[assignment]

    @property
    def rt(self) -> RouterRuntime:
        return type(self).runtime

    # --- request parsing --------------------------------------------------
    def approval_token(self, body: Mapping[str, Any]) -> Optional[str]:
        for header in DAI_TOKEN_HEADERS:
            value = self.headers.get(header)
            if value and value.strip():
                return value.strip()
        value = body.get("dai_approval_token")
        return str(value).strip() if value else None

    def task_hint(self, body: Mapping[str, Any]) -> str:
        explicit = body.get("dai_hint")
        if isinstance(explicit, str) and explicit.strip():
            return explicit[:500]
        messages = body.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    return content[:500]
                if isinstance(content, list):  # vision-style content blocks
                    text = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
                    return text[:500] if text.strip() else "image"
                break
        prompt = body.get("prompt")
        return str(prompt)[:500] if isinstance(prompt, str) else ""

    def upstream_payload(self, body: Mapping[str, Any], cand: Candidate, *, stream: bool) -> Dict[str, Any]:
        """Client body minus router-private fields, retargeted at ``cand``.

        Nothing is injected beyond ``model``/``stream``: extra parameters such
        as ``stream_options`` are rejected by some backends (local Ollama), and
        a 400 there would surface as a client error.
        """
        payload = {k: v for k, v in body.items() if k not in DAI_BODY_KEYS}
        payload["model"] = cand.model
        payload["stream"] = stream
        return payload

    # --- dispatch ---------------------------------------------------------
    def dispatch(self, method: str, path: str, query: Dict[str, str]) -> None:
        if path in AUDIO_PATHS:
            self.forward_audio(path)
        elif method in ("GET", "HEAD"):
            self.dispatch_get(path, query)
        elif method == "POST":
            self.dispatch_post(path)
        else:
            raise HttpError(
                405, "method_not_allowed", f"{method} is not supported for {path}", headers={"Allow": "GET, HEAD, POST"}
            )

    def dispatch_get(self, path: str, query: Mapping[str, str]) -> None:
        rt = self.rt
        # /health stays unauthenticated so doctor and monitoring work keyless.
        if path in ("/", "/health"):
            self.send_json(200, rt.health())
            return
        if path == "/ready":
            healthy = rt.health(probe_local=False)
            self.send_json(200 if healthy["ready_for_chat"] else 503, healthy)
            return
        if path == "/version":
            self.send_json(200, {**SERVICE.as_dict(), "python": sys.version.split()[0]})
            return
        if path == "/v1/models":
            self.send_json(200, self.models_payload(query))
            return
        if path in UNSUPPORTED_PATHS:
            raise HttpError(501, "not_implemented", UNSUPPORTED_PATHS[path])

        self.check_auth()
        if path == "/v1/status/keys":
            self.send_json(
                200,
                {
                    "providers": rt.providers_configured(),
                    "keys_needed": keys_needed(rt.env()),
                    "env_file": str(rt.env_path),
                    "env_exists": rt.env_path.exists(),
                    "redacted_secrets": len(rt.redactor.registered),
                },
            )
            return
        if path == "/v1/status/cooldowns":
            now = time.time()
            active = rt.cooldowns()
            self.send_json(
                200, {"count": len(active), "cooldowns": {k: round(v - now, 1) for k, v in sorted(active.items())}}
            )
            return
        if path == "/v1/status/stats":
            self.send_json(200, rt.stats.snapshot())
            return
        if path == "/v1/status/plan":
            self.send_json(200, rt.plan(task_hint=query.get("hint", ""), preferred=query.get("model")).as_dict())
            return
        if path == "/v1/status/config":
            self.send_json(200, self.config_payload())
            return
        raise HttpError(404, "not_found", f"No route for GET {path}. Try /health or /v1/models")

    def dispatch_post(self, path: str) -> None:
        rt = self.rt
        if path == "/v1/status/reset":
            self.check_auth()
            cleared = rt.clear_cooldowns()
            rt.stats.reset()
            for cached in (rt.pool_file, rt.catalog_file, rt.policy_file):
                cached.invalidate()
            self.send_json(
                200,
                {"ok": True, "cooldowns_cleared": cleared, "stats_reset": True, "config_reloaded": True},
            )
            return
        if path in ("/v1/chat/completions", "/v1/completions"):
            self.handle_completion(path)
            return
        if path in UNSUPPORTED_PATHS:
            raise HttpError(501, "not_implemented", UNSUPPORTED_PATHS[path])
        raise HttpError(404, "not_found", f"No route for POST {path}")

    # --- payloads ---------------------------------------------------------
    def config_payload(self) -> Dict[str, Any]:
        rt = self.rt
        return {
            "service": SERVICE.as_dict(),
            "config": self.config.public_dict(),
            "runtime": {
                "cooldown_seconds": rt.cooldown_seconds,
                "short_cooldown": rt.short_cooldown,
                "provider_cooldown": rt.provider_cooldown,
                "request_timeout": rt.request_timeout,
                "stream_timeout": rt.stream_timeout,
                "strict_models": rt.strict_models,
                "providers_enabled": rt.enabled_providers(),
                "paid_openrouter_enabled": rt.paid_openrouter_enabled(),
                "default_model": rt.default_model(),
            },
            "files": {
                "env": str(rt.env_path),
                "pool": str(rt.pool_file.path),
                "catalog": str(rt.catalog_file.path),
                "policy": str(rt.policy_file.path),
                "approvals": str(rt.approvals.path),
                "cooldowns": str(rt.cooldown_store.path),
                "stats": str(rt.stats.store.path) if rt.stats.store else None,
            },
            "pool_summary": {k: len(v) for k, v in rt.pool().items() if isinstance(v, list)},
            "catalog_models": len(rt.catalog().get("models") or []),
            "config_errors": rt.config_errors(),
        }

    def models_payload(self, query: Mapping[str, str]) -> Dict[str, Any]:
        wanted = (query.get("provider") or "").strip()
        models: List[Dict[str, Any]] = []
        seen = set()
        for cand in self.rt.plan().candidates:
            if wanted and cand.provider != wanted:
                continue
            if cand.public_id in seen:
                continue
            seen.add(cand.public_id)
            models.append(
                {
                    "id": cand.public_id,
                    "object": "model",
                    "owned_by": cand.provider,
                    "created": int(time.time()),
                    "dai": {"free": cand.free, "origin": cand.origin},
                }
            )
        if not wanted:
            models = [
                {
                    "id": "dai/vision-auto",
                    "object": "model",
                    "owned_by": "debian-ai",
                    "description": "Rotation across vision-capable free models (GUI grounding)",
                },
                {
                    "id": "dai/auto",
                    "object": "model",
                    "owned_by": "debian-ai",
                    "description": "Rotation across cloud free + local pools (text focused)",
                },
            ] + models
        return {"object": "list", "data": models}

    # --- voice relay ------------------------------------------------------
    def forward_audio(self, path: str) -> None:
        """Byte-level pass-through to the local voice bridge (STT/TTS)."""
        self.check_auth()
        raw = self.rt.voice_bridge_url
        parts = parse.urlsplit(raw)
        if not parts.netloc:
            raise HttpError(501, "voice_bridge_not_configured", "Set DAI_VOICE_BRIDGE_URL in .env.")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        body = self.read_body()
        conn = conn_cls(host, port, timeout=self.rt.request_timeout)
        try:
            conn.request(
                self.command,
                path,
                body=body,
                headers={
                    "Content-Type": self.headers.get("Content-Type") or "application/octet-stream",
                    "Accept": "*/*",
                    "User-Agent": self.server_version,
                },
            )
            response = conn.getresponse()
            data = response.read()
            content_type = response.getheader("Content-Type") or "application/octet-stream"
            self._response_status = response.status
            self._start(
                response.status,
                {"Content-Type": content_type, "Content-Length": str(len(data))},
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (ConnectionRefusedError, ConnectionResetError, OSError, http.client.HTTPException) as exc:
            raise HttpError(
                502, "voice_bridge_unreachable", "The local voice bridge did not answer; is it started?"
            ) from exc
        finally:
            conn.close()

    # --- completions ------------------------------------------------------
    def handle_completion(self, path: str) -> None:
        rt = self.rt
        self.check_auth()
        body = self.read_json(required=True)

        legacy = path == "/v1/completions"
        if legacy:
            if not body.get("prompt"):
                raise HttpError(400, "prompt_required", "/v1/completions needs a 'prompt'.")
        elif not body.get("messages"):
            raise HttpError(400, "messages_required", "/v1/chat/completions needs a non-empty 'messages' array.")

        requested_model = body.get("model") or rt.default_model()
        if not isinstance(requested_model, str) or not requested_model.strip():
            raise HttpError(400, "invalid_model", "'model' must be a non-empty string.")

        token = self.approval_token(body)
        preferred = None if requested_model.strip() in ("dai/auto", "auto", "router/auto") else requested_model.strip()
        plan = rt.plan(task_hint=self.task_hint(body), preferred=preferred)

        # Paid OpenRouter: policy flag, or a one-shot approval token.
        paid_by_policy = rt.paid_openrouter_enabled()
        explicit_paid = next(
            (c for c in plan.candidates if c.origin == "explicit" and c.provider == "openrouter" and not c.free),
            None,
        )
        if explicit_paid is not None and not paid_by_policy and not token:
            raise HttpError(
                403,
                "paid_models_disabled",
                "Paid OpenRouter models need owner approval: set inference.openrouter.paid_enabled in "
                "policy/sovereign.json, or send a token from "
                f"`bin/issue-approval.sh openrouter_paid {explicit_paid.model}`.",
                extra={"requested_model": requested_model},
            )

        if not plan.candidates:
            self.log_message(
                "route-unavailable: %s",
                rt.redactor.redact(
                    json.dumps(
                        {
                            "requested_model": requested_model,
                            "strict_models": rt.strict_models,
                            "enabled_providers": rt.enabled_providers(),
                            "cooldowns_active": len(rt.cooldowns()),
                            "notes": plan.notes,
                        }
                    )
                ),
            )
            status = rt.providers_configured()
            any_key = any(v for k, v in status.items() if k != "ollama_local")
            raise HttpError(
                503,
                "no_providers_ready" if not any_key else "requested_model_unavailable",
                "; ".join(plan.notes)
                or "No candidate models available right now (all cooling down?). See /v1/status/cooldowns",
                extra={
                    "keys_needed": keys_needed(rt.env()),
                    "requested_model": requested_model,
                    "cooldowns_active": len(rt.cooldowns()),
                    "hint": "bin/doctor.sh explains what is missing; POST /v1/status/reset clears cooldowns.",
                },
            )

        wants_stream = bool(body.get("stream")) and not legacy
        attempts: List[Dict[str, Any]] = []
        token_spent = False
        request_started = time.monotonic()

        for index, cand in enumerate(plan.candidates):
            # Re-check cooldowns: a 401 or quota failure cools the whole
            # provider, and the plan was built before that happened.  Without
            # this, one bad key still costs a request per model in the pool.
            if rt.is_cooled(cand.cooldown_key) or rt.is_cooled(cand.provider_key):
                attempts.append({"candidate": cand.cooldown_key, "skipped": "cooled_during_request"})
                continue
            if cand.provider == "openrouter" and not cand.free and not paid_by_policy:
                if not token:
                    attempts.append({"candidate": cand.cooldown_key, "skipped": "paid_model_needs_approval"})
                    continue
                if not token_spent:
                    err, detail = rt.approvals.consume("openrouter_paid", token, cand.model)
                    if err:
                        raise HttpError(
                            403, err, detail or "Approval token rejected.", extra={"requested_model": requested_model}
                        )
                    token_spent = True

            payload = self.upstream_payload(body, cand, stream=wants_stream)
            endpoint = f"{cand.base_url}/completions" if legacy else f"{cand.base_url}/chat/completions"
            attempt_started = time.monotonic()
            outcome, failure = (
                self.attempt_stream(cand, endpoint, payload, attempts, plan)
                if wants_stream
                else self.attempt_json(cand, endpoint, payload, attempts, plan)
            )
            elapsed_ms = (time.monotonic() - attempt_started) * 1000.0

            if outcome == "served":
                rt.stats.record(cand.cooldown_key, status=200, kind="ok", ok=True, elapsed_ms=elapsed_ms)
                self.log_message(
                    "served by %s/%s after %d attempt(s), %.0fms total",
                    cand.provider,
                    cand.model,
                    index + 1,
                    (time.monotonic() - request_started) * 1000.0,
                )
                return
            if outcome == "client_error":
                rt.stats.record(
                    cand.cooldown_key,
                    status=failure.upstream_status if failure else 400,
                    kind=failure.kind if failure else "client_error",
                    ok=False,
                    elapsed_ms=elapsed_ms,
                )
                return
            rt.stats.record(
                cand.cooldown_key,
                status=failure.upstream_status if failure else 0,
                kind=failure.kind if failure else "unknown",
                ok=False,
                elapsed_ms=elapsed_ms,
            )

        raise HttpError(
            503,
            "all_candidates_failed",
            "Every candidate failed or was rate-limited. Wait for cooldowns to expire, or add another "
            "provider key (see KEYS.md).",
            extra={"attempts": attempts[:12], "requested_model": requested_model, "notes": plan.notes},
        )

    # --- one attempt ------------------------------------------------------
    def _apply_failure(self, cand: Candidate, failure: Failure, attempts: List[Dict[str, Any]]) -> None:
        rt = self.rt
        if failure.cooldown:
            rt.set_cooldown(cand.cooldown_key, failure.cooldown)
        if failure.provider_cooldown:
            rt.set_cooldown(cand.provider_key, failure.provider_cooldown)
        attempts.append(
            {
                "candidate": cand.cooldown_key,
                "status": failure.upstream_status,
                "kind": failure.kind,
                "error": (failure.message or "")[:300],
                "cooldown_applied": round(failure.cooldown or failure.provider_cooldown, 1),
            }
        )

    def _requested_id(self, plan: Plan) -> Optional[str]:
        if not plan.requested:
            return None
        return f"{plan.requested['provider']}/{plan.requested['model']}"

    def _served_elsewhere(self, cand: Candidate, plan: Plan) -> Optional[str]:
        """What the client asked for, when something else answered.

        Computed at serve time rather than plan time: a requested model that
        was available when we planned but then 429'd is still a fallback the
        client should hear about.
        """
        requested_id = self._requested_id(plan)
        if not requested_id:
            return None
        if plan.requested and plan.requested["provider"] == "vision-auto":
            return None  # any vision model satisfies dai/vision-auto
        if cand.provider == plan.requested["provider"] and cand.model == plan.requested["model"]:  # type: ignore[union-attr]
            return None
        return plan.fallback_from or requested_id

    def _annotation(self, cand: Candidate, plan: Plan, attempts: List[Dict[str, Any]], stream: bool) -> Dict[str, Any]:
        annotation: Dict[str, Any] = {
            "provider": cand.provider,
            "model": cand.model,
            "attempts": len(attempts) + 1,
            "stream": stream,
        }
        requested_id = self._requested_id(plan)
        if requested_id:
            annotation["requested"] = requested_id
        fallback = self._served_elsewhere(cand, plan)
        if fallback:
            annotation["fallback_from"] = fallback
        if plan.notes:
            annotation["notes"] = plan.notes
        return annotation

    def _headers(self, cand: Candidate, plan: Plan, attempts: List[Dict[str, Any]]) -> Dict[str, str]:
        headers = {
            "X-DAI-Provider": cand.provider,
            "X-DAI-Model": cand.model,
            "X-DAI-Attempts": str(len(attempts) + 1),
        }
        fallback = self._served_elsewhere(cand, plan)
        if fallback:
            headers["X-DAI-Fallback-From"] = fallback
        return headers

    def _failure(self, cand: Candidate, status: int, payload: Any) -> Failure:
        rt = self.rt
        return classify_failure(
            status,
            payload,
            cooldown_seconds=rt.cooldown_seconds,
            short_cooldown=rt.short_cooldown,
            provider_cooldown=rt.provider_cooldown,
        )

    def attempt_json(
        self, cand: Candidate, endpoint: str, payload: Dict[str, Any], attempts: List[Dict[str, Any]], plan: Plan
    ) -> Attempt:
        rt = self.rt
        status, data = request_json(
            "POST", endpoint, payload, cand.headers, timeout=rt.request_timeout, redactor=rt.redactor
        )
        if is_success(status):
            if isinstance(data, dict):
                data.setdefault("id", f"chatcmpl-{uuid.uuid4().hex[:12]}")
                data["dai_routed"] = self._annotation(cand, plan, attempts, stream=False)
            self.send_json(status, data, headers=self._headers(cand, plan, attempts))
            return "served", None

        failure = self._failure(cand, status, data)
        if not failure.retryable:
            # The payload is the problem — rotating would only burn rate limits.
            if isinstance(data, dict):
                data["dai_routed"] = self._annotation(cand, plan, attempts, stream=False)
            self.send_json(status, data, headers=self._headers(cand, plan, attempts))
            return "client_error", failure

        self._apply_failure(cand, failure, attempts)
        return "failed", failure

    def attempt_stream(
        self, cand: Candidate, endpoint: str, payload: Dict[str, Any], attempts: List[Dict[str, Any]], plan: Plan
    ) -> Attempt:
        """Stream one candidate.  Rotation is possible only before the first byte."""
        rt = self.rt
        status, err_or_headers, response = open_stream(
            "POST", endpoint, payload, cand.headers, timeout=rt.stream_timeout, redactor=rt.redactor
        )

        if response is None or not is_success(status):
            failure = self._failure(cand, status, None if response is not None else err_or_headers)
            if response is not None:
                response.close()
            if not failure.retryable:
                self.send_json(status, err_or_headers, headers=self._headers(cand, plan, attempts))
                return "client_error", failure
            self._apply_failure(cand, failure, attempts)
            return "failed", failure

        headers = err_or_headers if isinstance(err_or_headers, dict) else {}
        content_type = str(headers.get("Content-Type") or headers.get("content-type") or "").lower()
        if "text/event-stream" not in content_type:
            # Upstream ignored stream=true: degrade to one JSON response.
            try:
                raw = response.read()
            except Exception:
                raw = b""
            finally:
                response.close()
            try:
                data: Any = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            except json.JSONDecodeError:
                data = {
                    "error": {"message": rt.redactor.redact(raw[:2000].decode("utf-8", "replace")), "type": "non_json"}
                }
            if isinstance(data, dict):
                data.setdefault("id", f"chatcmpl-{uuid.uuid4().hex[:12]}")
                data["dai_routed"] = self._annotation(cand, plan, attempts, stream=False)
            self.send_json(status, data, headers=self._headers(cand, plan, attempts))
            return "served", None

        # Committed: the client owns this stream now, so no further rotation.
        self.start_stream(headers=self._headers(cand, plan, attempts))
        self.write_stream(f": dai_routed {json.dumps(self._annotation(cand, plan, attempts, stream=True))}\n\n")
        usage: Dict[str, Any] = {}
        interrupted = False
        try:
            for line in iter_sse_lines(response):
                if line.startswith(b"data:"):
                    chunk = line[5:].strip()
                    if chunk == b"[DONE]":
                        break  # re-emitted below, after our usage trailer
                    if b'"usage"' in chunk:
                        try:
                            parsed = json.loads(chunk)
                        except json.JSONDecodeError:
                            parsed = None
                        if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
                            usage = parsed["usage"]
                if not self.write_stream(line):
                    interrupted = True
                    break
        except Exception as exc:
            interrupted = True
            self.write_stream(
                "event: error\ndata: "
                + json.dumps({"error": "upstream_stream_interrupted", "detail": rt.redactor.redact(str(exc))})
                + "\n\n"
            )
        finally:
            with contextlib.suppress(Exception):
                response.close()
        # Trailer: usage first (clients stop reading at [DONE]), then terminate.
        if usage and not interrupted:
            self.write_stream(f": dai_usage {json.dumps(usage)}\n\n")
        if not interrupted:
            self.write_stream("data: [DONE]\n\n")
        self.end_stream()
        if interrupted:
            rt.stats.record(cand.cooldown_key, status=200, kind="stream_interrupted", ok=False)
        return "served", None


def check_configuration(runtime: RouterRuntime) -> Dict[str, Any]:
    """Validate files and keys without serving.  Used by ``--check`` and doctor."""
    problems: List[str] = []
    pool = runtime.pool()
    catalog = runtime.catalog()
    policy = runtime.policy()

    if not pool:
        problems.append(f"rotation pool empty or unreadable: {runtime.pool_file.path}")
    for key in ("openrouter_free", "ollama_local_models"):
        if key not in pool:
            problems.append(f"rotation pool is missing '{key}'")
        elif not pool.get(key) and key == "openrouter_free":
            problems.append("rotation pool 'openrouter_free' is empty: no cloud rotation without it")
    for key, value in pool.items():
        if isinstance(value, list):
            for entry in value:
                if not isinstance(entry, str) or not entry.strip():
                    problems.append(f"rotation pool '{key}' has a non-string entry: {entry!r}")
    if not isinstance(catalog.get("models"), list):
        problems.append(f"catalog has no 'models' list: {runtime.catalog_file.path}")
    if not policy:
        problems.append(f"policy empty or unreadable: {runtime.policy_file.path}")
    for name, spec in PROVIDERS.items():
        if spec.requires_key and spec.configured(runtime.env()):
            key = spec.api_key(runtime.env())
            if len(key) < 8:
                problems.append(f"{name} api key looks truncated ({len(key)} chars)")
    problems.extend(runtime.config_errors())

    return {
        "ok": not problems,
        "problems": problems,
        # Present for a uniform contract with the worker: `problems` fail
        # `--check`, `warnings` do not.  Missing keys are information, not a
        # warning — `keys_needed` already reports them.
        "warnings": [],
        "providers_configured": runtime.providers_configured(),
        "keys_needed": keys_needed(runtime.env()),
        "candidates_now": len(runtime.plan().candidates),
        "pool_models": {k: len(v) for k, v in pool.items() if isinstance(v, list)},
        "catalog_models": len(catalog.get("models") or []),
        "cooldowns_active": len(runtime.cooldowns()),
        "files": {
            "env": str(runtime.env_path),
            "env_exists": runtime.env_path.exists(),
            "pool": str(runtime.pool_file.path),
            "catalog": str(runtime.catalog_file.path),
            "policy": str(runtime.policy_file.path),
            "approvals": str(runtime.approvals.path),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="model-router", description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    parser.add_argument("--print-config", action="store_true", help="print resolved (secret-free) config and exit")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{SERVICE.name} {VERSION}")
        return 0

    load_dotenv(Path(os.environ.get("DAI_ENV", str(ROOT / ".env"))))
    runtime = build_runtime()
    config = build_config(runtime)

    if args.check:
        report = check_configuration(runtime)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if args.print_config:
        print(json.dumps({"config": config.public_dict(), **runtime.health(probe_local=False)}, indent=2, default=str))
        return 0

    httpd = create_server(
        config,
        RouterHandler,
        service=SERVICE,
        redactor=runtime.redactor,
        bind={"runtime": runtime, "timeout": config.extra.get("client_timeout", 180)},
    )
    banner = "\n".join(
        [
            f"model-router {VERSION} listening on http://{config.host}:{config.port}",
            f"env={runtime.env_path} exists={runtime.env_path.exists()}",
            f"providers={runtime.providers_configured()}",
            f"candidates_now={len(runtime.plan().candidates)} auth_required={bool(config.auth_token)}",
        ]
    )
    report = check_configuration(runtime)
    for problem in report["problems"]:
        banner += f"\nPROBLEM: {problem}"
    for warning in report["warnings"]:
        banner += f"\nWARNING: {warning}"
    serve_forever(httpd, on_shutdown=runtime.stats.flush, banner=banner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
