# DAI Isolated Execution Sandbox

The active OMEGA/SENTINEL execution path on dhakidd is Bubblewrap-based. It is separate from the model router, GUI worker, voice bridge and mobile microphone.

## Active components

- Service implementation: services/execution-sandbox/common.py, sandbox.py, server.py, sentinel.py and sentinel_server.py.
- Python client used by DAI: lib/dai/execution.py (ExecutionClient).
- OMEGA writer-only skill: skills/omega-execution/SKILL.md.
- SENTINEL verifier-only skill: skills/sentinel-verification/SKILL.md.
- CLI: **bin/dai-execution**
- Installer: **bin/install-execution-sandbox.sh**
- Persistent projects and role tokens: /data/dai-execution-sandbox/.

The systemd user units execute the service implementation directly from services/execution-sandbox/.

## Runtime and API

- OMEGA write/build API: http://127.0.0.1:8790
- SENTINEL verification API: http://127.0.0.1:8791
- Persistent project files: /data/dai-execution-sandbox/projects/<project>
- OMEGA token: /data/dai-execution-sandbox/state/api.token (0600)
- SENTINEL token: /data/dai-execution-sandbox/state/sentinel.token (0600)
- Audit log: /data/dai-execution-sandbox/state/audit.jsonl

OMEGA routes:

- GET /health — unauthenticated loopback readiness.
- GET /v1/projects — list project names, sizes and file counts (OMEGA token).
- POST /v1/projects/<project>/files — create source files (OMEGA token).
- POST /v1/projects/<project>/run — execute an argv array in a disposable workspace (OMEGA token).

SENTINEL routes:

- GET /health — unauthenticated loopback readiness.
- POST /v1/projects/<project>/verify — run independent tests and return PASS/FAIL (SENTINEL token).

There is no browser CORS surface. Both listeners bind only to 127.0.0.1. The APIs use separate bearer tokens; the OMEGA service cannot read the SENTINEL token and has no verification route, while SENTINEL cannot read the OMEGA token and has no write route. OMEGA’s skill is writer-only; the trusted mission coordinator hands the project slug to the separate SENTINEL skill. The token files are protected from the two service processes, but any unrelated process running as the same Unix account outside those systemd restrictions could read them; do not give OMEGA unrestricted host-shell access.

## Isolation boundary

Each build or verification run gets a fresh Bubblewrap environment:

- Private user, PID, IPC, UTS, mount and network namespaces.
- No network connectivity.
- Ephemeral root filesystem; only /usr, /lib, /lib64, /bin and a minimal dynamic-loader cache are exposed read-only.
- Project source is mounted read-only at /input and copied into a 256 MiB tmpfs workspace.
- /tmp is a separate 128 MiB tmpfs.
- No host home, /root, /run, /var, /data, /proc or /sys view.
- The sandbox runs as the invoking unprivileged user inside a user namespace. It does not use sudo, the Docker socket or a privileged container.
- Wall-clock timeout, CPU time, address-space, per-file size, output, descriptor, service memory, CPU and task limits apply.

The worker's execution workspace is ephemeral. Changes made by a build command do not alter persistent project source. OMEGA's authenticated write endpoint is the only normal persistent source-write route.

SENTINEL is a separate systemd user service. Its mount namespace makes the entire projects directory read-only. It has a separate token and cannot read the OMEGA token. OMEGA cannot read the SENTINEL token. Both services are explicitly denied access to the Docker socket. The systemd services retain AF_UNIX for Bubblewrap's internal IPC, while the Docker socket path is hidden with InaccessiblePaths.

## File and execution policy

- Project names are 1-64 character safe slugs.
- A write request accepts 1-500 UTF-8 text files, maximum 1 MiB each and 10 MiB total.
- Paths are relative. Traversal, symlinked parents, credential-like paths, private-key extensions, .git, .ssh, live .env files and special files are rejected.
- Existing files are not overwritten unless overwrite=true is explicit.
- Files are committed atomically one at a time; the whole multi-file manifest is preflighted for path and conflict errors.
- Projects are limited to 192 MiB and 10,000 files.
- Build/test commands are argv arrays, not raw shell command strings. The sandbox has no network and no host filesystem mounts.
- SENTINEL requires at least one test command. A PASS requires every command to exit 0 before timeout, required files to exist, and the source tree hash to remain unchanged.
- Audit records contain operation metadata and hashes, not source contents or tokens.

## OMEGA and SENTINEL workflow

OMEGA write manifest example:

    {
      "files": [
        {"path": "src/app.py", "content": "def answer():\n    return 42\n"},
        {"path": "tests/test_execution_sandbox.py", "content": "import unittest\nfrom src.app import answer\nclass TestApp(unittest.TestCase):\n    def test_answer(self): self.assertEqual(answer(), 42)\n"}
      ]
    }

SENTINEL test manifest:

    [
      ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
    ]

Trusted-coordinator full write/build/verify convenience (not an OMEGA-agent command):

    ./bin/dai-execution run \
      --project example-project \
      --source /path/to/generated/source \
      --tests-json /path/to/tests.json \
      --required-file src/app.py \
      --required-file tests/test_execution_sandbox.py

Separate operations:

    ./bin/dai-execution status
    ./bin/dai-execution projects
    ./bin/dai-execution write --project example-project --manifest /path/to/manifest.json
    ./bin/dai-execution build --project example-project
    ./bin/dai-execution verify --project example-project --tests-json /path/to/tests.json --required-file src/app.py

Trusted-coordinator Python interface (the OMEGA agent must use only the omega role; SENTINEL is invoked separately):

    from lib.dai.execution import ExecutionClient

    omega = ExecutionClient("omega")
    written = omega.write_files("example-project", [
        {"path": "src/app.py", "content": "def answer():\n    return 42\n"},
    ])

    sentinel = ExecutionClient("sentinel")
    report = sentinel.verify(
        "example-project",
        [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]],
        ["src/app.py", "tests/test_execution_sandbox.py"],
        120,
    )
    assert report.get("verdict") == "PASS" and report.get("ok") is True

## Install and service operations

    ./bin/install-execution-sandbox.sh
    systemctl --user status dai-execution-sandbox.service dai-sentinel.service --no-pager
    journalctl --user -u dai-execution-sandbox.service -u dai-sentinel.service -n 100 --no-pager

The installer runs the sandbox tests, validates the user units, checks user namespaces, installs separate role tokens, restarts both services and checks both loopback health endpoints.

## Limits and honest status

This is a strong local Linux namespace sandbox, not a formally verified VM boundary. It shares the host kernel. Do not run deliberately hostile malware or expose either API beyond loopback. Use a dedicated VM or microVM for hostile multi-tenant workloads.

The verified runtime profile is Python with the host's standard library. No package installation or internet access is available inside jobs. Other host tools may be invoked only if present in the read-only runtime; they are not certified language profiles. Each project has a 192 MiB limit, but there is not yet a global project-retention quota. A rare filesystem error during a multi-file write may leave earlier individually committed files in place.

The OMEGA and SENTINEL skills are role-separated and call the real APIs through separate credentials. The trusted coordinator may use the combined CLI for acceptance. This is an execution backend, not a new autonomous planner: the mission planner must select the OMEGA skill, hand off the project slug, and invoke the SENTINEL skill for its independent verdict. The active systemd units use the Bubblewrap implementation in services/execution-sandbox/. The duplicate runtime/ snapshot, older Docker-based prototype module, sandbox_core.py draft and older experimental CLI are not launched by the active units.
