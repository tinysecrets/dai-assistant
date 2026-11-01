#!/usr/bin/env python3
"""Bubblewrap runner: private namespaces, no network, ephemeral writable workspace."""

from __future__ import annotations

import contextlib
import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from common import SandboxError, project_path, tree_digest, validate_cwd

OUTPUT_LIMIT = 65_536
MAX_TIMEOUT = 300
WORKSPACE_BYTES = 268_435_456
TMP_BYTES = 134_217_728

_BOOTSTRAP = r"""
import os, shutil, sys
src, dst = "/input", "/workspace"
for current, dirs, files in os.walk(src, topdown=True, followlinks=False):
    for name in list(dirs) + list(files):
        path = os.path.join(current, name)
        if os.path.islink(path):
            raise SystemExit("SENTINEL: symlink in source tree refused")
shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
os.chdir(os.path.join(dst, sys.argv[1]) if sys.argv[1] != "." else dst)
os.execvp(sys.argv[2], sys.argv[2:])
"""


def _bwrap_base(project: Path) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SandboxError("bubblewrap (bwrap) is not installed")
    if not Path("/etc/ld.so.cache").is_file():
        raise SandboxError("host dynamic linker cache is unavailable")
    args = [
        bwrap,
        "--unshare-all",
        "--uid",
        "0",
        "--gid",
        "0",
        "--die-with-parent",
        "--new-session",
        "--tmpfs",
        "/",
        "--dir",
        "/usr",
        "--ro-bind",
        "/usr",
        "/usr",
        "--dir",
        "/lib",
        "--ro-bind",
        "/lib",
        "/lib",
        "--dir",
        "/lib64",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--dir",
        "/bin",
        "--ro-bind",
        "/bin",
        "/bin",
        "--dir",
        "/etc",
        "--ro-bind",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--dir",
        "/etc/alternatives",
        "--ro-bind",
        "/etc/alternatives",
        "/etc/alternatives",
        "--perms",
        "1777",
        "--size",
        str(TMP_BYTES),
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home",
        "--tmpfs",
        "/root",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/var",
        "--dir",
        "/tmp/home",
        "--dir",
        "/input",
        "--ro-bind",
        str(project),
        "/input",
        "--perms",
        "1777",
        "--size",
        str(WORKSPACE_BYTES),
        "--tmpfs",
        "/workspace",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp/home",
        "--setenv",
        "USER",
        "sandbox",
        "--setenv",
        "LOGNAME",
        "sandbox",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "XDG_CACHE_HOME",
        "/tmp/cache",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "PYTHONNOUSERSITE",
        "1",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--chdir",
        "/workspace",
    ]
    return args


def _bounded_process(argv: list[str], timeout_seconds: int) -> dict[str, Any]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    prlimit = shutil.which("prlimit")
    if prlimit:
        argv = [
            prlimit,
            f"--cpu={timeout_seconds + 5}:{timeout_seconds + 5}",
            "--as=1610612736",
            "--fsize=67108864",
            "--nofile=256:256",
            "--core=0",
            "--",
            *argv,
        ]
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return {
            "exit_code": 127,
            "timed_out": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout": "",
            "stderr": f"could not start sandbox: {type(exc).__name__}: {exc}",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    for label, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)

    deadline = started + timeout_seconds
    timed_out = False
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0 and proc.poll() is None:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            remaining = 0.1
        events = selector.select(min(0.1, max(0.01, remaining)))
        if not events and proc.poll() is not None:
            events = [(key, None) for key in list(selector.get_map().values())]
        for key, _ in events:
            label = key.data
            try:
                chunk = os.read(key.fileobj.fileno(), 8192)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            room = OUTPUT_LIMIT - len(buffers[label])
            if room > 0:
                buffers[label].extend(chunk[:room])
            if len(chunk) > room:
                truncated[label] = True
        if timed_out and proc.poll() is not None and not selector.get_map():
            break

    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    return {
        "exit_code": proc.returncode if proc.returncode is not None else 124,
        "timed_out": timed_out,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": buffers["stdout"].decode("utf-8", errors="replace"),
        "stderr": buffers["stderr"].decode("utf-8", errors="replace"),
        "stdout_truncated": truncated["stdout"],
        "stderr_truncated": truncated["stderr"],
    }


def validate_argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise SandboxError("argv must be a non-empty array of at most 64 strings")
    if any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 8192 for arg in value):
        raise SandboxError("argv contains an invalid argument")
    if sum(len(x.encode("utf-8")) for x in value) > 32_768:
        raise SandboxError("argv exceeds 32 KiB")
    executable = value[0]
    if not executable or executable.startswith("/") or "/" in executable or executable in {".", ".."}:
        raise SandboxError("executable must be a command name available inside the sandbox, not a host path")
    return value


def run_project(name: str, argv: Any, cwd: Any = ".", timeout_seconds: Any = 120) -> dict[str, Any]:
    project = project_path(name, create=False)
    before = tree_digest(project)
    command = validate_argv(argv)
    workdir = validate_cwd(project, cwd)
    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        raise SandboxError("timeout_seconds must be an integer") from None
    if timeout < 1 or timeout > MAX_TIMEOUT:
        raise SandboxError(f"timeout_seconds must be between 1 and {MAX_TIMEOUT}")

    wrapper = _bwrap_base(project) + [
        "--",
        "/usr/bin/python3",
        "-I",
        "-c",
        _BOOTSTRAP,
        workdir,
        *command,
    ]
    result = _bounded_process(wrapper, timeout)
    after = tree_digest(project)
    unchanged = before["sha256"] == after["sha256"]
    result.update(
        {
            "ok": result["exit_code"] == 0 and not result["timed_out"] and unchanged,
            "project": name,
            "argv": command,
            "cwd": workdir,
            "source_sha256_before": before["sha256"],
            "source_sha256_after": after["sha256"],
            "source_unchanged": unchanged,
            "workspace_mode": "ephemeral-tmpfs-copy",
            "network": "unshared",
            "timeout_seconds": timeout,
        }
    )
    if not unchanged:
        result["ok"] = False
        result["stderr"] += "\nSENTINEL: persistent project changed during sandbox run"
    return result
