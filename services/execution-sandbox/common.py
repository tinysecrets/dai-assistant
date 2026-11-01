#!/usr/bin/env python3
"""Shared, symlink-safe filesystem and audit primitives for DAI execution."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any

BASE = Path(os.environ.get("DAI_SANDBOX_ROOT", "/data/dai-execution-sandbox"))
PROJECTS = BASE / "projects"
STATE = BASE / "state"
TOKEN_FILE = STATE / "api.token"
AUDIT_FILE = STATE / "audit.jsonl"

MAX_FILES = 500
MAX_FILE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 10_485_760
MAX_TREE_FILES = 10_000
MAX_TREE_BYTES = 201_326_592
MAX_STORE_FILES = 100_000
MAX_STORE_BYTES = int(os.environ.get("DAI_SANDBOX_MAX_STORE_BYTES", str(10 * 1024 * 1024 * 1024)))
BLOCKED_DIRS = {".git", ".ssh", ".gnupg", ".config", ".local", ".cache"}
BLOCKED_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "credentials.json", "secrets.json"}
SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}


class SandboxError(ValueError):
    """A caller supplied an unsafe or invalid sandbox request."""


def ensure_state() -> None:
    if BASE.is_symlink() or not BASE.is_dir():
        raise SandboxError("sandbox root must be an existing real directory")
    STATE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STATE, 0o700)


def ensure_layout() -> None:
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if BASE.is_symlink():
        raise SandboxError("sandbox root must not be a symlink")
    os.chmod(BASE, 0o700)
    PROJECTS.mkdir(mode=0o700, exist_ok=True)
    STATE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(PROJECTS, 0o700)
    os.chmod(STATE, 0o700)


def validate_project(name: Any) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise SandboxError("project must be a 1-64 character slug using letters, digits, dot, underscore or hyphen")
    if name in {".", ".."}:
        raise SandboxError("invalid project name")
    return name


def safe_relpath(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 240:
        raise SandboxError("file path must be a non-empty relative path of at most 240 bytes")
    if value.startswith("/") or "\\" in value or "\x00" in value or ":" in value:
        raise SandboxError("absolute paths, backslashes, NUL and drive prefixes are forbidden")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise SandboxError("empty, dot and parent path components are forbidden")
    if any(len(part.encode("utf-8")) > 100 or any(ord(ch) < 32 for ch in part) for part in raw_parts):
        raise SandboxError("invalid or overlong path component")
    if any(part.lower() in BLOCKED_DIRS for part in raw_parts):
        raise SandboxError("protected metadata and credential directories are not writable")
    leaf = raw_parts[-1].lower()
    if leaf in BLOCKED_NAMES:
        raise SandboxError("credential-like filenames are not writable")
    if leaf.startswith(".env") and leaf not in SAFE_ENV_TEMPLATES:
        raise SandboxError("live environment files are not writable; use .env.example")
    if Path(leaf).suffix in {".pem", ".key", ".p12", ".pfx", ".der", ".jks"}:
        raise SandboxError("private-key and certificate material is not writable")
    return tuple(raw_parts)


def project_path(name: Any, create: bool = True) -> Path:
    safe_name = validate_project(name)
    if create:
        ensure_layout()
    else:
        ensure_state()
    projects_fd = os.open(PROJECTS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(safe_name, 0o700, dir_fd=projects_fd)
        fd = os.open(safe_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=projects_fd)
        os.close(fd)
    except FileNotFoundError:
        raise SandboxError("project does not exist") from None
    finally:
        os.close(projects_fd)
    return PROJECTS / safe_name


def list_projects() -> list[dict[str, Any]]:
    ensure_layout()
    items: list[dict[str, Any]] = []
    with os.scandir(PROJECTS) as entries:
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                digest = tree_digest(Path(entry.path))
                items.append({"name": entry.name, "files": digest["files"], "bytes": digest["bytes"]})
            except Exception:
                items.append({"name": entry.name, "integrity": "invalid"})
    return sorted(items, key=lambda x: x["name"])


def store_usage() -> dict[str, int]:
    """Count the persistent project store without reading source contents."""
    ensure_layout()
    projects = files = total_bytes = 0
    with os.scandir(PROJECTS) as entries:
        for entry in entries:
            if entry.is_symlink():
                raise SandboxError("symlink found at project-store root")
            if not entry.is_dir(follow_symlinks=False):
                continue
            projects += 1
            for current, dirs, names in os.walk(entry.path, topdown=True, followlinks=False):
                for dirname in list(dirs):
                    candidate = Path(current) / dirname
                    st = candidate.lstat()
                    if not stat.S_ISDIR(st.st_mode):
                        raise SandboxError("symlink or special directory found in project store")
                for filename in names:
                    candidate = Path(current) / filename
                    st = candidate.lstat()
                    if not stat.S_ISREG(st.st_mode):
                        raise SandboxError("symlink or special file found in project store")
                    files += 1
                    total_bytes += st.st_size
                    if files > MAX_STORE_FILES or total_bytes > MAX_STORE_BYTES:
                        raise SandboxError("persistent project store has reached its global quota")
    return {"projects": projects, "files": files, "bytes": total_bytes}


def _existing_stat(root_fd: int, parts: tuple[str, ...]):
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise SandboxError("unsafe or non-directory parent path") from exc
            os.close(fd)
            fd = next_fd
        try:
            existing = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(existing.st_mode):
            raise SandboxError("existing target is not a regular file")
        return existing
    finally:
        os.close(fd)


def _open_parent(root_fd: int, parts: tuple[str, ...]) -> tuple[int, str]:
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o755, dir_fd=fd)
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except Exception:
        os.close(fd)
        raise


def _preflight_existing(root_fd: int, parts: tuple[str, ...], overwrite: bool) -> None:
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return
            os.close(fd)
            fd = next_fd
        try:
            existing = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not overwrite:
            raise SandboxError(f"file already exists (set overwrite=true to replace): {'/'.join(parts)}")
        if stat.S_ISDIR(existing.st_mode):
            raise SandboxError(f"cannot replace a directory with a file: {'/'.join(parts)}")
    finally:
        os.close(fd)


def write_files(name: str, files: Any, overwrite: bool = False) -> dict[str, Any]:
    if not isinstance(overwrite, bool):
        raise SandboxError("overwrite must be a boolean")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise SandboxError(f"files must contain 1-{MAX_FILES} entries")
    prepared: list[tuple[tuple[str, ...], bytes, bool]] = []
    seen: set[tuple[str, ...]] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) - {"path", "content", "executable"}:
            raise SandboxError("each file must contain only path, content and optional executable")
        executable = item.get("executable", False)
        if not isinstance(executable, bool):
            raise SandboxError("executable must be a boolean")
        parts = safe_relpath(item.get("path"))
        if parts in seen:
            raise SandboxError("duplicate file path in one write request")
        seen.add(parts)
        content = item.get("content")
        if not isinstance(content, str):
            raise SandboxError("file content must be UTF-8 text")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise SandboxError(f"file exceeds {MAX_FILE_BYTES} byte limit: {item['path']}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise SandboxError(f"request exceeds {MAX_TOTAL_BYTES} total content bytes")
        prepared.append((parts, data, executable))

    for parts, _data, _executable in prepared:
        if any(parts[:index] in seen for index in range(1, len(parts))):
            raise SandboxError("a manifest file path cannot also be a parent directory")

    root = project_path(name, create=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    results: list[dict[str, Any]] = []
    try:
        for parts, _data, _executable in prepared:
            _preflight_existing(root_fd, parts, overwrite)

        usage = store_usage()
        current_project = tree_digest(root)
        byte_delta = 0
        new_files = 0
        for parts, data, _executable in prepared:
            existing = _existing_stat(root_fd, parts)
            if existing is None:
                byte_delta += len(data)
                new_files += 1
            else:
                if not overwrite:
                    raise SandboxError("file already exists (set overwrite=true to replace): " + chr(47).join(parts))
                byte_delta += len(data) - existing.st_size

        if current_project["bytes"] + byte_delta > MAX_TREE_BYTES:
            raise SandboxError(f"project would exceed {MAX_TREE_BYTES} bytes")
        if current_project["files"] + new_files > MAX_TREE_FILES:
            raise SandboxError(f"project would exceed {MAX_TREE_FILES} files")
        if usage["bytes"] + byte_delta > MAX_STORE_BYTES:
            raise SandboxError(f"project store would exceed {MAX_STORE_BYTES} bytes")
        if usage["files"] + new_files > MAX_STORE_FILES:
            raise SandboxError(f"project store would exceed {MAX_STORE_FILES} files")

        for parts, data, executable in prepared:
            parent_fd, leaf = _open_parent(root_fd, parts)
            temp_name = ".dai-tmp-" + secrets.token_hex(12)
            temp_fd = -1
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
                with os.fdopen(temp_fd, "wb", closefd=True) as stream:
                    temp_fd = -1
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o755 if executable else 0o644)
                    os.fsync(stream.fileno())
                if overwrite:
                    os.replace(temp_name, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    action = "replaced"
                else:
                    os.link(temp_name, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                    os.unlink(temp_name, dir_fd=parent_fd)
                    action = "created"
                os.fsync(parent_fd)
                results.append(
                    {
                        "path": "/".join(parts),
                        "action": action,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "executable": executable,
                    }
                )
            except FileExistsError:
                raise SandboxError(f"file already exists (set overwrite=true to replace): {'/'.join(parts)}") from None
            finally:
                if temp_fd >= 0:
                    os.close(temp_fd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp_name, dir_fd=parent_fd)
                os.close(parent_fd)
    finally:
        os.close(root_fd)
    audit(
        "omega_write",
        {
            "project": name,
            "files": [
                {"path": x["path"], "bytes": x["bytes"], "sha256": x["sha256"], "executable": x["executable"]}
                for x in results
            ],
            "status": "written",
        },
    )
    return {"ok": True, "project": name, "root": str(root), "files": results}


def tree_digest(root: Path) -> dict[str, Any]:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("project root is not a real directory")
    digest = hashlib.sha256()
    count = 0
    total = 0
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        dirs.sort()
        names.sort()
        current_path = Path(current)
        for directory in list(dirs):
            p = current_path / directory
            st = p.lstat()
            if not stat.S_ISDIR(st.st_mode):
                raise SandboxError(f"symlink or special directory rejected: {p.relative_to(root)}")
            rel = p.relative_to(root).as_posix()
            digest.update(b"D\0" + rel.encode("utf-8") + b"\0")
        for filename in names:
            p = current_path / filename
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise SandboxError(f"symlink or special file rejected: {p.relative_to(root)}")
            count += 1
            total += st.st_size
            if count > MAX_TREE_FILES or total > MAX_TREE_BYTES:
                raise SandboxError("project exceeds file-count or 192 MiB safety limit")
            rel = p.relative_to(root).as_posix()
            digest.update(b"F\0" + rel.encode("utf-8") + b"\0" + str(st.st_mode & 0o777).encode() + b"\0")
            with p.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return {"sha256": digest.hexdigest(), "files": count, "bytes": total}


def validate_cwd(root: Path, cwd: Any) -> str:
    if cwd in (None, "", "."):
        return "."
    parts = safe_relpath(cwd)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise SandboxError("cwd must be an existing directory without symlinks")
    return "/".join(parts)


def audit(event: str, details: dict[str, Any]) -> None:
    ensure_state()
    record = {"ts": round(time.time(), 3), "event": event, **details}
    raw = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_token_file(path: Path, label: str) -> str:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise SandboxError(f"{label} token file must be a regular 0600 file") from None
    else:
        token = secrets.token_urlsafe(48)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return path.read_text(encoding="ascii").strip()


def ensure_api_token() -> str:
    ensure_layout()
    return _ensure_token_file(TOKEN_FILE, "API")


def ensure_sentinel_token() -> str:
    ensure_state()
    return _ensure_token_file(STATE / "sentinel.token", "SENTINEL")
