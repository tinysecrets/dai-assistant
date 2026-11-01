# DAI Isolated Execution Sandbox

Version 1.0.0 — local implementation for OMEGA writes, isolated execution and SENTINEL verification on dhakidd.

## Components

- **Sandbox Worker** (sandbox.py): executes argv arrays inside Bubblewrap user, PID, IPC, UTS, mount and network namespaces. The host root is replaced by an ephemeral root; only /usr, required loader libraries and a minimal /etc/ld.so.cache are visible. The source project is mounted read-only at /input, copied into a size-limited tmpfs workspace, and commands execute there. The workspace disappears after each run.
- **OMEGA Write Route** (common.py + server.py): accepts UTF-8 source manifests and creates files under /data/dai-execution-sandbox/projects/<project>. It uses directory file descriptors, O_NOFOLLOW, exclusive temporary files and atomic per-file linking/replacement. It refuses traversal, symlinked parent directories, credential files, private-key extensions, .git, and live .env files.
- **SENTINEL** (sentinel.py + sentinel_server.py): a separate systemd user service and separate API token. It checks required files, hashes the source tree, runs each test command in a fresh isolated copy and returns a hard PASS or FAIL. Its systemd mount namespace makes the entire projects directory read-only. It cannot read OMEGA's API token; OMEGA cannot read SENTINEL's token.
- **OMEGA API** (server.py): loopback-only at 127.0.0.1:8790, bearer-authenticated except /health, no CORS, request-size limits and one active operation at a time.
- **SENTINEL API** (sentinel_server.py): loopback-only at 127.0.0.1:8791, its own bearer token, one active verification at a time, and read-only project access.
- **Client** (client.py): routes write/run requests to OMEGA and verification requests to the separate SENTINEL service.

## Security properties

- Bubblewrap is used instead of the root-managed Docker daemon. Neither service uses the Docker socket.
- Every execution gets private user/PID/IPC/UTS/mount/network namespaces (--unshare-all).
- The sandbox has no network, no host home, no host /run, no host /var, no host /data, no host /root, no host /tmp, no /proc mount (the sandbox cannot inspect host processes) and no host /sys view.
- Host runtime files are read-only. Project source is read-only; execution occurs on a 256 MiB tmpfs copy. /tmp is a separate 128 MiB tmpfs.
- The sandbox process runs as the invoking unprivileged user inside a user namespace; no sudo, root daemon or privileged container is used.
- CPU time, address space, individual file size, open descriptors, wall-clock duration, service memory, CPU and task count are bounded.
- The worker never writes its execution results back to the source tree. OMEGA's separate writer route is the only persistent source-write path.
- SENTINEL's systemd process cannot write to the projects directory. Its API token is separate and inaccessible to OMEGA; OMEGA's token is inaccessible to SENTINEL.
- Writes are limited to the sandbox projects root and use no-follow directory traversal. Existing files are not replaced unless overwrite=true is explicit.
- Audit records contain file paths, hashes and operation results, never source contents or bearer tokens.
- Tokens are generated locally and stored in state/api.token and state/sentinel.token, each mode 0600.

## Important limitations

This is a strong local Linux namespace sandbox for generated code, not a formally verified VM boundary. It shares the host kernel and Bubblewrap's security depends on the kernel's user-namespace implementation. Do not run deliberately hostile malware or expose either API beyond loopback. For hostile multi-tenant workloads, use a dedicated VM or a microVM runtime.

The worker currently uses the host's installed runtime tools (Python is verified); it does not download dependencies and has no network. A project that requires an unavailable language runtime or external package must fail until that runtime is deliberately added to the sandbox image. Each project is limited to 192 MiB and 10,000 files; each OMEGA write request is limited to 10 MiB and 500 files. The persistent store is capped at 10 GiB and 100,000 files by default. Set DAI_SANDBOX_MAX_STORE_BYTES to an explicit byte limit before starting the service to change the byte quota. There is no automatic project-retention cleanup; remove old mission projects deliberately after preserving any required artifacts. A manifest is preflighted for path and conflict errors, and each file is committed atomically, but a rare filesystem failure partway through a multi-file write can leave earlier files committed.

## API

OMEGA (127.0.0.1:8790):

- GET /health — unauthenticated readiness.
- GET /v1/projects — list isolated projects (OMEGA bearer token required).
- POST /v1/projects/<slug>/files — OMEGA write route:
  {"files":[{"path":"src/app.py","content":"print('hello')","executable":false},{"path":"bin/run.sh","content":"#!/bin/sh\\necho hello\\n","executable":true}],"overwrite":false}
- POST /v1/projects/<slug>/run — run an argv array in an ephemeral workspace:
  {"argv":["python3","-m","unittest","discover","-s","tests"],"cwd":".","timeout_seconds":120}

SENTINEL (127.0.0.1:8791):

- GET /health — unauthenticated readiness and read-only status.
- POST /v1/projects/<slug>/verify — independent verification:
  {"required_files":["src/app.py"],"tests":[["python3","-m","unittest","discover","-s","tests","-v"]],"timeout_seconds":120}

Both APIs require Authorization: Bearer <token> on every non-health route. There are no CORS headers. OMEGA and SENTINEL use different tokens.

## Install and operate

Both services are installed as user units and source their code from this DAI repository. From the repository root, run:

    ./bin/install-execution-sandbox.sh

The installer checks Bubblewrap and user namespaces, installs/reloads the unit files, enables both services, runs the isolation and client test suites, and verifies both loopback health endpoints.

Token files (owner-only):

- OMEGA: /data/dai-execution-sandbox/state/api.token
- SENTINEL: /data/dai-execution-sandbox/state/sentinel.token

    python3 /data/dai-execution-sandbox/client.py health
    python3 /data/dai-execution-sandbox/client.py sentinel-health
    python3 /data/dai-execution-sandbox/client.py projects

OMEGA write manifest example:

    {
      "files": [
        {"path": "src/hello.py", "content": "def hello():\n    return 'world'\n"},
        {"path": "tests/test_hello.py", "content": "import unittest\nfrom src.hello import hello\nclass TestHello(unittest.TestCase):\n    def test_hello(self): self.assertEqual(hello(), 'world')\n"}
      ]
    }

    python3 /data/dai-execution-sandbox/client.py write --project omega-demo --manifest /path/to/manifest.json
    python3 /data/dai-execution-sandbox/client.py run --project omega-demo --argv-json '["python3","-m","unittest","discover","-s","tests","-v"]'

SENTINEL test manifest is a JSON array of argv arrays, for example:

    [
      ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
    ]

    python3 /data/dai-execution-sandbox/client.py verify --project omega-demo --tests /path/to/tests.json --required-file src/hello.py --required-file tests/test_hello.py

A completed verification returns verdict PASS only when every test exits 0 before timeout, every required file exists, and the source tree remains unchanged. Any nonzero exit, timeout, missing file or source-tree change returns verdict FAIL and ok=false; the CLI exits 2.

## Project boundary and current integration status

OMEGA may write only inside /data/dai-execution-sandbox/projects/. It cannot nominate arbitrary host paths or modify $HOME/dai, $HOME/dai-core, .config/genie, phone integrations, system services or other existing repositories.

The authenticated loopback APIs are the integration interface for the DAI Mission Engine. The audited dai-core worker that was present on this workstation is a read-only diagnostic worker, not a code-writing mission executor; no existing OMEGA/SENTINEL client integration was found in the inspected DAI files. This implementation therefore exposes the real write/run/verify routes without silently rewriting or replacing the current mission engine.
