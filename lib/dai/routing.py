"""Provider rotation planning and upstream failure classification.

Pure logic, no I/O: the router service feeds it the rotation pool, the model
catalog, which provider keys exist, and the active cooldowns, and gets back an
ordered candidate list plus a verdict for each upstream failure.

Two behaviours here are deliberate fixes:

* **Failure classification reads only the upstream error fields.**  The old
  heuristic treated any 4xx/5xx whose body merely contained the substring
  ``"rate"`` (``"Failed to operate"``, ``"generate"``) as rate limiting and
  applied a 90 s cooldown.  Phrases are now matched explicitly.
* **A bad request does not rotate.**  If the client's payload is invalid,
  trying 20 more models wastes their rate limits and hides the real error; the
  upstream response goes straight back, annotated with what served it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# --- failure kinds ---------------------------------------------------------
KIND_OK = "ok"
KIND_RATE_LIMITED = "rate_limited"
KIND_QUOTA = "quota_exhausted"
KIND_AUTH = "auth_failed"
KIND_MODEL_UNAVAILABLE = "model_unavailable"
KIND_CONTEXT_LENGTH = "context_length_exceeded"
KIND_UNSUPPORTED_INPUT = "unsupported_input"
KIND_CLIENT_ERROR = "client_error"
KIND_SERVER_ERROR = "server_error"
KIND_TIMEOUT = "timeout"
KIND_NETWORK = "network"

RATE_LIMIT_PHRASES = ("rate limit", "rate_limit", "ratelimit", "too many requests", "try again later")
QUOTA_PHRASES = ("quota", "credit", "insufficient", "billing", "exceeded your current", "payment required", "allowance")
AUTH_PHRASES = (
    "invalid api key",
    "invalid_api_key",
    "unauthorized",
    "unauthenticated",
    "invalid authentication",
    "api key not",
)
UNAVAILABLE_PHRASES = (
    "no providers",
    "not available",
    "model not found",
    "does not exist",
    "no such model",
    "overloaded",
    "capacity",
    "temporarily",
    "service unavailable",
    "maintenance",
)
CONTEXT_PHRASES = ("context length", "context_length", "maximum context", "too many tokens", "reduce the length")
UNSUPPORTED_PHRASES = (
    "does not support images",
    "image input",
    "vision",
    "does not support tools",
    "tool use is not",
    "unsupported parameter",
    "does not support 'tools'",
    "streaming not supported",
)

_VIRTUAL_TEXT = ("auto", "dai/auto", "router/auto")
_VIRTUAL_VISION = ("dai/vision-auto", "vision-auto", "dai/vision")

DEFAULT_PROVIDER_COOLDOWN = 600.0


@dataclass(frozen=True)
class ProviderSpec:
    """One inference backend the router can rotate across."""

    name: str
    key_env: str
    default_base_url: str
    base_url_env: str
    pool_key: Optional[str]
    default_models: Tuple[str, ...] = ()
    requires_key: bool = True
    extra_header_env: Tuple[Tuple[str, str, str], ...] = ()  # (header, env_var, default)

    def base_url(self, env: Mapping[str, str]) -> str:
        override = (env.get(self.base_url_env) or "").strip()
        return (override or self.default_base_url).rstrip("/")

    def api_key(self, env: Mapping[str, str]) -> str:
        return (env.get(self.key_env) or "").strip() if self.key_env else ""

    def configured(self, env: Mapping[str, str]) -> bool:
        return bool(self.api_key(env)) if self.requires_key else True

    def headers(self, env: Mapping[str, str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if self.requires_key:
            out["Authorization"] = f"Bearer {self.api_key(env)}"
        for header, var, default in self.extra_header_env:
            value = (env.get(var) or default).strip()
            if value:
                out[header] = value
        return out


PROVIDERS: Dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        name="openrouter",
        key_env="OPENROUTER_API_KEY",
        default_base_url="https://openrouter.ai/api/v1",
        base_url_env="DAI_OPENROUTER_BASE_URL",
        pool_key="openrouter_free",
        extra_header_env=(
            ("HTTP-Referer", "OPENROUTER_HTTP_REFERER", "https://localhost/debian-ai"),
            ("X-Title", "OPENROUTER_APP_TITLE", "Debian AI Assistant"),
        ),
    ),
    "groq": ProviderSpec(
        name="groq",
        key_env="GROQ_API_KEY",
        default_base_url="https://api.groq.com/openai/v1",
        base_url_env="DAI_GROQ_BASE_URL",
        pool_key="groq_models",
        default_models=("llama-3.3-70b-versatile",),
    ),
    "cerebras": ProviderSpec(
        name="cerebras",
        key_env="CEREBRAS_API_KEY",
        default_base_url="https://api.cerebras.ai/v1",
        base_url_env="DAI_CEREBRAS_BASE_URL",
        pool_key=None,
        default_models=("llama-3.3-70b",),
        extra_header_env=(),
    ),
    "ollama_cloud": ProviderSpec(
        name="ollama_cloud",
        key_env="OLLAMA_API_KEY",
        default_base_url="https://ollama.com/v1",
        base_url_env="DAI_OLLAMA_CLOUD_BASE_URL",
        pool_key="ollama_cloud_models",
    ),
    "ollama_local": ProviderSpec(
        name="ollama_local",
        key_env="",
        default_base_url="http://127.0.0.1:11434/v1",
        base_url_env="DAI_OLLAMA_LOCAL_BASE_URL",
        pool_key="ollama_local_models",
        default_models=("llama3.2:3b",),
        requires_key=False,
    ),
}

# Order used when rotating: cloud free pools first, local last.
PROVIDER_ORDER: Tuple[str, ...] = ("openrouter", "groq", "cerebras", "ollama_cloud", "ollama_local")

# ``provider/`` prefixes a caller may use to pin a model to a backend.
PREFIX_TO_PROVIDER: Dict[str, str] = {
    "groq": "groq",
    "cerebras": "cerebras",
    "ollama-cloud": "ollama_cloud",
    "ollama_cloud": "ollama_cloud",
    "ollama": "ollama_local",
    "local": "ollama_local",
    "openrouter": "openrouter",
}


@dataclass
class Candidate:
    provider: str
    model: str
    base_url: str
    headers: Dict[str, str] = field(default_factory=dict)
    origin: str = "pool"  # pool | explicit | vision
    cooldown_key: str = ""
    free: bool = True

    def __post_init__(self) -> None:
        if not self.cooldown_key:
            self.cooldown_key = f"{self.provider}:{self.model}"

    @property
    def provider_key(self) -> str:
        return f"{self.provider}:*"

    @property
    def public_id(self) -> str:
        """Model id as advertised in ``/v1/models`` (kept stable for clients)."""
        return self.model if self.provider == "openrouter" else f"{self.provider}/{self.model}"

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def completions_endpoint(self) -> str:
        return f"{self.base_url}/completions"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "public_id": self.public_id,
            "base_url": self.base_url,
            "origin": self.origin,
            "cooldown_key": self.cooldown_key,
        }


@dataclass
class Failure:
    """Verdict on an upstream response."""

    kind: str
    retryable: bool
    cooldown: float = 0.0
    provider_cooldown: float = 0.0
    message: str = ""
    upstream_status: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "retryable": self.retryable,
            "cooldown": self.cooldown,
            "provider_cooldown": self.provider_cooldown,
            "message": self.message,
            "upstream_status": self.upstream_status,
        }


@dataclass
class Plan:
    """Result of planning one request."""

    candidates: List[Candidate]
    requested: Optional[Dict[str, str]] = None  # what the client asked for
    fallback_from: Optional[str] = None  # set when we could not honour it
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requested": self.requested,
            "fallback_from": self.fallback_from,
            "notes": self.notes,
            "candidates": [c.as_dict() for c in self.candidates],
        }


def is_success(status: int) -> bool:
    return 200 <= status < 300


def _contains_any(text: str, phrases: Sequence[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def extract_error(payload: Any) -> str:
    """Pull the recognised error fields out of an upstream payload.

    Deliberately narrow: scanning the whole serialised body is what produced
    the false ``rate_limited`` verdicts.  Returns the text with its original
    casing so it is fit to show an operator.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.lower()
    if not isinstance(payload, dict):
        return ""
    parts: List[str] = []

    def collect(value: Any, depth: int = 0) -> None:
        if depth > 2 or value is None:
            return
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for key in ("message", "code", "type", "error", "reason", "detail"):
                if key in value:
                    collect(value[key], depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value[:3]:
                collect(item, depth + 1)

    for key in ("error", "message", "code", "detail", "reason"):
        if key in payload:
            collect(payload[key])
    return " ".join(parts)


def error_text(payload: Any) -> str:
    """Lowercased error text, for phrase matching only (never for display)."""
    return extract_error(payload).lower()


def classify_failure(
    status: int,
    payload: Any = None,
    *,
    cooldown_seconds: float = 90.0,
    short_cooldown: float = 30.0,
    provider_cooldown: float = DEFAULT_PROVIDER_COOLDOWN,
) -> Failure:
    """Map an upstream status/body onto a :class:`Failure` verdict."""
    text = error_text(payload)
    message = extract_error(payload)[:300]

    if is_success(status):
        return Failure(KIND_OK, retryable=False, message="", upstream_status=status)

    if status == 429:
        return Failure(KIND_RATE_LIMITED, True, cooldown_seconds, 0.0, message, status)

    if status == 401:
        # Every model on this provider will fail the same way: cool the provider.
        return Failure(KIND_AUTH, True, 0.0, provider_cooldown, message or "invalid or missing api key", status)

    if status in (402, 403):
        if _contains_any(text, RATE_LIMIT_PHRASES):
            return Failure(KIND_RATE_LIMITED, True, cooldown_seconds, 0.0, message, status)
        if _contains_any(text, QUOTA_PHRASES) or status == 402:
            return Failure(KIND_QUOTA, True, 0.0, provider_cooldown, message or "quota/credits exhausted", status)
        if _contains_any(text, AUTH_PHRASES):
            return Failure(KIND_AUTH, True, 0.0, provider_cooldown, message, status)
        return Failure(KIND_AUTH, True, short_cooldown, short_cooldown, message or "forbidden", status)

    if status == 404:
        return Failure(KIND_MODEL_UNAVAILABLE, True, short_cooldown, 0.0, message or "model not found", status)

    if status in (400, 422):
        if _contains_any(text, CONTEXT_PHRASES):
            return Failure(KIND_CONTEXT_LENGTH, True, short_cooldown, 0.0, message, status)
        if _contains_any(text, UNSUPPORTED_PHRASES):
            return Failure(KIND_UNSUPPORTED_INPUT, True, short_cooldown, 0.0, message, status)
        if _contains_any(text, UNAVAILABLE_PHRASES):
            return Failure(KIND_MODEL_UNAVAILABLE, True, short_cooldown, 0.0, message, status)
        if _contains_any(text, RATE_LIMIT_PHRASES):
            return Failure(KIND_RATE_LIMITED, True, cooldown_seconds, 0.0, message, status)
        # The client's payload is the problem: tell them, do not burn the pool.
        return Failure(KIND_CLIENT_ERROR, False, 0.0, 0.0, message or "bad request", status)

    if status in (408, 504) or _contains_any(text, ("timeout", "timed out")):
        return Failure(KIND_TIMEOUT, True, short_cooldown, 0.0, message or "upstream timeout", status)

    if status == 502:
        return Failure(KIND_NETWORK, True, short_cooldown, 0.0, message or "network error", status)

    if 500 <= status < 600:
        return Failure(KIND_SERVER_ERROR, True, short_cooldown, 0.0, message or "upstream server error", status)

    return Failure(KIND_CLIENT_ERROR, False, 0.0, 0.0, message or f"unexpected status {status}", status)


def is_vision_request(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(re.search(r"\b(image|screenshot|screen shot|photo|picture|vision|visual|ui element)\b", lowered))


def resolve_explicit(preferred: Optional[str]) -> Optional[Dict[str, str]]:
    """Resolve a requested model id to ``{"provider": ..., "model": ...}``.

    ``None`` means "auto" (text rotation).  ``dai/vision-auto`` resolves to a
    marker the planner expands into the vision pool.
    """
    if not preferred:
        return None
    wanted = preferred.strip()
    if wanted in _VIRTUAL_TEXT:
        return None
    if wanted in _VIRTUAL_VISION:
        return {"provider": "vision-auto", "model": wanted}

    head, slash, tail = wanted.partition("/")
    if slash and head.lower() in PREFIX_TO_PROVIDER:
        return {"provider": PREFIX_TO_PROVIDER[head.lower()], "model": tail or wanted}
    if head.lower() in PROVIDERS:
        return {"provider": head.lower(), "model": tail or wanted}
    # Bare ids: an Ollama-style tag (``hermes3:8b``) is local, anything else is
    # treated as an OpenRouter slug.  ``:free`` always means OpenRouter.
    if ":" in wanted and not wanted.endswith(":free"):
        return {"provider": "ollama_local", "model": wanted}
    return {"provider": "openrouter", "model": wanted}


def _vision_models(pool: Mapping[str, Any], catalog: Mapping[str, Any]) -> List[str]:
    """Vision-capable ids: the explicit pool first, then free catalog entries."""
    out: List[str] = [str(m) for m in (pool.get("openrouter_free_vision") or []) if m]
    seen = set(out)
    for entry in catalog.get("models") or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "")
        if not mid or mid in seen:
            continue
        arch = entry.get("architecture") or {}
        modalities = arch.get("input_modalities") or []
        pricing = entry.get("pricing") or {}
        # Only genuinely free ids may enter the rotation: the paid guard in the
        # service would otherwise skip them after they had taken a slot.
        if "image" in modalities and str(pricing.get("prompt", "1")) == "0" and mid.endswith(":free"):
            out.append(mid)
            seen.add(mid)
    return out


def _text_models(pool: Mapping[str, Any], catalog: Mapping[str, Any], task_hint: str) -> Tuple[List[str], bool]:
    """OpenRouter text rotation, with vision-capable models first when hinted.

    Returns ``(models, boosted)``.  When boosted, the caller rotates *within*
    each group rather than across the whole list, so the intent survives
    round-robin instead of being shuffled straight back out.
    """
    models: List[str] = [str(m) for m in (pool.get("openrouter_free") or []) if m]
    if models and is_vision_request(task_hint):
        vision = set(_vision_models(pool, catalog))
        primary = [m for m in models if m in vision]
        if primary:
            rest = [m for m in models if m not in vision]
            return primary + rest, True
    return models, False


def _rotate(models: List[str], rr_index: int) -> List[str]:
    if len(models) <= 1:
        return models
    start = rr_index % len(models)
    return models[start:] + models[:start]


def plan_candidates(
    *,
    pool: Mapping[str, Any],
    catalog: Mapping[str, Any],
    env: Mapping[str, str],
    cooldowns: Mapping[str, float],
    now: float,
    rr_index: int = 0,
    task_hint: str = "",
    preferred: Optional[str] = None,
    strict_models: bool = False,
    enabled_providers: Optional[Sequence[str]] = None,
) -> Plan:
    """Build the ordered candidate list for one request.

    ``cooldowns`` maps a candidate key (``provider:model``) or a provider key
    (``provider:*``) to the unix time at which it becomes usable again.
    ``enabled_providers`` lets ``policy/sovereign.json`` switch a backend off
    entirely; ``None`` means "everything the policy allows".
    """
    notes: List[str] = []
    resolved = resolve_explicit(preferred)
    allowed = set(enabled_providers) if enabled_providers is not None else set(PROVIDER_ORDER)
    groups: List[List[Candidate]] = []  # rotated independently, order preserved

    def cooled(key: str) -> bool:
        return cooldowns.get(key, 0.0) > now

    def make(provider_name: str, model: str, *, origin: str, free: Optional[bool] = None) -> Optional[Candidate]:
        spec = PROVIDERS[provider_name]
        if provider_name not in allowed:
            return None
        if spec.requires_key and not spec.configured(env):
            return None
        if cooled(f"{provider_name}:{model}") or cooled(f"{provider_name}:*"):
            return None
        if free is None:
            free = model.endswith(":free") or provider_name != "openrouter"
        return Candidate(
            provider=provider_name,
            model=model,
            base_url=spec.base_url(env),
            headers=spec.headers(env),
            origin=origin,
            free=free,
        )

    out: List[Candidate] = []
    seen: set = set()

    def push(cand: Optional[Candidate], group: List[Candidate]) -> None:
        if cand is None:
            return
        key = (cand.provider, cand.model)
        if key in seen:
            return
        seen.add(key)
        group.append(cand)
        out.append(cand)

    def new_group() -> List[Candidate]:
        group: List[Candidate] = []
        groups.append(group)
        return group

    # 1. An explicitly requested model (or the vision rotation).
    requested: Optional[Dict[str, str]] = None
    if resolved and resolved["provider"] == "vision-auto":
        requested = {"provider": "vision-auto", "model": resolved["model"]}
        group = new_group()
        for mid in _vision_models(pool, catalog):
            push(make("openrouter", mid, origin="vision"), group)
        if not out:
            notes.append("no vision-capable candidates available (needs OPENROUTER_API_KEY)")
            return Plan(out, requested=requested, notes=notes)
        return Plan(_rotate(group, rr_index), requested=requested, notes=notes)

    pinned: List[Candidate] = []
    if resolved:
        requested = dict(resolved)
        cand = make(resolved["provider"], resolved["model"], origin="explicit")
        if cand is not None:
            # Pinned, and in its own group so round-robin cannot move it.
            seen.add((cand.provider, cand.model))
            pinned.append(cand)
            out.append(cand)
        else:
            reason = (
                "cooling down"
                if cooled(f"{resolved['provider']}:{resolved['model']}") or cooled(f"{resolved['provider']}:*")
                else "provider not configured"
            )
            notes.append(f"requested model unavailable ({reason}): {preferred}")
            if strict_models:
                return Plan([], requested=requested, notes=notes)

    # 2. The rotating pools.  Each provider is its own group, and a vision
    #    boost splits OpenRouter into "vision first" + "everything else" so the
    #    boost survives round-robin.
    for provider_name in PROVIDER_ORDER:
        spec = PROVIDERS[provider_name]
        if provider_name not in allowed or not spec.configured(env):
            continue
        if provider_name == "openrouter":
            models, boosted = _text_models(pool, catalog, task_hint)
            if boosted:
                vision = set(_vision_models(pool, catalog))
                primary = [m for m in models if m in vision]
                rest = [m for m in models if m not in vision]
                for chunk in (primary, rest):
                    if not chunk:
                        continue
                    group = new_group()
                    for mid in chunk:
                        push(make(provider_name, mid, origin="pool"), group)
                continue
        elif provider_name == "cerebras":
            models = [(env.get("CEREBRAS_MODEL") or "").strip() or spec.default_models[0]]
        else:
            # An explicit empty list means "nothing from this provider"; a
            # missing key falls back to the provider's built-in default.
            raw = pool.get(spec.pool_key) if spec.pool_key else None
            models = [str(m) for m in raw if m] if isinstance(raw, list) else list(spec.default_models)
        group = new_group()
        for mid in models:
            push(make(provider_name, mid, origin="pool"), group)

    if not out:
        notes.append("no providers ready — add a key to .env (see KEYS.md) or start local Ollama")
        return Plan(out, requested=requested, notes=notes)

    # 3. Round-robin inside each group, preserving group order, so repeated
    #    calls do not hammer one model while provider preference still holds.
    ordered: List[Candidate] = list(pinned)
    for group in groups:
        ordered.extend(_rotate(group, rr_index))

    fallback_from = None
    if requested and (
        not ordered or ordered[0].provider != requested["provider"] or ordered[0].model != requested["model"]
    ):
        fallback_from = f"{requested['provider']}/{requested['model']}"

    return Plan(ordered, requested=requested, fallback_from=fallback_from, notes=notes)


def rotate_index(current: int) -> int:
    """Advance a round-robin counter, wrapping to stay small."""
    return (current + 1) % 1_000_000


def provider_status(env: Mapping[str, str]) -> Dict[str, bool]:
    """Which providers are usable right now (keys present; local is keyless)."""
    return {name: PROVIDERS[name].configured(env) for name in PROVIDER_ORDER}


def keys_needed(env: Mapping[str, str]) -> List[str]:
    return [
        PROVIDERS[name].key_env
        for name in PROVIDER_ORDER
        if PROVIDERS[name].requires_key and not PROVIDERS[name].configured(env)
    ]
