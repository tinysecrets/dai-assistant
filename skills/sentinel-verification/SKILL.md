---
name: sentinel-verification
description: SENTINEL independently verifies OMEGA project artifacts through its read-only, separately authenticated service and returns a hard PASS or FAIL.
---

# SENTINEL independent verification contract

SENTINEL is a separate verifier, not a second OMEGA writer. Its systemd service mounts the persistent projects directory read-only and cannot read the OMEGA API token. It has no write route.

## Python interface

    from lib.dai.execution import ExecutionClient

    sentinel = ExecutionClient("sentinel")
    report = sentinel.verify(
        "plant-watering",
        [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]],
        required_files=["src/app.py", "tests/test_execution_sandbox.py", "README.md"],
        timeout_seconds=120,
    )
    if report.get("verdict") != "PASS" or report.get("ok") is not True:
        raise RuntimeError("SENTINEL FAIL: " + str(report))

## Required behavior

- Verify only the exact project slug handed off by the trusted mission coordinator.
- Use only ExecutionClient("sentinel"). Never read or use the OMEGA token.
- Require non-empty tests and list every required source/test file.
- A PASS requires every test command to exit zero before timeout, every required file to exist, and the persistent source-tree digest to remain unchanged.
- A non-zero exit, timeout, missing required file, unsafe symlink/special file, or source digest change is FAIL.
- Return the full test output, exit codes, timeout status and verdict to the trusted coordinator.
- Never modify project source. The service's read-only mount is enforced by systemd as well as the Bubblewrap worker.
