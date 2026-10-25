#!/usr/bin/env python3
"""Narrow authenticated mobile web interface for the local DAI model router."""
from __future__ import annotations
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = Path(os.environ.get("DAI_PHONE_TOKEN_FILE") or (Path.home() / ".config" / "dai-phone-ui" / "token"))
BIND_HOST = os.environ.get("DAI_PHONE_BIND", "100.105.171.108")
PORT = int(os.environ.get("DAI_PHONE_PORT", "8767"))
ROUTER = "http://127.0.0.1:11435/v1/chat/completions"
MAX_BODY = 1024 * 1024
MAX_MESSAGES = 50
MAX_TEXT = 100_000
ALLOWED_IPS = {"100.104.165.28", "100.105.171.108", "127.0.0.1", "::1"}
requests_by_ip = defaultdict(deque)

def read_token() -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Phone UI token file is missing; run bin/setup-phone-ui.py") from exc
    if len(token) < 40:
        raise RuntimeError("Phone UI token is invalid")
    return token

TOKEN = read_token()

class Handler(BaseHTTPRequestHandler):
    server_version = "DAIPhoneUI/1.0"
    def log_message(self, fmt, *args):
        # Do not log prompts, auth headers, or user content.
        print("DAIPhoneUI %s %s" % (self.client_address[0], fmt.split(" ")[0]), flush=True)

    def send_json(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def allowed_peer(self):
        return self.client_address[0] in ALLOWED_IPS

    def do_GET(self):
        if not self.allowed_peer():
            return self.send_json(403, {"error": "peer_not_allowed"})
        if self.path == "/health":
            return self.send_json(200, {"ok": True, "service": "dai-phone-ui", "router": "loopback-only"})
        if self.path not in ("/", "/index.html"):
            return self.send_json(404, {"error": "not_found"})
        try:
            raw = (ROOT / "index.html").read_bytes()
        except OSError:
            return self.send_json(500, {"error": "ui_unavailable"})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if not self.allowed_peer():
            return self.send_json(403, {"error": "peer_not_allowed"})
        if self.path != "/api/chat":
            return self.send_json(404, {"error": "not_found"})
        supplied = self.headers.get("Authorization", "")
        if not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:].strip(), TOKEN):
            return self.send_json(401, {"error": "unauthorized"})
        now = time.time()
        bucket = requests_by_ip[self.client_address[0]]
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= 20:
            return self.send_json(429, {"error": "rate_limited", "detail": "Try again in a minute."})
        bucket.append(now)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json(400, {"error": "invalid_length"})
        if length < 2 or length > MAX_BODY:
            return self.send_json(413 if length > MAX_BODY else 400, {"error": "payload_too_large" if length > MAX_BODY else "empty_body"})
        try:
            doc = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.send_json(400, {"error": "invalid_json"})
        messages = doc.get("messages") if isinstance(doc, dict) else None
        if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
            return self.send_json(400, {"error": "messages_required_or_too_many"})
        clean = []
        total = 0
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") not in ("system", "user", "assistant"):
                return self.send_json(400, {"error": "invalid_message"})
            content = msg.get("content")
            if not isinstance(content, str):
                return self.send_json(400, {"error": "text_only_supported"})
            total += len(content)
            clean.append({"role": msg["role"], "content": content})
        if total > MAX_TEXT:
            return self.send_json(413, {"error": "message_text_too_large"})
        payload = json.dumps({"model": "dai/auto", "messages": clean, "stream": False, "max_tokens": 2048}).encode()
        req = urllib.request.Request(ROUTER, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    return self.send_json(502, {"error": "router_response_too_large"})
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = exc.read(65536)
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            return self.send_json(502, {"error": "router_unavailable", "detail": "The local DAI router did not respond."})

if __name__ == "__main__":
    if BIND_HOST not in ("100.105.171.108", "127.0.0.1"):
        raise SystemExit("Refusing unexpected bind address")
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"DAI phone UI listening on {BIND_HOST}:{PORT}; router remains on loopback", flush=True)
    server.serve_forever()
