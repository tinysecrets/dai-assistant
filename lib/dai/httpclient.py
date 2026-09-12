"""Upstream HTTP client: JSON calls plus SSE streaming.

Network failures never raise out of this module — they come back as a
synthetic status (``502`` for transport errors, ``504`` for timeouts) with an
already-redacted error body, so callers can treat every outcome uniformly and
rotate to the next provider.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib import error, parse, request

from .redact import Redactor
from .redact import redact as _default_redact

USER_AGENT = "dai-spine/1.0 (+https://localhost/debian-ai)"

STATUS_NETWORK_ERROR = 502
STATUS_TIMEOUT = 504


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: Dict[str, str]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self, default: Any = None) -> Any:
        if not self.body:
            return {} if default is None else default
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return default


def _build_request(
    method: str,
    url: str,
    body: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
) -> request.Request:
    data = None
    extra = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        extra["Content-Type"] = "application/json"
    for key, value in (headers or {}).items():
        if value:
            extra[key] = value
    return request.Request(url, data=data, method=method, headers=extra)


def _error_body(exc: error.HTTPError, redactor: Optional[Redactor]) -> Dict[str, Any]:
    redact = redactor.redact if redactor else _default_redact
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    if not raw:
        return {"error": {"message": redact(str(exc.reason) or exc.msg), "type": "upstream_http_error"}}
    try:
        parsed = json.loads(raw)
        # Re-serialize through the redactor so embedded keys are scrubbed.
        return json.loads(redact(json.dumps(parsed)))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"error": {"message": redact(raw[:2000]), "type": "upstream_non_json"}}


def open_raw(
    method: str,
    url: str,
    body: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 120.0,
    redactor: Optional[Redactor] = None,
) -> Tuple[int, Any, Optional[Any]]:
    """Perform a request, returning ``(status, parsed_body, raw_response)``.

    ``raw_response`` is non-``None`` only on success and must be closed by the
    caller (used for streaming).  On any failure the parsed body is a dict with
    an ``error`` key and ``raw_response`` is ``None``.
    """
    req = _build_request(method, url, body, headers)
    try:
        resp = request.urlopen(req, timeout=timeout)
    except error.HTTPError as exc:
        return exc.code, _error_body(exc, redactor), None
    except socket.timeout as exc:
        return (
            STATUS_TIMEOUT,
            {"error": {"message": f"upstream timeout after {timeout}s: {exc}", "type": "timeout"}},
            None,
        )
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            return STATUS_TIMEOUT, {"error": {"message": f"upstream timeout after {timeout}s", "type": "timeout"}}, None
        return (
            STATUS_NETWORK_ERROR,
            {"error": {"message": (redactor.redact if redactor else _default_redact)(str(reason)), "type": "network"}},
            None,
        )
    except Exception as exc:
        return (
            STATUS_NETWORK_ERROR,
            {"error": {"message": (redactor.redact if redactor else _default_redact)(str(exc)), "type": "unexpected"}},
            None,
        )

    raw = resp.read()
    resp.close()
    if not raw:
        return resp.status, {}, None
    try:
        return resp.status, json.loads(raw.decode("utf-8", errors="replace")), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (
            resp.status,
            {
                "error": {
                    "message": (redactor.redact if redactor else _default_redact)(
                        raw[:2000].decode("utf-8", "replace")
                    ),
                    "type": "non_json",
                }
            },
            None,
        )


def request_json(
    method: str,
    url: str,
    body: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 120.0,
    redactor: Optional[Redactor] = None,
) -> Tuple[int, Any]:
    """JSON request → ``(status, parsed_json_or_error_dict)``.  Never raises."""
    status, parsed, _ = open_raw(method, url, body, headers, timeout=timeout, redactor=redactor)
    return status, parsed


def open_stream(
    method: str,
    url: str,
    body: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 120.0,
    redactor: Optional[Redactor] = None,
) -> Tuple[int, Any, Optional[Any]]:
    """Open a streaming request without reading the body.

    Returns ``(status, error_or_headers_dict, response_object)``.  When
    ``response_object`` is not ``None`` the caller owns it and must close it.
    """
    req = _build_request(method, url, body, headers)
    req.add_header("Accept", "text/event-stream")
    try:
        resp = request.urlopen(req, timeout=timeout)
        return resp.status, dict(resp.headers.items()), resp
    except error.HTTPError as exc:
        return exc.code, _error_body(exc, redactor), None
    except socket.timeout:
        return STATUS_TIMEOUT, {"error": {"message": f"upstream timeout after {timeout}s", "type": "timeout"}}, None
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return (
            STATUS_NETWORK_ERROR,
            {"error": {"message": (redactor.redact if redactor else _default_redact)(str(reason)), "type": "network"}},
            None,
        )
    except Exception as exc:
        return (
            STATUS_NETWORK_ERROR,
            {"error": {"message": (redactor.redact if redactor else _default_redact)(str(exc)), "type": "unexpected"}},
            None,
        )


def iter_sse_lines(response: Any) -> Iterator[bytes]:
    """Yield raw lines (including the trailing newline) from an SSE response."""
    while True:
        line = response.readline()
        if not line:
            break
        yield line


def url_join(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def encode_query(params: Dict[str, Any]) -> str:
    return parse.urlencode({k: v for k, v in params.items() if v is not None})
