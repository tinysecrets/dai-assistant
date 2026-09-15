"""HTTP tests for the voice-bridge.

The bridge reads its configuration from the environment at import time (after
loading ``.env`` itself — see the ``DAI_PIPER_VOICE`` fix), so each case runs
``server.py`` as a subprocess on an ephemeral port with every path redirected
into a temp directory.  Nothing touches the network: the fake voice file in
``DAI_VOICE_MODELS_DIR`` keeps TTS from ever reaching the voice download, and
no STT request is made that would trigger a model fetch.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.support import ROOT, free_port

SERVER = ROOT / "services" / "voice-bridge" / "server.py"
FAKE_VOICE = "en_US-test-medium"


def sys_python() -> str:
    # The system interpreter, not the voice venv: the suite must not depend on
    # the optional venv being present (or absent).
    return sys.executable


def http(url, body=None, headers=None, timeout=10):
    method = "POST" if body is not None else "GET"
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _wait_healthy(base: str, proc, timeout: float = 10.0) -> None:
    """Wait for /health — but fail fast if our process died (a stale server
    on a just-freed port would answer otherwise, and the tests must hit the
    bridge *they* started)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"voice-bridge exited early (status {proc.returncode})")
        try:
            status, body, _h = http(f"{base}/health")
            if status == 200:
                # A stale server on a just-freed port would answer too; ours
                # is seconds old, not minutes.
                if json.loads(body).get("uptime_seconds", 999) < 5:
                    return
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError("voice-bridge never came up")


def _start_bridge(tmp: Path, env_overrides: dict):
    env = {
        **os.environ,
        "DAI_ENV": str(tmp / ".env"),  # absent file: fine
        "DAI_VOICE_BRIDGE_HOST": "127.0.0.1",
        "DAI_PIPER_VOICE": FAKE_VOICE,
        "DAI_VOICE_MODELS_DIR": str(tmp),
    }
    env.update(env_overrides)
    log = open(tmp / "bridge.log", "wb")
    # free_port() can hand back a port whose previous owner has not released
    # it yet; if the bind fails the server exits immediately, so retry with a
    # fresh port.
    last_err = None
    for _attempt in range(5):
        port = free_port()
        env["DAI_VOICE_BRIDGE_PORT"] = str(port)
        proc = subprocess.Popen(
            [sys_python(), str(SERVER)], env=env, stdout=log, stderr=subprocess.STDOUT
        )
        base = f"http://127.0.0.1:{port}"
        try:
            _wait_healthy(base, proc, timeout=5.0)
            return proc, log, base
        except RuntimeError as exc:
            last_err = exc
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            time.sleep(0.3)
    log.close()
    raise RuntimeError(f"voice-bridge never came up: {last_err}")


def _stop(proc, log):
    proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
    log.close()


