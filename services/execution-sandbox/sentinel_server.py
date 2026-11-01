#!/usr/bin/env python3
"""SENTINEL verifier API. Its systemd unit mounts project sources read-only."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from common import SandboxError, audit, ensure_sentinel_token, ensure_state
from sentinel import verify_project

HOST = "127.0.0.1"
PORT = int(os.environ.get("DAI_SENTINEL_PORT", "8791"))
MAX_BODY = 1_048_576
ACTIVE_LIMIT = threading.BoundedSemaphore(1)
API_TOKEN = ""


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "DAI-SENTINEL/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        return bool(supplied) and hmac.compare_digest(supplied, API_TOKEN)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise SandboxError("Content-Length is required") from None
        if length < 2 or length > MAX_BODY:
            raise SandboxError(f"request body must be between 2 and {MAX_BODY} bytes")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise SandboxError("request body ended before Content-Length")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SandboxError("request body must be valid UTF-8 JSON") from None
        if not isinstance(value, dict):
            raise SandboxError("request JSON must be an object")
        return value

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/health":
            self._send(404, {"ok": False, "error": "not_found"})
            return
        self._send(
            200,
            {
                "ok": True,
                "service": "dai-sentinel",
                "version": "1.0.0",
                "bind": f"{HOST}:{PORT}",
                "project_mount": "read-only (systemd ReadOnlyPaths)",
                "source_mount_in_worker": "read-only",
                "network": "unshared",
                "bwrap_available": shutil.which("bwrap") is not None,
            },
        )

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        if not ACTIVE_LIMIT.acquire(blocking=False):
            self._send(429, {"ok": False, "error": "sentinel_busy"})
            return
        started = time.monotonic()
        try:
            body = self._body()
            parts = [unquote(x) for x in urlsplit(self.path).path.split("/") if x]
            if len(parts) != 4 or parts[:2] != ["v1", "projects"] or parts[3] != "verify":
                self._send(404, {"ok": False, "error": "not_found"})
                return
            project = parts[2]
            report = verify_project(
                project,
                body.get("tests"),
                body.get("required_files", []),
                body.get("timeout_seconds", 120),
            )
            self._send(200, report)
        except SandboxError as exc:
            self._send(400, {"ok": False, "error": "invalid_request", "detail": str(exc)})
        except Exception as exc:
            audit(
                "sentinel_api_error",
                {
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            self._send(500, {"ok": False, "error": "internal_error", "detail": type(exc).__name__})
        finally:
            ACTIVE_LIMIT.release()


def main() -> None:
    global API_TOKEN
    ensure_state()
    API_TOKEN = ensure_sentinel_token()
    server = Server((HOST, PORT), Handler)
    print(f"SENTINEL verifier listening on http://{HOST}:{PORT}", flush=True)
    print("SENTINEL token is stored in state/sentinel.token (0600); it is never logged.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
