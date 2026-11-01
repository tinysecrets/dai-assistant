#!/usr/bin/env python3
"""Small authenticated client for the OMEGA and SENTINEL loopback API."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:8790"
SENTINEL_URL = "http://127.0.0.1:8791"
TOKEN_FILE = Path("/data/dai-execution-sandbox/state/api.token")
SENTINEL_TOKEN_FILE = Path("/data/dai-execution-sandbox/state/sentinel.token")


def request(
    method: str,
    path: str,
    payload=None,
    authenticated: bool = True,
    base_url: str = BASE_URL,
    token_file: Path = TOKEN_FILE,
):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if authenticated:
        try:
            token = token_file.read_text(encoding="ascii").strip()
        except OSError:
            raise RuntimeError(
                f"API token is unavailable at {token_file}; is its service installed and started?"
            ) from None
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=330) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"error": "http_error"}
        return exc.code, body
    except urllib.error.URLError as exc:
        raise RuntimeError(f"sandbox API unreachable: {exc.reason}") from None


def load_json(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read JSON file {path}: {exc}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="DAI isolated execution sandbox client")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("health")
    sub.add_parser("sentinel-health")
    sub.add_parser("projects")

    write = sub.add_parser("write", help="OMEGA: create project source files")
    write.add_argument("--project", required=True)
    write.add_argument("--manifest", required=True, help="JSON object: {files:[{path,content}]}")
    write.add_argument("--overwrite", action="store_true")

    run = sub.add_parser("run", help="OMEGA: execute argv in an ephemeral isolated copy")
    run.add_argument("--project", required=True)
    run.add_argument("--argv-json", required=True, help='JSON argv array, e.g. \'["python3","-m","unittest"]\'')
    run.add_argument("--cwd", default=".")
    run.add_argument("--timeout", type=int, default=120)

    verify = sub.add_parser("verify", help="SENTINEL: independently run tests and return PASS/FAIL")
    verify.add_argument("--project", required=True)
    verify.add_argument("--tests", required=True, help="JSON file containing an array of argv arrays")
    verify.add_argument("--required-file", action="append", default=[])
    verify.add_argument("--timeout", type=int, default=120)

    args = parser.parse_args()
    try:
        if args.action == "health":
            status, result = request("GET", "/health", authenticated=False)
        elif args.action == "sentinel-health":
            status, result = request("GET", "/health", authenticated=False, base_url=SENTINEL_URL)
        elif args.action == "projects":
            status, result = request("GET", "/v1/projects")
        elif args.action == "write":
            manifest = load_json(args.manifest)
            if not isinstance(manifest, dict):
                raise RuntimeError("manifest must be a JSON object")
            status, result = request(
                "POST",
                f"/v1/projects/{args.project}/files",
                {
                    "files": manifest.get("files"),
                    "overwrite": args.overwrite,
                },
            )
        elif args.action == "run":
            try:
                argv = json.loads(args.argv_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid --argv-json: {exc}") from None
            status, result = request(
                "POST",
                f"/v1/projects/{args.project}/run",
                {
                    "argv": argv,
                    "cwd": args.cwd,
                    "timeout_seconds": args.timeout,
                },
            )
        else:
            tests = load_json(args.tests)
            status, result = request(
                "POST",
                f"/v1/projects/{args.project}/verify",
                {
                    "tests": tests,
                    "required_files": args.required_file,
                    "timeout_seconds": args.timeout,
                },
                base_url=SENTINEL_URL,
                token_file=SENTINEL_TOKEN_FILE,
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if status >= 400 or result.get("ok") is False:
            return 2
        return 0
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