class VoiceBridgeCase(unittest.TestCase):
    """A live bridge with a fake voice and no token."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dai-vb-"))
        (cls.tmp / f"{FAKE_VOICE}.onnx").write_bytes(b"not a real onnx model")
        (cls.tmp / f"{FAKE_VOICE}.onnx.json").write_text("{}")
        cls.proc, cls.log, cls.base = _start_bridge(cls.tmp, {})

    @classmethod
    def tearDownClass(cls):
        _stop(cls.proc, cls.log)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_health_reports_shape(self):
        status, body, _h = http(f"{self.base}/health")
        self.assertEqual(status, 200)
        doc = json.loads(body)
        for key in ("ok", "service", "version", "whisper_loaded", "voice_loaded", "ffmpeg", "formats"):
            self.assertIn(key, doc)
        self.assertEqual(doc["service"], "voice-bridge")
        self.assertEqual(doc["voice"], FAKE_VOICE)
        # Fresh process: both models are loaded lazily, so neither is up yet.
        self.assertFalse(doc["whisper_loaded"])
        self.assertFalse(doc["voice_loaded"])

    def test_speech_without_working_tts_is_a_clean_500(self):
        status, body, _h = http(
            f"{self.base}/v1/audio/speech",
            body=json.dumps({"input": "hello", "response_format": "wav"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 500)
        doc = json.loads(body)
        # The fake model file exists, so no download is attempted; loading it
        # fails (or piper is absent) and the bridge reports it in the envelope.
        self.assertEqual(doc["error"], "tts_load_failed")
        self.assertIn("detail", doc)

    def test_speech_missing_input(self):
        status, body, _h = http(
            f"{self.base}/v1/audio/speech",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "input_required")

    def test_speech_unknown_voice_rejected(self):
        status, body, _h = http(
            f"{self.base}/v1/audio/speech",
            body=json.dumps({"input": "hi", "voice": "zz_ZZ-nobody"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "unknown_voice")

    def test_speech_unsupported_format(self):
        status, body, _h = http(
            f"{self.base}/v1/audio/speech",
            body=json.dumps({"input": "hi", "response_format": "flac8"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "unsupported_format")

    def test_speech_malformed_body(self):
        status, body, _h = http(
            f"{self.base}/v1/audio/speech",
            body=b"{nope",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid_json")

    def test_transcription_requires_file(self):
        boundary = "dai-test-boundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\ntiny\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        status, resp, _h = http(
            f"{self.base}/v1/audio/transcriptions",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(resp)["error"], "file_required")

    def test_unknown_route_404s_as_json(self):
        status, body, _h = http(f"{self.base}/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "not_found")


class VoiceBridgeAuthCase(unittest.TestCase):
    """Same bridge with a bearer token: /health stays open, routes do not."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dai-vba-"))
        (cls.tmp / f"{FAKE_VOICE}.onnx").write_bytes(b"not a real onnx model")
        (cls.tmp / f"{FAKE_VOICE}.onnx.json").write_text("{}")
        cls.proc, cls.log, cls.base = _start_bridge(
            cls.tmp, {"DAI_VOICE_BRIDGE_TOKEN": "vb-test-token"}
        )

    @classmethod
    def tearDownClass(cls):
        _stop(cls.proc, cls.log)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_health_stays_open(self):
        status, _body, _h = http(f"{self.base}/health")
        self.assertEqual(status, 200)

    def test_speech_without_token_is_401(self):
        status, body, _h = http(
            f"{self.base}/v1/audio/speech",
            body=b'{"input": "hi"}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "unauthorized")

    def test_speech_with_token_passes_auth(self):
        status, body, _h = http(
            f"{self.base}/v1/audio/speech",
            body=b'{"input": "hi"}',
            headers={"Content-Type": "application/json", "Authorization": "Bearer vb-test-token"},
        )
        # Auth is over; the bridge then fails the same way the no-token case
        # does (no working TTS here) — which proves the token was accepted.
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "tts_load_failed")


class VoiceBridgeCheckCase(unittest.TestCase):
    """``--check`` must honour .env (the DAI_PIPER_VOICE bug), with real
    environment variables winning over the file."""

    def _check(self, env_overrides):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DAI_")}
        env.update(env_overrides)
        proc = subprocess.run(
            [sys_python(), str(SERVER), "--check"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_check_reads_env_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="dai-vbenv-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        env_file = tmp / ".env"
        env_file.write_text("DAI_PIPER_VOICE=en_GB-file-voice\n")
        doc = self._check({"DAI_ENV": str(env_file)})
        self.assertEqual(doc["voice"], "en_GB-file-voice")

    def test_check_real_env_wins_over_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="dai-vbenv-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        env_file = tmp / ".env"
        env_file.write_text("DAI_PIPER_VOICE=en_GB-file-voice\n")
        doc = self._check({"DAI_ENV": str(env_file), "DAI_PIPER_VOICE": "env-wins"})
        self.assertEqual(doc["voice"], "env-wins")

    def test_check_missing_env_file_not_fatal(self):
        doc = self._check({"DAI_ENV": "/nonexistent/.env"})
        self.assertTrue(doc["ok"])


if __name__ == "__main__":
    unittest.main()
