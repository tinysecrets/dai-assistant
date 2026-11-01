---
name: omega-execution
description: OMEGA materializes generated code through its isolated writer, runs bounded builds, and hands the project identifier to the trusted coordinator for independent SENTINEL verification.
---

# OMEGA real execution contract

Code generation alone is not completion. OMEGA writes source files to the isolated project store and runs a real build in the disposable Bubblewrap sandbox. OMEGA does not verify or certify its own work.

## Python interface

    from lib.dai.execution import ExecutionClient

    omega = ExecutionClient("omega")

    omega.write_files("plant-watering", [
        {"path": "src/app.py", "content": "def due(days_since_watered, interval):\n    return days_since_watered >= interval\n"},
        {"path": "tests/test_execution_sandbox.py", "content": "import unittest\nfrom src.app import due\nclass DueTests(unittest.TestCase):\n    def test_due(self): self.assertTrue(due(7, 7))\n"},
        {"path": "README.md", "content": "# Plant watering tracker\n"},
    ])
    build = omega.run("plant-watering", ["python3", "-m", "compileall", "-q", "."])
    if not build.get("ok"):
        raise RuntimeError("build failed: " + str(build))

After the build, return the project slug and build result to the trusted mission coordinator. The coordinator dispatches the separate SENTINEL verification step.

## Required behavior

- Use one unique, safe project slug per mission. Existing files are not overwritten by default.
- Use only ExecutionClient("omega"). Do not instantiate the SENTINEL client, read its token, call its API or claim verification.
- OMEGA's credential may write files and request bounded builds; its service cannot read the SENTINEL token.
- Commands are argv arrays, never shell strings. Do not use shell=True.
- No network exists inside the Bubblewrap execution namespace. Do not assume dependency installation, downloads or remote package registries work.
- Prefer standard-library projects or dependencies already visible in the sandbox runtime.
- Include meaningful behavioral tests and a README. Syntax checks alone are not sufficient semantic verification.
- Persistent source is read-only during execution. Build writes are temporary and discarded.
- Report the project slug, files written, build exit code, timeouts and output. Do not report the mission complete until the trusted coordinator receives SENTINEL's separate PASS.
- Never read, print, forward, or log role tokens. Do not access host paths except through the OMEGA write API.
