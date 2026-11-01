#!/usr/bin/env python3
"""Loopback-only authenticated API for OMEGA writes, sandbox runs and SENTINEL."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from common import SandboxError, audit, ensure_api_token, ensure_layout, list_projects, write_files
from sandbox import run_project

HOST = "127.0.0.1"
PORT = int(os.environ.get("DAI_SANDBOX_PORT", "8790"))
MAX_BODY = 12 * 1024 * 1024
ACTIVE_LIMIT = threading.BoundedSemaphore(1)
API_TOKEN = ""


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "DAI-Execution-Sandbox/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        # Do not log request bodies, bearer tokens or generated source.
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
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
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
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(
                200,
                {
                    "ok": True,
                    "service": "dai-execution-sandbox",
                    "version": "1.0.0",
                    "bind": f"{HOST}:{PORT}",
                    "isolation": {
                        "runtime": "bubblewrap",
                        "user_namespaces": "kernel-probed",
                        "network": "unshared",
                        "rootfs": "ephemeral-tmpfs",
                        "project_source": "read-only-copy-in",
                        "execution_workspace": "256 MiB tmpfs",
                        "persistent_write_route": "OMEGA-only API",
                        "verification": "SENTINEL independent run",
                    },
                    "bwrap_available": shutil.which("bwrap") is not None,
                },
            )
            return
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        if path == "/v1/projects":
            self._send(200, {"ok": True, "projects": list_projects()})
            return
        self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        if not ACTIVE_LIMIT.acquire(blocking=False):
            self._send(
                429, {"ok": False, "error": "sandbox_busy", "detail": "Two sandbox operations are already active"}
            )
            return
        started = time.monotonic()
        try:
            body = self._body()
            parts = [unquote(x) for x in urlsplit(self.path).path.split("/") if x]
            if len(parts) != 4 or parts[:2] != ["v1", "projects"]:
                self._send(404, {"ok": False, "error": "not_found"})
                return
            project, action = parts[2], parts[3]
            if action == "files":
                result = write_files(project, body.get("files"), body.get("overwrite", False))
                self._send(201, result)
                return
            if action == "run":
                result = run_project(
                    project,
                    body.get("argv"),
                    body.get("cwd", "."),
                    body.get("timeout_seconds", 120),
                )
                audit(
                    "omega_sandbox_run",
                    {
                        "project": project,
                        "argv": result.get("argv", []),
                        "exit_code": result.get("exit_code"),
                        "timed_out": result.get("timed_out"),
                        "source_unchanged": result.get("source_unchanged"),
                        "duration_ms": result.get("duration_ms"),
                    },
                )
                self._send(200, result)
                return
            self._send(404, {"ok": False, "error": "not_found"})
        except SandboxError as exc:
            self._send(400, {"ok": False, "error": "invalid_request", "detail": str(exc)})
        except FileExistsError as exc:
            self._send(409, {"ok": False, "error": "file_exists", "detail": str(exc)})
        except Exception as exc:
            audit(
                "sandbox_api_error",
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
    ensure_layout()
    API_TOKEN = ensure_api_token()
    server = Server((HOST, PORT), Handler)
    print(f"DAI execution sandbox listening on http://{HOST}:{PORT}", flush=True)
    print("API token is stored in state/api.token (0600); it is never logged.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
