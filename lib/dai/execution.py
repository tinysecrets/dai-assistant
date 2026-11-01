"""Role-separated client and repository snapshot integration for DAI execution."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

STATE = Path(os.environ.get("DAI_SANDBOX_STATE", "/data/dai-execution-sandbox/state"))
OMEGA_URL = os.environ.get("DAI_OMEGA_URL", "http://127.0.0.1:8790").rstrip("/")
SENTINEL_URL = os.environ.get("DAI_SENTINEL_URL", "http://127.0.0.1:8791").rstrip("/")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_RESPONSE = 12 * 1024 * 1024
MAX_FILES = 500
MAX_FILE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 10 * 1024 * 1024
SKIP_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "coverage",
    "skills-ready",
}
SKIP_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
    "api.token",
    "sentinel.token",
}


class ExecutionError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status, self.payload = status, payload
        super().__init__(str(payload.get("detail") or payload.get("error") or "execution request failed"))


def repository_manifest(root: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Snapshot UTF-8 Git files safely; exclude secrets, binaries and generated trees."""
    root = Path(root).resolve(strict=True)
    if not (root / ".git").exists():
        raise ValueError("snapshot root must be a Git worktree")
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    if proc.returncode:
        raise RuntimeError("git file inventory failed: " + proc.stderr.decode("utf-8", "replace")[:1000])
    files: list[dict[str, str]] = []
    skipped: list[str] = []
    total = 0
    seen: set[str] = set()
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="strict")
        parts = Path(rel).parts
        if not parts or any(part in ("", ".", "..") or part in SKIP_PARTS for part in parts):
            skipped.append(rel)
            continue
        name = parts[-1].lower()
        if name in SKIP_NAMES or (name.startswith(".env.") and name != ".env.example"):
            skipped.append(rel)
            continue
        if name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore")):
            skipped.append(rel)
            continue
        path = root.joinpath(*parts)
        try:
            st = path.lstat()
        except OSError:
            skipped.append(rel)
            continue
        if path.is_symlink() or not path.is_file():
            skipped.append(rel)
            continue
        if st.st_size > MAX_FILE_BYTES:
            raise ValueError(f"source file exceeds 1 MiB sandbox limit: {rel}")
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            skipped.append(rel + " [binary/unreadable]")
            continue
        size = len(source.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValueError(f"UTF-8 source file exceeds 1 MiB sandbox limit: {rel}")
        if rel in seen:
            continue
        seen.add(rel)
        total += size
        if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ValueError("repository snapshot exceeds the sandbox 500-file/10-MiB limits")
        files.append({"path": rel, "content": source, "executable": bool(st.st_mode & 0o111)})
    if not files:
        raise ValueError("repository snapshot contains no eligible text files")
    return files, {
        "included_files": len(files),
        "included_bytes": total,
        "skipped_count": len(skipped),
        "skipped_paths": skipped[:100],
    }


class ExecutionClient:
    """OMEGA writes/builds; SENTINEL verifies with its independent token."""

    def __init__(self, role: str = "omega", timeout: int = 180) -> None:
        if role not in ("omega", "sentinel"):
            raise ValueError("role must be omega or sentinel")
        self.role, self.timeout = role, max(1, int(timeout))
        self.base = OMEGA_URL if role == "omega" else SENTINEL_URL
        self.token_path = STATE / ("api.token" if role == "omega" else "sentinel.token")

    def _token(self) -> str:
        if self.token_path.is_symlink():
            raise RuntimeError("execution token must not be a symbolic link")
        try:
            st = self.token_path.stat()
            if not self.token_path.is_file() or st.st_mode & 0o077:
                raise RuntimeError("execution token must be a regular 0600 file")
            value = self.token_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise RuntimeError("execution token is missing") from exc
        if len(value) < 40:
            raise RuntimeError("execution token is invalid")
        return value

    def request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> Any:
        if not path.startswith("/") or path.startswith("//") or "://" in path or "\x00" in path:
            raise ValueError("request path must be local and absolute")
        headers = {"Accept": "application/json", "Authorization": "Bearer " + self._token()}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(data) > MAX_TOTAL_BYTES + 2 * 1024 * 1024:
                raise ValueError("request payload exceeds 12 MiB")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise RuntimeError("execution response exceeded 12 MiB")
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read(1024 * 1024)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                value = {"detail": raw.decode("utf-8", errors="replace")[:2000]}
            raise ExecutionError(exc.code, value if isinstance(value, dict) else {"detail": str(value)}) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("execution service unavailable: " + str(exc.reason)) from exc

    def health(self) -> dict[str, Any]:
        req = urllib.request.Request(self.base + "/health", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout, 5)) as response:
                return json.loads(response.read(65536).decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError("execution service unavailable: " + str(exc.reason)) from exc

    @staticmethod
    def _project(value: str) -> str:
        if not isinstance(value, str) or not PROJECT_RE.fullmatch(value) or value in {".", ".."}:
            raise ValueError("project must be a safe 1-64 character slug")
        return value

    def projects(self) -> dict[str, Any]:
        if self.role != "omega":
            raise PermissionError("project listing is restricted to OMEGA")
        return self.request("GET", "/v1/projects")

    def write_files(self, project: str, files: list[dict[str, str]], overwrite: bool = False) -> dict[str, Any]:
        if self.role != "omega":
            raise PermissionError("only OMEGA may write project files")
        project = self._project(project)
        if not isinstance(files, list) or not files:
            raise ValueError("files must be a non-empty list of path/content objects")
        return self.request("POST", f"/v1/projects/{project}/files", {"files": files, "overwrite": bool(overwrite)})

    def run(self, project: str, argv: list[str], cwd: str = ".", timeout_seconds: int = 120) -> dict[str, Any]:
        if self.role != "omega":
            raise PermissionError("only OMEGA may request build execution")
        project = self._project(project)
        return self.request(
            "POST",
            f"/v1/projects/{project}/run",
            {
                "argv": argv,
                "cwd": cwd,
                "timeout_seconds": int(timeout_seconds),
            },
        )

    def verify(
        self,
        project: str,
        tests: list[list[str]],
        required_files: Optional[list[str]] = None,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        if self.role != "sentinel":
            raise PermissionError("only SENTINEL may verify project artifacts")
        project = self._project(project)
        return self.request(
            "POST",
            f"/v1/projects/{project}/verify",
            {
                "tests": tests,
                "required_files": required_files or [],
                "timeout_seconds": int(timeout_seconds),
            },
        )

    def write_build_verify(
        self,
        project: str,
        files: list[dict[str, str]],
        build_argv: list[str],
        sentinel_tests: list[list[str]],
        required_files: Optional[list[str]] = None,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        if self.role != "omega":
            raise PermissionError("the full workflow must be initiated by OMEGA")
        written = self.write_files(project, files)
        build = self.run(project, build_argv, timeout_seconds=timeout_seconds)
        if not build.get("ok"):
            return {"ok": False, "state": "BUILD_FAILED", "write": written, "build": build, "sentinel": None}
        verification = ExecutionClient("sentinel", timeout=max(self.timeout, timeout_seconds + 15)).verify(
            project, sentinel_tests, required_files, timeout_seconds
        )
        ok = verification.get("verdict") == "PASS" and verification.get("ok") is True
        return {
            "ok": ok,
            "state": "COMPLETED" if ok else "VERIFICATION_FAILED",
            "write": written,
            "build": build,
            "sentinel": verification,
        }

    def snapshot_build_verify(self, root: Path, project: str, timeout_seconds: int = 180) -> dict[str, Any]:
        """Persist a complete source snapshot, compile it, then run DAI's suite independently."""
        if self.role != "omega":
            raise PermissionError("only OMEGA may snapshot and dispatch a build")
        files, inventory = repository_manifest(root)
        required = [
            "lib/dai/execution.py",
            "services/execution-sandbox/server.py",
            "services/execution-sandbox/sentinel_server.py",
            "tests/test_repo_consistency.py",
        ]
        included = {item["path"] for item in files}
        missing_required = [item for item in required if item not in included]
        if missing_required:
            raise ValueError("source snapshot is missing required verification files: " + ", ".join(missing_required))
        written = self.write_files(project, files)
        build = self.run(project, ["python3", "-m", "compileall", "-q", "."], timeout_seconds=min(120, timeout_seconds))
        if not build.get("ok"):
            return {
                "ok": False,
                "state": "BUILD_FAILED",
                "project": project,
                "inventory": inventory,
                "write": written,
                "build": build,
                "sentinel": None,
            }
        runner = (
            "import subprocess,sys,unittest; "
            "r=subprocess.run(['git','init','-q','-b','main'],capture_output=True,text=True); "
            "r.returncode==0 or sys.exit('git init failed: '+r.stderr); "
            "r=subprocess.run(['git','add','-A'],capture_output=True,text=True); "
            "r.returncode==0 or sys.exit('git add failed: '+r.stderr); "
            "suite=unittest.defaultTestLoader.discover('tests',pattern='test_*.py',top_level_dir='.'); "
            "n=suite.countTestCases(); "
            "n>0 or sys.exit('SENTINEL discovered zero repository tests'); "
            "result=unittest.TextTestRunner(verbosity=1).run(suite); "
            "sys.exit(0 if result.wasSuccessful() else 1)"
        )
        sentinel = ExecutionClient("sentinel", timeout=max(self.timeout, timeout_seconds + 30))
        verification = sentinel.verify(
            project,
            [["python3", "-c", runner]],
            required_files=required,
            timeout_seconds=min(300, max(1, timeout_seconds)),
        )
        ok = verification.get("verdict") == "PASS" and verification.get("ok") is True
        return {
            "ok": ok,
            "state": "COMPLETED" if ok else "VERIFICATION_FAILED",
            "project": project,
            "inventory": inventory,
            "write": written,
            "build": build,
            "sentinel": verification,
        }


SandboxClient = ExecutionClient
