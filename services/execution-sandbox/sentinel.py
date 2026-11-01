#!/usr/bin/env python3
"""Independent verifier: required-file checks plus fresh, read-only-source test runs."""

from __future__ import annotations

from typing import Any

from common import SandboxError, audit, project_path, safe_relpath, tree_digest
from sandbox import run_project, validate_argv


def verify_project(
    name: str,
    tests: Any,
    required_files: Any = None,
    timeout_seconds: Any = 120,
) -> dict[str, Any]:
    project = project_path(name, create=False)
    before = tree_digest(project)

    if not isinstance(tests, list) or not tests or len(tests) > 20:
        raise SandboxError("tests must contain 1-20 argv arrays")
    commands = [validate_argv(command) for command in tests]

    required = required_files if required_files is not None else []
    if not isinstance(required, list) or len(required) > 200:
        raise SandboxError("required_files must be an array of at most 200 paths")
    missing: list[str] = []
    for raw in required:
        parts = safe_relpath(raw)
        path = project.joinpath(*parts)
        if path.is_symlink() or not path.is_file():
            missing.append(raw)

    results: list[dict[str, Any]] = []
    for index, command in enumerate(commands, start=1):
        result = run_project(name, command, ".", timeout_seconds)
        result["test_index"] = index
        results.append(result)

    after = tree_digest(project)
    unchanged = before["sha256"] == after["sha256"]
    all_commands_passed = all(item.get("ok") is True for item in results)
    passed = not missing and all_commands_passed and unchanged
    report = {
        "ok": passed,
        "verdict": "PASS" if passed else "FAIL",
        "verifier": "SENTINEL",
        "project": name,
        "required_files": required,
        "missing_required_files": missing,
        "tests": results,
        "test_count": len(results),
        "passed_count": sum(1 for item in results if item.get("ok") is True),
        "source_sha256_before": before["sha256"],
        "source_sha256_after": after["sha256"],
        "source_unchanged": unchanged,
        "independent_read_only_source": True,
    }
    audit(
        "sentinel_verification",
        {
            "project": name,
            "verdict": report["verdict"],
            "test_count": report["test_count"],
            "passed_count": report["passed_count"],
            "missing_required_files": missing,
            "source_unchanged": unchanged,
        },
    )
    return report
