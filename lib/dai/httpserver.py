"""HTTP server base for the spine services.

Guarantees the two things the previous hand-rolled handlers got wrong:

* **Every request gets a response.**  A malformed body, a missing policy file
  or an unexpected exception used to escape into ``socketserver`` and drop the
  connection with no status line at all.  Now it is a JSON error: 400, 413,
  500 — with the traceback logged locally and never sent to the client.
* **No secret ever reaches a response or a log line.**  All outbound text goes
  through the configured :class:`~dai.redact.Redactor`.

Also provides HTTP/1.1 keep-alive, chunked SSE streaming, optional bearer
auth, and a clean startup/shutdown path.
"""

from __future__ import annotations

import json
import signal
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib import parse

from .env import Config
from .redact import Redactor

MAX_BODY_DEFAULT = 8 * 1024 * 1024


class HttpError(Exception):
    """An error that should be rendered as a JSON HTTP response."""

    def __init__(
        self,
        status: int,
        error: str,
        detail: Optional[str] = None,
        *,
        extra: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.detail = detail
        self.extra = extra or {}
        self.headers = headers or {}

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"error": self.error}
        if self.detail:
            payload["detail"] = self.detail
        payload.update(self.extra)
        return payload


class ServiceStartupError(RuntimeError):
    """Raised when a service cannot bind its port."""


class ServiceInfo:
    """Static identity for a service, surfaced on ``/health``."""

    def __init__(self, name: str, version: str = "1.0", docs: str = "") -> None:
        self.name = name
        self.version = version
        self.docs = docs

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"service": self.name, "version": self.version}
        if self.docs:
            out["docs"] = self.docs
        return out


