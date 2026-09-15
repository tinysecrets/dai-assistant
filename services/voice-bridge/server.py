"""Local, free, offline voice for the spine — STT + TTS, no API keys.

OpenAI-compatible endpoints, proxied by the model-router:

    POST /v1/audio/transcriptions   multipart/form-data: file, model, language
    POST /v1/audio/speech           JSON: input, voice, response_format
    GET  /health                    status + loaded-model state
    GET  /version                   service identity

Speech-to-text uses faster-whisper (int8, CPU).  Text-to-speech uses piper
(ONNX, CPU).  Models download once into ~/.local/voice-models, then run fully
offline forever.

Run it with the voice venv's python (it is not on the system path):

    ~/.local/voice-venv/bin/python services/voice-bridge/server.py

``--check`` validates install and config without serving (used by doctor.sh,
which runs it under the system python), and ``--version`` prints the identity:
the same pair ``make check`` uses for every service.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib import parse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.dai import ServiceInfo
from lib.dai.env import load_dotenv

# The starter deliberately never sources .env (see bin/start-spine.sh); each
# service parses it itself.  voice-bridge reads its knobs at import time, so
# the file must be loaded here, before the first os.environ.get below — real
# environment variables still win (override=False).
load_dotenv(Path(os.environ.get("DAI_ENV", str(ROOT / ".env"))))

SERVICE = ServiceInfo("voice-bridge", "1.0", "docs/API.md")
VERSION = SERVICE.version

WHISPER_MODEL = os.environ.get("DAI_WHISPER_MODEL", "tiny")
PIPER_VOICE = os.environ.get("DAI_PIPER_VOICE", "en_US-lessac-medium")
VOICE_MODELS_DIR = Path(os.environ.get("DAI_VOICE_MODELS_DIR", str(Path.home() / ".local" / "voice-models")))
HOST = os.environ.get("DAI_VOICE_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("DAI_VOICE_BRIDGE_PORT", "8766"))
AUTH_TOKEN = os.environ.get("DAI_VOICE_BRIDGE_TOKEN", "")
QUIET = os.environ.get("DAI_QUIET_LOGS", "") not in ("", "0", "false")
MAX_BODY = int(os.environ.get("DAI_MAX_BODY_BYTES", str(8 * 1024 * 1024)))

FFMPEG = shutil.which("ffmpeg") or ""
_STARTED = time.time()

STT_LOCK = threading.Lock()
TTS_LOCK = threading.Lock()
VOICE_WORKERS = int(os.environ.get("DAI_VOICE_BRIDGE_WORKERS", "4"))
VOICE_SEM = threading.BoundedSemaphore(VOICE_WORKERS)
_stt: Dict[str, Optional[object]] = {"model": None, "error": None}
_tts: Dict[str, Tuple[object, bool]] = {}

SPEECH_FORMATS = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "flac": "audio/flac",
}


class VoiceError(Exception):
    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.detail = detail


def _load_stt() -> object:
    if _stt["model"] is None and _stt["error"] is None:
        try:
            whisper = importlib.import_module("faster_whisper")
            _stt["model"] = whisper.WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        except Exception as exc:
            _stt["error"] = str(exc)
            raise VoiceError(500, "stt_load_failed", f"Could not load whisper model '{WHISPER_MODEL}': {exc}") from None
    if _stt["error"]:
        raise VoiceError(500, "stt_load_failed", f"Could not load whisper model '{WHISPER_MODEL}': {_stt['error']}")
    return _stt["model"]


def _load_tts() -> object:
    cached = _tts.get(PIPER_VOICE)
    if cached is not None and cached[1]:
        return cached[0]
    model_path = VOICE_MODELS_DIR / f"{PIPER_VOICE}.onnx"
    config_path = VOICE_MODELS_DIR / f"{PIPER_VOICE}.onnx.json"
    if not model_path.exists():
        try:
            download_voice = importlib.import_module("piper.download_voices").download_voice
            VOICE_MODELS_DIR.mkdir(parents=True, exist_ok=True)
            download_voice(PIPER_VOICE, VOICE_MODELS_DIR)
        except Exception as exc:
            raise VoiceError(500, "voice_download_failed", f"Could not fetch voice '{PIPER_VOICE}': {exc}") from None
    try:
        piper = importlib.import_module("piper")
        obj = piper.PiperVoice.load(model_path, config_path)
    except Exception as exc:
        raise VoiceError(500, "tts_load_failed", f"Could not load piper voice '{PIPER_VOICE}': {exc}") from None
    _tts[PIPER_VOICE] = (obj, True)
    return obj


def _tts_wav(text: str) -> bytes:
    with TTS_LOCK:
        voice = _load_tts()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
    return buffer.getvalue()


def _to_requested_format(wav_bytes: bytes, fmt: str) -> Tuple[bytes, str]:
    if fmt == "wav":
        return wav_bytes, "audio/wav"
    if not FFMPEG:
        raise VoiceError(400, "format_unavailable", f"response_format '{fmt}' needs ffmpeg; use 'wav'.")
    if fmt == "opus":
        fmt_cli = "opus"
        content_type = "audio/ogg"
    else:
        fmt_cli = fmt
        content_type = SPEECH_FORMATS[fmt]
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "-", "-f", fmt_cli, "-"],
        input=wav_bytes,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise VoiceError(500, "encode_failed", f"ffmpeg could not encode '{fmt}': {proc.stderr.decode('utf-8', errors='replace')[:200]}")
    return proc.stdout, content_type


def _parse_multipart(content_type: str, body: bytes) -> Dict[str, object]:
    if "boundary=" not in content_type.lower():
        raise VoiceError(400, "bad_multipart", "Content-Type must be multipart/form-data with a boundary.")
    message = f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode("utf-8", errors="replace") + body
    parsed = BytesParser(policy=policy.default).parsebytes(message)
    fields: Dict[str, object] = {}
    for part in parsed.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if not name:
            if part.get_filename():
                name = "filename"
        if name not in fields:
            fields[name] = payload if name == "file" else (payload or b"").decode("utf-8", errors="replace").strip()
    return fields


def _deps_available() -> Dict[str, bool]:
    def has(mod: str) -> bool:
        try:
            return importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            return False

    return {"faster_whisper": has("faster_whisper"), "piper": has("piper")}


def check_configuration() -> Dict[str, object]:
    """Validate install and config without serving.  Used by ``--check`` and doctor.

    ``problems`` fail the check; ``warnings`` only inform.  Runtime deps
    (faster-whisper, piper) live in the optional voice venv, so their absence
    is a warning, not a fault — a system-python ``--check`` must still pass.
    (A malformed DAI_VOICE_BRIDGE_PORT or DAI_MAX_BODY_BYTES crashes at import,
    which doctor.sh already flags as a check that did not return JSON.)
    """
    warnings: List[str] = []
    problems: List[str] = []
    deps = _deps_available()
    if not deps["faster_whisper"]:
        warnings.append("faster-whisper not installed — STT will fail (pip install into the voice venv)")
    if not deps["piper"]:
        warnings.append("piper not installed — TTS will fail (pip install into the voice venv)")
    if not FFMPEG:
        warnings.append("ffmpeg not installed — only response_format=wav is available")
    if not VOICE_MODELS_DIR.exists():
        warnings.append(f"voice models dir {VOICE_MODELS_DIR} missing — models download on first use")
    if HOST not in ("127.0.0.1", "localhost") and not AUTH_TOKEN:
        warnings.append("bound to a non-loopback host without a bearer token — set DAI_VOICE_BRIDGE_TOKEN")
    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "service": SERVICE.name,
        "version": VERSION,
        "whisper_model": WHISPER_MODEL,
        "voice": PIPER_VOICE,
        "whisper_loaded": _stt["model"] is not None,
        "voice_loaded": bool(_tts),
        "whisper_available": bool(deps["faster_whisper"]),
        "piper_available": bool(deps["piper"]),
        "ffmpeg": bool(FFMPEG),
        "voice_models_dir": str(VOICE_MODELS_DIR),
        "speech_formats": list(SPEECH_FORMATS),
        "max_workers": VOICE_WORKERS,
        "cpu_affinity": os.environ.get("DAI_VOICE_BRIDGE_CPUS", "0-2"),
        "thread_hint": os.environ.get("DAI_VOICE_BRIDGE_THREADS", "4"),
    }


class VoiceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"dai-{SERVICE.name}/{VERSION}"
    sys_version = ""

    # --- plumbing ---------------------------------------------------------
    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise VoiceError(411, "length_required", "Send a Content-Length header.")
        try:
            length = int(raw_length)
        except ValueError:
            raise VoiceError(400, "bad_content_length", "Content-Length must be an integer.") from None
        if length < 0:
            raise VoiceError(400, "bad_content_length", "Content-Length must not be negative.")
        if length > MAX_BODY:
            self.close_connection = True
            raise VoiceError(413, "payload_too_large", f"Request body exceeds the {MAX_BODY} byte limit.")
        remaining = length
        chunks = bytearray()
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            chunks.extend(chunk)
            remaining -= len(chunk)
        return bytes(chunks)

    def _read_json(self) -> Dict[str, object]:
        raw = self._read_body()
        if not raw:
            raise VoiceError(400, "body_required", "A JSON request body is required.")
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise VoiceError(400, "invalid_json", f"Request body is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise VoiceError(400, "invalid_json", "Request body must be a JSON object.")
        return parsed

    def _check_auth(self) -> None:
        if not AUTH_TOKEN:
            return
        header = self.headers.get("Authorization") or ""
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
        if supplied != AUTH_TOKEN:
            raise VoiceError(401, "unauthorized", "This service requires a bearer token.")

    # --- responses --------------------------------------------------------
    def _respond(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response_only(code)
        self.send_header("Server", self.server_version)
        self.send_header("Date", self.date_time_string())
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close" if self.close_connection else "keep-alive")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _respond_json(self, code: int, payload: object) -> None:
        self._respond(code, json.dumps(payload, default=str).encode("utf-8"), "application/json")

    # --- endpoints --------------------------------------------------------
    def _health_payload(self) -> Dict[str, object]:
        return {
            "ok": True,
            "service": SERVICE.name,
            "version": VERSION,
            "whisper_model": WHISPER_MODEL,
            "whisper_loaded": _stt["model"] is not None,
            "voice": PIPER_VOICE,
            "voice_loaded": bool(_tts),
            "voice_models_dir": str(VOICE_MODELS_DIR),
            "ffmpeg": bool(FFMPEG),
            "formats": list(SPEECH_FORMATS),
            "max_workers": VOICE_WORKERS,
            "cpu_affinity": os.environ.get("DAI_VOICE_BRIDGE_CPUS", "0-2"),
            "thread_hint": os.environ.get("DAI_VOICE_BRIDGE_THREADS", "4"),
            "uptime_seconds": round(time.time() - _STARTED, 1),
        }

    def _transcribe(self) -> None:
        self._check_auth()
        content_type = self.headers.get("Content-Type") or ""
        body = self._read_body()
        fields = _parse_multipart(content_type, body)
        audio = fields.get("file")
        if not isinstance(audio, bytes) or not audio:
            raise VoiceError(400, "file_required", "Provide a 'file' part containing audio.")
        filename = str(fields.get("filename") or "audio.bin")
        suffix = Path(filename).suffix or ".wav"
        language = fields.get("language")
        with VOICE_SEM, STT_LOCK:
            model = _load_stt()
            tmp = tempfile.NamedTemporaryFile(prefix="stt-", suffix=suffix, delete=False)
            tmp_name = tmp.name
            try:
                tmp.write(audio)
                tmp.flush()
                del audio
                segments, _info = model.transcribe(
                    tmp_name,
                    language=language if isinstance(language, str) and language else None,
                    beam_size=5,
                    vad_filter=True,
                )
                text = "".join(seg.text for seg in segments).strip()
            finally:
                with contextlib.suppress(OSError):
                    tmp.close()
                    Path(tmp_name).unlink(missing_ok=True)
        self._respond_json(200, {"text": text})

    def _speech(self) -> None:
        self._check_auth()
        body = self._read_json()
        text = body.get("input")
        if not isinstance(text, str) or not text.strip():
            raise VoiceError(400, "input_required", "Provide an 'input' string to speak.")
        requested = body.get("voice")
        if requested is not None and str(requested) not in (PIPER_VOICE, "default"):
            raise VoiceError(400, "unknown_voice", f"Unknown voice '{requested}'; use '{PIPER_VOICE}'.")
        fmt = str(body.get("response_format") or "wav").lower()
        if fmt not in SPEECH_FORMATS:
            raise VoiceError(400, "unsupported_format", f"response_format '{fmt}'; try one of {sorted(SPEECH_FORMATS)}.")
        with VOICE_SEM:
            wav_bytes = _tts_wav(text.strip())
            data, content_type = _to_requested_format(wav_bytes, fmt)
        self._respond(200, data, content_type)

    # --- dispatch ---------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        parts = parse.urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        try:
            if method == "GET" and path == "/health":
                self._respond_json(200, self._health_payload())
                return
            if method == "GET" and path == "/version":
                self._respond_json(200, {**SERVICE.as_dict(), "python": sys.version.split()[0]})
                return
            if method == "POST" and path == "/v1/audio/transcriptions":
                self._transcribe()
                return
            if method == "POST" and path == "/v1/audio/speech":
                self._speech()
                return
            raise VoiceError(404, "not_found", f"No route for {method} {path}")
        except VoiceError as err:
            self._respond_json(err.status, {"error": err.error, "detail": err.detail})
        except Exception as exc:
            self.log_error("unhandled exception in %s %s: %s", method, path, str(exc))
            self._respond_json(500, {"error": "internal_error", "detail": "See the voice-bridge log."})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # --- logging ----------------------------------------------------------
    def log_message(self, fmt: str, *args: object) -> None:
        if QUIET and "ERROR" not in fmt.upper():
            return
        text = fmt % args if args else fmt
        stream = sys.stderr if fmt.startswith("ERROR") else sys.stdout
        try:
            stream.write(f"[{SERVICE.name}] {text}\n")
            stream.flush()
        except Exception:
            pass


def create_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, PORT), VoiceHandler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    return server


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="voice-bridge", description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{SERVICE.name} {VERSION}")
        return 0

    if args.check:
        report = check_configuration()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    server = create_server()
    stopping = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        print(f"received signal {signum}; shutting down", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    print(
        f"{SERVICE.name} {VERSION} listening on http://{HOST}:{PORT} "
        f"(whisper={WHISPER_MODEL}, voice={PIPER_VOICE}, ffmpeg={'yes' if FFMPEG else 'no'})",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
