#!/usr/bin/env python3
"""Local smoke test for the authenticated DAI phone interface; never prints token."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

base = f"http://{os.environ.get('DAI_PHONE_BIND', '100.105.171.108')}:{os.environ.get('DAI_PHONE_PORT', '8767')}"
token_path = Path(os.environ.get("DAI_PHONE_TOKEN_FILE") or (Path.home() / ".config" / "dai-phone-ui" / "token"))
token = token_path.read_text(encoding="utf-8").strip()

def request(path, body=None, auth=False):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=190) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

status, body = request("/health")
assert status == 200 and json.loads(body).get("ok") is True, "health failed"
print("PASS health endpoint")
status, body = request("/api/chat", {"messages":[{"role":"user","content":"should be rejected"}]})
assert status == 401, f"unauthenticated request was not rejected: {status}"
print("PASS unauthorized request rejected")
status, body = request("/api/chat", {"messages":[{"role":"user","content":"Reply with exactly: DAI phone bridge test passed"}]}, True)
assert status == 200, f"authenticated chat failed: HTTP {status} {body[:500]!r}"
doc = json.loads(body)
answer = doc.get("choices", [{}])[0].get("message", {}).get("content", "")
assert answer, "router returned no assistant text"
print("PASS authenticated chat returned HTTP 200")
print("model:", doc.get("model", "unknown"))
print("answer:", " ".join(answer.split())[:240])
print("route:", json.dumps(doc.get("dai_routed", {}), separators=(",", ":")))
