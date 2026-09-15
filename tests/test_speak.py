"""Unit tests for lib/dai/speak.py (dai say / dai heartbeat).

No services, no network: the TTS path is tested against an in-process stub
that records the request, and the "bridge down" paths against a port nothing
listens on.  ``heartbeat_line`` is a pure function and is tested per state.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.dai.speak import heartbeat_line, speak  # noqa: E402

READY_ROUTER = {"ready_for_chat": True}
IDLE_ROUTER = {"ready_for_chat": False}
READY_WORKER = {"ready": True}
LOADED_VOICE = {"voice_loaded": True, "whisper_loaded": False}
COLD_VOICE = {"voice_loaded": False, "whisper_loaded": False}


class TestHeartbeatLine(unittest.TestCase):
    """The line is meant to be *heard*: first person, short, TTS-friendly."""

    def test_all_healthy_and_ready(self):
        line = heartbeat_line(READY_ROUTER, READY_WORKER, LOADED_VOICE)
        self.assertIn("D-A-I here", line)
        self.assertIn("ready for chat", line)
        self.assertIn("worker's on standby", line)
        self.assertIn("voice is warmed up", line)
        self.assertIn("Nothing needs you.", line)

    def test_up_but_not_ready_for_chat(self):
        line = heartbeat_line(IDLE_ROUTER, READY_WORKER, LOADED_VOICE)
        self.assertIn("can't chat yet", line)
        self.assertIn("add a key or start Ollama", line)

    def test_router_down(self):
        line = heartbeat_line(None, READY_WORKER, LOADED_VOICE)
        self.assertIn("router's down", line)
        self.assertIn("logs/", line)

    def test_worker_down(self):
        line = heartbeat_line(READY_ROUTER, None, LOADED_VOICE)
        self.assertIn("worker's down", line)
        # Something is down, so the "all clear" closing line must not appear.
        self.assertNotIn("Nothing needs you.", line)
        self.assertIn("logs/", line)

    def test_voice_cold(self):
        line = heartbeat_line(READY_ROUTER, READY_WORKER, COLD_VOICE)
        self.assertIn("not warmed up yet", line)

    def test_all_down_reports_from_command_line(self):
        line = heartbeat_line(None, None, None)
        self.assertIn("command line", line)
        self.assertIn("dai up", line)

    def test_no_em_dashes_and_sentence_case(self):
        # The line goes through a phonemizer; keep the punctuation plain.
        for args in [
            (READY_ROUTER, READY_WORKER, LOADED_VOICE),
            (IDLE_ROUTER, None, COLD_VOICE),
            (None, None, None),
        ]:
            line = heartbeat_line(*args)
            self.assertNotIn("\u2014", line)
            for sentence in line.split(". "):
                self.assertTrue(
                    sentence[0].isupper() or sentence[0] in "Dd",
                    f"sentence not capitalised: {sentence!r}",
                )


class _StubTTS(BaseHTTPRequestHandler):
    """Records the last request; replies 200 with fake bytes, or a canned error."""

    received: ClassVar[dict] = {}
    reply_status: ClassVar[int] = 200
    reply_error: ClassVar[object] = None
    reply_body = b"fake-mp3-bytes"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _StubTTS.received = {
            "path": self.path,
            "body": json.loads(body.decode("utf-8")),
            "auth": self.headers.get("Authorization"),
        }
        status = self.__class__.reply_status
        payload = (
            json.dumps({"error": self.__class__.reply_error, "detail": "stub"}).encode()
            if status != 200
            else self.__class__.reply_body
        )
        self.send_response(status)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class SpeakCase(unittest.TestCase):
    def setUp(self):
        # Reset the stub's class-level state: every test sees a fresh stub.
        _StubTTS.received = {}
        _StubTTS.reply_status = 200
        _StubTTS.reply_error = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubTTS)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-say-"))
        self.out = self.tmp / "speech.mp3"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_speech_request_shape_and_saved_file(self):
        _StubTTS.received = {}
        doc = speak("hello there", fmt="mp3", out=self.out, base_url=self.base)
        self.assertTrue(doc["ok"], doc)
        self.assertEqual(doc["line"], "hello there")
        self.assertEqual(doc["audio"], str(self.out))
        self.assertEqual(doc["bytes"], len(_StubTTS.reply_body))
        self.assertEqual(self.out.read_bytes(), _StubTTS.reply_body)
        self.assertEqual(_StubTTS.received["path"], "/v1/audio/speech")
        self.assertEqual(_StubTTS.received["body"]["input"], "hello there")
        self.assertEqual(_StubTTS.received["body"]["response_format"], "mp3")
        self.assertEqual(_StubTTS.received["body"]["voice"], "default")
        self.assertIsNone(_StubTTS.received["auth"])

    def test_token_sent_as_bearer(self):
        _StubTTS.received = {}
        # Set and restore: other test modules spawn services that inherit
        # os.environ, and a leaked token would turn their no-token bridges
        # into token-protected ones.
        saved = os.environ.get("DAI_VOICE_BRIDGE_TOKEN")
        os.environ["DAI_VOICE_BRIDGE_TOKEN"] = "sekrit-token"
        try:
            speak("hi", fmt="wav", out=self.out, base_url=self.base)
        finally:
            if saved is None:
                del os.environ["DAI_VOICE_BRIDGE_TOKEN"]
            else:
                os.environ["DAI_VOICE_BRIDGE_TOKEN"] = saved
        self.assertEqual(_StubTTS.received["auth"], "Bearer sekrit-token")

    def test_bridge_down_reports_cleanly(self):
        doc = speak("hi", fmt="wav", out=self.out, base_url="http://127.0.0.1:1")
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["error"], "voice_bridge_down")
        self.assertIn("voice-bridge", doc["detail"])

    def test_http_error_envelope_is_preserved(self):
        _StubTTS.reply_status = 400
        _StubTTS.reply_error = "unsupported_format"
        doc = speak("hi", fmt="wav", out=self.out, base_url=self.base)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["error"], "unsupported_format")
        self.assertFalse(self.out.exists())

    def test_bad_format_refused_without_network(self):
        doc = speak("hi", fmt="flac8", out=self.out, base_url=self.base)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["error"], "unsupported_format")
        self.assertEqual(_StubTTS.received, {})


if __name__ == "__main__":
    unittest.main()