class JsonHandler(BaseHTTPRequestHandler):
    """Base request handler.

    Subclasses set :attr:`config` / :attr:`service_info` (``create_server``
    does it for them) and implement :meth:`dispatch`.
    """

    protocol_version = "HTTP/1.1"
    server_version = "dai-spine/1.0"
    sys_version = ""

    config: Config = Config(root=Path("."), port=0)
    service_info: ServiceInfo = ServiceInfo("dai")
    redactor: Redactor = Redactor()

    # --- plumbing ---------------------------------------------------------
    @property
    def max_body_bytes(self) -> int:
        return getattr(self.config, "max_body_bytes", MAX_BODY_DEFAULT) or MAX_BODY_DEFAULT

    def parsed_path(self) -> Tuple[str, Dict[str, str]]:
        parts = parse.urlsplit(self.path)
        query = {k: v[-1] for k, v in parse.parse_qs(parts.query, keep_blank_values=True).items()}
        return parts.path.rstrip("/") or "/", query

    # --- request body -----------------------------------------------------
    def read_body(self) -> bytes:
        """Read the request body, enforcing the size cap.

        Always drains what it declares it will read so keep-alive stays in sync.
        """
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                raise HttpError(411, "length_required", "Chunked request bodies are not supported; send Content-Length.")
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            raise HttpError(400, "bad_content_length", "Content-Length must be an integer.") from None
        if length < 0:
            raise HttpError(400, "bad_content_length", "Content-Length must not be negative.")
        if length > self.max_body_bytes:
            # Cannot safely drain an arbitrarily large body: end the connection.
            self.close_connection = True
            raise HttpError(
                413,
                "payload_too_large",
                f"Request body exceeds the {self.max_body_bytes} byte limit.",
                headers={"Connection": "close"},
            )
        remaining = length
        chunks = []
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_json(self, *, required: bool = False) -> Dict[str, Any]:
        """Parse a JSON object body; malformed input is a clean 400."""
        raw = self.read_body()
        if not raw:
            if required:
                raise HttpError(400, "body_required", "A JSON request body is required.")
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HttpError(400, "invalid_json", f"Request body is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise HttpError(400, "invalid_json", "Request body must be a JSON object.")
        return parsed

    # --- auth -------------------------------------------------------------
    def check_auth(self) -> None:
        """Enforce the optional bearer token (unset by default → localhost-only)."""
        expected = getattr(self.config, "auth_token", "") or ""
        if not expected:
            return
        header = self.headers.get("Authorization") or ""
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
        if supplied != expected:
            raise HttpError(
                401,
                "unauthorized",
                "This service requires a bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # --- responses --------------------------------------------------------
    def _start(self, code: int, headers: Dict[str, str]) -> None:
        self.send_response_only(code)
        self.send_header("Server", self.server_version)
        self.send_header("Date", self.date_time_string())
        for key, value in headers.items():
            self.send_header(key, value)

    def send_json(self, code: int, payload: Any, *, headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        all_headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        all_headers.update(headers or {})
        self._response_status = code
        self._start(code, all_headers)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def send_error_json(self, err: HttpError) -> None:
        headers = dict(err.headers)
        if self.close_connection:
            headers.setdefault("Connection", "close")
        self.send_json(err.status, err.to_payload(), headers=headers)

    def send_error(self, code: int, message: str = "", explain: str = "") -> None:
        """Render protocol-level errors (bad request line, etc.) as JSON too.

        ``BaseHTTPRequestHandler`` answers these with an HTML page, which a
        JSON client cannot parse; the status code is preserved.
        """
        try:
            self._response_status = code
            self.send_json(
                code,
                {"error": "bad_request" if code == 400 else "http_protocol_error", "detail": (message or "").strip()},
                headers={"Connection": "close"} if self.close_connection else None,
            )
        except Exception:  # noqa: BLE001 - the socket is already unusable
            self.close_connection = True

    # --- streaming (chunked SSE) -----------------------------------------
    def start_stream(self, *, content_type: str = "text/event-stream", headers: Optional[Dict[str, str]] = None) -> None:
        all_headers = {
            "Content-Type": content_type,
            "Transfer-Encoding": "chunked",
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        }
        all_headers.update(headers or {})
        self._response_status = 200
        self._start(200, all_headers)
        self.end_headers()
        self._streaming = True

    def write_stream(self, data: bytes | str) -> bool:
        """Write one chunk.  Returns ``False`` once the client has gone away."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data:
            return True
        try:
            self.wfile.write(b"%x\r\n%b\r\n" % (len(data), data))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            return False

    def end_stream(self) -> None:
        if not getattr(self, "_streaming", False):
            return
        self._streaming = False
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    # --- dispatch ---------------------------------------------------------
    def dispatch(self, method: str, path: str, query: Dict[str, str]) -> None:  # pragma: no cover
        raise HttpError(404, "not_found", f"No route for {method} {path}")

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        path, query = self.parsed_path()
        self._response_status = 0
        self._streaming = False
        try:
            self.dispatch(method, path, query)
        except HttpError as err:
            if not getattr(self, "_streaming", False):
                self.send_error_json(err)
            else:
                self.write_stream(f"event: error\ndata: {json.dumps(err.to_payload())}\n\n")
                self.end_stream()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - the client gets a clean 500
            detail = self.redactor.redact("".join(traceback.format_exception_only(type(exc), exc)).strip())
            self.log_error("unhandled exception in %s %s: %s", method, path, detail)
            if getattr(self, "_streaming", False):
                self.write_stream(f"event: error\ndata: {json.dumps({'error': 'internal_error'})}\n\n")
                self.end_stream()
            else:
                try:
                    self.send_error_json(
                        HttpError(500, "internal_error", "The service hit an unexpected error; see its log.")
                    )
                except Exception:  # noqa: BLE001 - connection is already unusable
                    self.close_connection = True
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            self.log_access(method, path, self._response_status or 0, elapsed_ms)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    # --- logging ----------------------------------------------------------
    def log_access(self, method: str, path: str, status: int, elapsed_ms: float) -> None:
        if getattr(self.config, "quiet", False):
            return
        line = f"{method} {path} -> {status} in {elapsed_ms:.0f}ms from {self.address_string()}"
        self.log_message("%s", line)

    def log_error(self, fmt: str, *args: Any) -> None:
        self.log_message("ERROR " + fmt, *args)

    def log_message(self, fmt: str, *args: Any) -> None:
        text = fmt % args if args else fmt
        stream = sys.stderr if text.startswith("ERROR") else sys.stdout
        try:
            stream.write(f"[{self.service_info.name}] {self.redactor.redact(text)}\n")
            stream.flush()
        except Exception:  # noqa: BLE001 - logging must never break a request
            pass


class SpineServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    redactor: Redactor = Redactor()

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: ANN401
        """Log socket-level errors without dumping a traceback on a hung-up client."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout, ConnectionAbortedError)):
            return
        sys.stderr.write(self.redactor.redact(traceback.format_exc()))


def create_server(
    config: Config,
    handler_cls: Callable[..., JsonHandler],
    *,
    service: Optional[ServiceInfo] = None,
    redactor: Optional[Redactor] = None,
    bind: Optional[Dict[str, Any]] = None,
) -> SpineServer:
    """Bind a server whose handler class carries this config.

    A fresh subclass is built per server so two services (or two test cases)
    can run in one process without sharing class-level state.  ``bind`` adds
    service-specific class attributes (state objects, caches).
    """
    service = service or ServiceInfo("dai")
    attrs: Dict[str, Any] = {
        "config": config,
        "service_info": service,
        "redactor": redactor or Redactor(),
        "server_version": f"dai-{service.name}/{service.version}",
    }
    attrs.update(bind or {})
    bound = type(
        f"Bound{getattr(handler_cls, '__name__', 'Handler')}",
        (handler_cls,),  # type: ignore[misc]
        attrs,
    )
    try:
        httpd = SpineServer((config.host, config.port), bound)
    except OSError as exc:
        raise ServiceStartupError(
            f"{service.name} cannot bind {config.host}:{config.port} ({exc}). "
            "Another process may hold the port; check with: ss -ltnp"
        ) from exc
    httpd.redactor = redactor or Redactor()  # type: ignore[attr-defined]
    return httpd


def serve_forever(
    httpd: SpineServer,
    *,
    on_shutdown: Optional[Callable[[], None]] = None,
    banner: Optional[str] = None,
) -> None:
    """Serve until SIGINT/SIGTERM, then flush state and exit cleanly."""
    if banner:
        print(banner, flush=True)
    stopping = threading.Event()

    def _stop(signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        print(f"received signal {signum}; shutting down", flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):  # not on the main thread
            pass
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        try:
            httpd.server_close()
        finally:
            if on_shutdown:
                on_shutdown()
