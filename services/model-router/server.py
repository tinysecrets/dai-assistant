#!/usr/bin/env python3
"""Debian AI model-router — cloud-first free top-model rotation.

OpenAI-compatible API on :11435.
Rotates across OpenRouter :free, Groq, Ollama Cloud, then local Ollama.
Keys live only in .env (never logged). Ready with zero keys: health reports
what's missing; chat returns 503 until at least one provider is configured.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = Path(os.environ.get("DAI_ENV", ROOT / ".env"))
POLICY_PATH = Path(os.environ.get("DAI_POLICY", ROOT / "policy" / "sovereign.json"))
APPROVALS_PATH = Path(os.environ.get("DAI_APPROVALS", ROOT / "policy" / "approvals.json"))
POOL_PATH = Path(os.environ.get("DAI_POOL", ROOT / "config" / "rotation-pool.json"))
CATALOG_PATH = Path(os.environ.get("DAI_CATALOG", ROOT / "config" / "free-models.json"))
STATE_PATH = Path(os.environ.get("DAI_ROUTER_STATE", ROOT / "state" / "router-cooldowns.json"))
HOST = os.environ.get("DAI_ROUTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("DAI_ROUTER_PORT", "11435"))

_lock = threading.Lock()
_rr = 0


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


load_dotenv(ENV_PATH)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default if default is not None else {}
    with path.open() as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def redact(text: str) -> str:
    return re.sub(
        r"(?i)(api[_-]?key|authorization|bearer|sk-[a-z0-9_-]+|gsk_[a-z0-9]+)\s*[:=]?\s*\S+",
        r"\1=[REDACTED]",
        text,
    )


def http_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, v)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw) if raw else {"error": str(e)}
        except json.JSONDecodeError:
            parsed = {"error": redact(raw or str(e))}
        return e.code, parsed
    except Exception as e:  # noqa: BLE001
        return 502, {"error": redact(str(e))}


def cooldowns() -> dict[str, float]:
    data = load_json(STATE_PATH, {"cooldowns": {}})
    now = time.time()
    cleaned = {k: v for k, v in data.get("cooldowns", {}).items() if v > now}
    return cleaned


def set_cooldown(key: str, seconds: float = 90.0) -> None:
    with _lock:
        data = load_json(STATE_PATH, {"cooldowns": {}})
        cds = data.setdefault("cooldowns", {})
        cds[key] = time.time() + seconds
        # drop expired
        now = time.time()
        data["cooldowns"] = {k: v for k, v in cds.items() if v > now}
        save_json(STATE_PATH, data)


def provider_status() -> dict[str, Any]:
    return {
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
        "groq": bool(os.environ.get("GROQ_API_KEY")),
        "ollama_cloud": bool(os.environ.get("OLLAMA_API_KEY")),
        "ollama_local": True,  # keyless; may still be down
        "cerebras": bool(os.environ.get("CEREBRAS_API_KEY")),
    }


def build_candidates(task_hint: str = "", preferred: str | None = None) -> list[dict[str, Any]]:
    """Ordered list of {provider, model, base_url, headers, cooldown_key}."""
    global _rr
    pool = load_json(POOL_PATH, {})
    status = provider_status()
    cds = cooldowns()
    out: list[dict[str, Any]] = []

    def add(provider: str, model: str, base: str, headers: dict[str, str]) -> None:
        ck = f"{provider}:{model}"
        if cds.get(ck, 0) > time.time():
            return
        out.append(
            {
                "provider": provider,
                "model": model,
                "base_url": base.rstrip("/"),
                "headers": headers,
                "cooldown_key": ck,
            }
        )

    # Preferred explicit model id
    if preferred and preferred not in ("auto", "dai/auto", "router/auto"):
        if preferred == "dai/vision-auto":
            # Vision-only rotation: GUI grounding needs an image-capable model.
            # Do NOT mix text-only models here (they reject image input).
            if status["openrouter"]:
                headers = {
                    "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                    "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "https://localhost/debian-ai"),
                    "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "Debian AI Assistant"),
                }
                for mid in pool.get("openrouter_free_vision") or []:
                    add("openrouter", mid, "https://openrouter.ai/api/v1", headers)
            if len(out) > 1:
                with _lock:
                    start = _rr % len(out)
                    _rr += 1
                out = out[start:] + out[:start]
            return out
        if preferred.startswith("groq/") and status["groq"]:
            add(
                "groq",
                preferred.removeprefix("groq/"),
                "https://api.groq.com/openai/v1",
                {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            )
        elif preferred.startswith("ollama-cloud/") and status["ollama_cloud"]:
            add(
                "ollama_cloud",
                preferred.removeprefix("ollama-cloud/"),
                "https://ollama.com/v1",
                {"Authorization": f"Bearer {os.environ['OLLAMA_API_KEY']}"},
            )
        elif preferred.startswith("ollama/") or ":" in preferred and not preferred.endswith(":free"):
            # local-style tags like hermes3:8b
            local_model = preferred.removeprefix("ollama/")
            add("ollama_local", local_model, "http://127.0.0.1:11434/v1", {})
        elif status["openrouter"]:
            mid = preferred.removeprefix("openrouter/")
            add(
                "openrouter",
                mid,
                "https://openrouter.ai/api/v1",
                {
                    "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                    "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "https://localhost/debian-ai"),
                    "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "Debian AI Assistant"),
                },
            )

    # OpenRouter free rotation pool (primary)
    if status["openrouter"]:
        models = list(pool.get("openrouter_free") or [])
        # optional: merge catalog tool-capable top if present
        catalog = load_json(CATALOG_PATH, {}).get("models") or []
        if catalog and task_hint:
            # light boost: put vision models first if hint asks
            if any(w in task_hint.lower() for w in ("image", "screenshot", "photo", "vision")):
                vision = [
                    m["id"]
                    for m in catalog
                    if "image" in ((m.get("architecture") or {}).get("input_modalities") or [])
                    and str((m.get("pricing") or {}).get("prompt", "1")) == "0"
                ]
                models = vision + [m for m in models if m not in vision]
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "https://localhost/debian-ai"),
            "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "Debian AI Assistant"),
        }
        for mid in models:
            add("openrouter", mid, "https://openrouter.ai/api/v1", headers)

    # Groq bucket (separate rate limits)
    if status["groq"]:
        headers = {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}
        for mid in pool.get("groq_models") or []:
            add("groq", mid, "https://api.groq.com/openai/v1", headers)

    # Cerebras (sidekick-style cheap/fast)
    if status["cerebras"]:
        headers = {"Authorization": f"Bearer {os.environ['CEREBRAS_API_KEY']}"}
        model = os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")
        add("cerebras", model, "https://api.cerebras.ai/v1", headers)

    # Ollama Cloud
    if status["ollama_cloud"]:
        headers = {"Authorization": f"Bearer {os.environ['OLLAMA_API_KEY']}"}
        for mid in pool.get("ollama_cloud_models") or []:
            add("ollama_cloud", mid, "https://ollama.com/v1", headers)

    # Local last
    for mid in pool.get("ollama_local_models") or ["llama3.2:3b"]:
        add("ollama_local", mid, "http://127.0.0.1:11434/v1", {})

    # Round-robin offset so we don't always hammer the first free model
    if len(out) > 1:
        with _lock:
            start = _rr % len(out)
            _rr += 1
        out = out[start:] + out[:start]
    return out


def chat_via_candidate(cand: dict[str, Any], body: dict[str, Any]) -> tuple[int, Any]:
    payload = dict(body)
    payload["model"] = cand["model"]
    payload["stream"] = False
    url = f"{cand['base_url']}/chat/completions"
    return http_json("POST", url, payload, cand["headers"])


class Handler(BaseHTTPRequestHandler):
    server_version = "dai-model-router/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(redact("%s - %s" % (self.address_string(), fmt % args)))

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode() or "{}")

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        policy = load_json(POLICY_PATH, {})
        status = provider_status()
        configured = [k for k, v in status.items() if v and k != "ollama_local"]
        if self.path in ("/", "/health"):
            self._send(
                200,
                {
                    "ok": True,
                    "service": "model-router",
                    "mode": policy.get("mode", "cloud_first_free_rotation"),
                    "ready_for_chat": bool(configured) or self._local_ollama_up(),
                    "providers_configured": status,
                    "keys_needed": [k for k, v in status.items() if not v and k != "ollama_local"],
                    "openrouter_paid_enabled": (policy.get("inference") or {})
                    .get("openrouter", {})
                    .get("paid_enabled", False),
                    "pool": str(POOL_PATH),
                },
            )
            return
        if self.path.startswith("/v1/models"):
            models = []
            for c in build_candidates():
                models.append(
                    {
                        "id": f"{c['provider']}/{c['model']}"
                        if c["provider"] != "openrouter"
                        else c["model"],
                        "object": "model",
                        "owned_by": c["provider"],
                    }
                )
            # virtual auto model
            models.insert(0, {
                "id": "dai/auto", "object": "model", "owned_by": "debian-ai",
                "description": "Rotation across cloud free + local pools (text focused)",
            })
            models.insert(0, {
                "id": "dai/vision-auto", "object": "model", "owned_by": "debian-ai",
                "description": "Rotation across vision-capable free models (GUI grounding)",
            })
            self._send(200, {"object": "list", "data": models})
            return
        if self.path == "/v1/status/keys":
            self._send(200, {"providers": status, "env_file": str(ENV_PATH), "env_exists": ENV_PATH.exists()})
            return
        self._send(404, {"error": "not found"})

    def _local_ollama_up(self) -> bool:
        code, _ = http_json("GET", "http://127.0.0.1:11434/api/tags", timeout=2)
        return code == 200

    def do_POST(self) -> None:  # noqa: N802
        policy = load_json(POLICY_PATH, {})
        body = self._read_json()

        if self.path not in ("/v1/chat/completions", "/v1/completions"):
            self._send(404, {"error": "not found"})
            return

        # Paid openrouter models require approval
        model = body.get("model") or "dai/auto"
        if isinstance(model, str) and model.startswith("openrouter/") and not model.endswith(":free"):
            raw = model.removeprefix("openrouter/")
            if not raw.endswith(":free"):
                or_cfg = (policy.get("inference") or {}).get("openrouter") or {}
                if not or_cfg.get("paid_enabled"):
                    self._send(
                        403,
                        {
                            "error": "paid_models_disabled",
                            "detail": "Paid OpenRouter models need owner approval + policy flag.",
                        },
                    )
                    return

        # Extract task hint from last user message for light routing
        task_hint = ""
        for msg in reversed(body.get("messages") or []):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    task_hint = content[:500]
                break

        preferred = None if model in ("dai/auto", "auto", "router/auto") else str(model)
        candidates = build_candidates(task_hint=task_hint, preferred=preferred)

        if not candidates:
            self._send(
                503,
                {
                    "error": "no_providers_ready",
                    "detail": "Add at least one key to .env (OPENROUTER_API_KEY recommended). See KEYS.md",
                    "keys_needed": [k for k, v in provider_status().items() if not v and k != "ollama_local"],
                },
            )
            return

        errors: list[dict[str, Any]] = []
        for cand in candidates:
            # Skip paid openrouter unless allowlisted free or paid enabled
            if cand["provider"] == "openrouter" and not str(cand["model"]).endswith(":free"):
                or_cfg = (policy.get("inference") or {}).get("openrouter") or {}
                if not or_cfg.get("paid_enabled"):
                    continue

            code, data = chat_via_candidate(cand, body)
            if code == 429 or (
                isinstance(data, dict)
                and "rate" in json.dumps(data).lower()
                and code >= 400
            ):
                set_cooldown(cand["cooldown_key"], float(os.environ.get("DAI_COOLDOWN_SECONDS", "90")))
                errors.append({"candidate": cand["cooldown_key"], "status": code, "error": "rate_limited"})
                continue
            if code >= 400:
                # cool brief on hard failures for that model
                if code in (401, 403, 404, 500, 502, 503):
                    set_cooldown(cand["cooldown_key"], 30.0)
                errors.append(
                    {
                        "candidate": cand["cooldown_key"],
                        "status": code,
                        "error": redact(json.dumps(data)[:300]),
                    }
                )
                continue

            # Success — annotate which backend served
            if isinstance(data, dict):
                data.setdefault("id", f"chatcmpl-{uuid.uuid4().hex[:12]}")
                data["dai_routed"] = {
                    "provider": cand["provider"],
                    "model": cand["model"],
                }
            self._send(code, data)
            return

        self._send(
            503,
            {
                "error": "all_candidates_failed",
                "detail": "Every free/top candidate failed or was rate-limited. Wait or add another provider key.",
                "attempts": errors[:12],
            },
        )


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"model-router listening on http://{HOST}:{PORT}")
    print(f"env={ENV_PATH} exists={ENV_PATH.exists()}")
    print(f"providers={provider_status()}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
