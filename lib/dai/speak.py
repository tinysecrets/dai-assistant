"""Spoken output for the spine: free-form speech and "I'm active" check-ins.

Backs the ``dai say`` and ``dai heartbeat`` subcommands.  Stdlib only — the
heavy lifting (faster-whisper, piper) lives in the voice-bridge's venv; this
module just talks HTTP to the voice-bridge and composes the status lines the
assistant speaks.

Conventions match the rest of the tree:
  * usage problems exit 4 with a JSON ``usage_error`` document on stdout
    (a human copy on stderr), so every exit path is machine-parseable;
  * real failures exit 1 with the same JSON shape on stderr;
  * everything else (including "services are down") is a *result*, exit 0.
    A heartbeat whose job is to say "the router is down" has done its job.
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, request

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.dai.env import env_int, env_str, load_dotenv  # noqa: E402

EXIT_USAGE = 4

SPEECH_FORMATS = ("wav", "mp3", "ogg", "opus", "flac")

# Best-effort players, in preference order.  A headless box has none of them;
# the audio is saved and reported, never lost.
_PLAYERS = (
    "paplay",
    "aplay",
    "mpg123",
)

_stop = False


def _on_signal(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True


def usage_error(message: str) -> int:
    """Report a usage problem the way every other exit path reports a result."""
    print(json.dumps({"error": "usage_error", "detail": message}), file=sys.stdout)
    print(f"error: {message}", file=sys.stderr)
    return EXIT_USAGE


class UsageErrorParser(argparse.ArgumentParser):
    """``ArgumentParser`` honouring this module's exit-code contract (exit 4 + JSON)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        print(json.dumps({"error": "usage_error", "detail": message}), file=sys.stdout)
        raise SystemExit(EXIT_USAGE)


# --- URLs -------------------------------------------------------------------


def _env() -> None:
    # Real environment variables win; .env fills the gaps.  Never source it.
    load_dotenv(ROOT / ".env")


def router_url() -> str:
    return f"http://{env_str('DAI_ROUTER_HOST', '127.0.0.1')}:{env_int('DAI_ROUTER_PORT', 11435)}"


def worker_url() -> str:
    return f"http://{env_str('DAI_AGENT_S_HOST', '127.0.0.1')}:{env_int('DAI_AGENT_S_PORT', 8765)}"


def voice_url() -> str:
    return f"http://{env_str('DAI_VOICE_BRIDGE_HOST', '127.0.0.1')}:{env_int('DAI_VOICE_BRIDGE_PORT', 8766)}"


def _health(url: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """GET ``url/health``; the payload on 200, else None (down or unreachable)."""
    req = request.Request(f"{url}/health", headers={"Accept": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            doc = json.loads(resp.read().decode("utf-8", errors="replace"))
            return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


# --- speech -----------------------------------------------------------------


def _default_out(fmt: str) -> Path:
    out_dir = ROOT / "state" / "speech"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return out_dir / f"say-{stamp}-{uuid.uuid4().hex[:6]}.{fmt}"


def speak(
    text: str,
    *,
    fmt: str = "mp3",
    out: Optional[Path] = None,
    timeout: float = 300.0,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Speak ``text`` through the voice-bridge and save the audio.

    Returns ``{"ok": True, "line", "audio", "bytes"}`` on success, or
    ``{"ok": False, "error", "detail"}`` — never raises for expected failure
    modes (bridge down, refused format, encode error).
    """
    if fmt not in SPEECH_FORMATS:
        return {"ok": False, "error": "unsupported_format", "detail": f"use one of {list(SPEECH_FORMATS)}"}
    body = {"input": text, "voice": "default", "response_format": fmt}
    headers = {"Content-Type": "application/json"}
    token = env_str("DAI_VOICE_BRIDGE_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(
        (base_url or voice_url()) + "/v1/audio/speech",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except error.HTTPError as exc:
        try:
            doc = json.loads(exc.read().decode("utf-8", errors="replace"))
        except ValueError:
            doc = {}
        return {
            "ok": False,
            "error": str(doc.get("error") or "tts_failed"),
            "detail": str(doc.get("detail") or f"voice-bridge answered {exc.code}"),
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": "voice_bridge_down",
            "detail": f"voice-bridge not reachable at {base_url or voice_url()} — {exc}",
        }
    path = Path(out) if out is not None else _default_out(fmt)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        return {"ok": False, "error": "save_failed", "detail": f"could not write {path}: {exc}"}
    return {"ok": True, "line": text, "audio": str(path), "bytes": len(data)}


def play(path: Path) -> bool:
    """Best-effort local playback.  Returns True if a player ran and exited 0."""
    for name in _PLAYERS:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            proc = subprocess.run([exe, str(path)], capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return True
    return False


# --- the "I'm active" line ----------------------------------------------------


def heartbeat_line(
    router: Optional[Dict[str, Any]],
    worker: Optional[Dict[str, Any]],
    voice: Optional[Dict[str, Any]],
) -> str:
    """Compose the free-form, first-person status line the assistant speaks.

    ``None`` means that service is down or unreachable.  The line stays short
    (it is meant to be heard, not read), keeps punctuation TTS-friendly (no
    em dashes), and says exactly what matters: am I alive, can I think, can
    I speak.
    """
    if router is None and worker is None and voice is None:
        return (
            "D-A-I here, reporting from the command line: all three services are down. "
            "Run dai up to bring me back. This blip is all you'll get until then."
        )
    parts: List[str] = []
    if router is not None:
        if router.get("ready_for_chat") is True:
            parts.append("the router's ready for chat")
        else:
            parts.append("the router's up, but I can't chat yet: add a key or start Ollama")
    else:
        parts.append("the router's down")
    if worker is not None:
        if worker.get("ready") is True:
            parts.append("the worker's on standby")
        else:
            parts.append("the worker's up but not ready")
    else:
        parts.append("the worker's down")
    if voice is not None:
        if voice.get("voice_loaded"):
            parts.append("my voice is warmed up")
        else:
            parts.append("my voice is installed but not warmed up yet")
    if len(parts) == 1:
        state = parts[0]
    else:
        state = ", ".join(parts[:-1]) + ", and " + parts[-1]
    state = state[0].upper() + state[1:]
    opener = "D-A-I here, and I'm active."
    if router is None or worker is None:
        closer = "Check the logs in logs/ if you want the details."
    elif router is not None and router.get("ready_for_chat") is True:
        closer = "Nothing needs you."
    else:
        closer = "I'll be able to help as soon as a key lands."
    return f"{opener} {state}. {closer}"


# --- CLI ----------------------------------------------------------------------


def _emit(doc: Dict[str, Any], ok: bool) -> None:
    print(json.dumps(doc, indent=2))
    if not ok:
        print(f"error: {doc.get('error')}: {doc.get('detail', '')}", file=sys.stderr)


def cmd_say(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    if not text:
        return usage_error("say needs some text — dai say \"hello\"")
    doc = speak(text, fmt=args.format, out=Path(args.out) if args.out else None)
    if doc["ok"] and not args.no_play:
        doc["played"] = play(Path(doc["audio"]))
    _emit(doc, doc["ok"])
    return 0 if doc["ok"] else 1


def cmd_heartbeat(args: argparse.Namespace) -> int:
    while True:
        router = _health(router_url())
        worker = _health(worker_url())
        voice = _health(voice_url())
        line = heartbeat_line(router, worker, voice)
        if not args.json:
            print(line)
        doc: Dict[str, Any] = {
            "ok": True,
            "line": line,
            "router": router is not None,
            "worker": worker is not None,
            "voice": voice is not None,
            "ready_for_chat": bool(router and router.get("ready_for_chat")),
        }
        if args.speak:
            spoken = speak(line, fmt=args.format, out=Path(args.out) if args.out else None)
            if spoken["ok"]:
                if not args.no_play:
                    spoken["played"] = play(Path(spoken["audio"]))
                doc["audio"] = spoken["audio"]
            else:
                _emit({**spoken, "line": line}, False)
                return 1
        if args.json:
            print(json.dumps(doc, indent=2))
        if args.every <= 0 or _stop:
            return 0
        # Sleep in small slices so SIGTERM/SIGINT stop the loop promptly.
        slept = 0.0
        while slept < args.every and not _stop:
            time.sleep(min(0.5, args.every - slept))
            slept += 0.5


def build_parser() -> argparse.ArgumentParser:
    parser = UsageErrorParser(
        prog="dai say | dai heartbeat",
        description="Spoken output: free-form speech and 'I'm active' check-ins.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    say_p = sub.add_parser("say", help="speak arbitrary text through the voice-bridge")
    say_p.add_argument("text", nargs="*", help="the words to speak")
    say_p.add_argument("--format", default="mp3", choices=SPEECH_FORMATS, help="audio format (default mp3)")
    say_p.add_argument("--out", help="where to save the audio (default state/speech/say-<ts>.<fmt>)")
    say_p.add_argument("--no-play", action="store_true", help="save only; never try a local player")
    say_p.set_defaults(func=cmd_say)

    hb_p = sub.add_parser("heartbeat", help="say (and optionally speak) a free-form status line")
    hb_p.add_argument("--speak", action="store_true", help="speak the line through the voice-bridge")
    hb_p.add_argument("--every", type=float, default=0.0, metavar="SECONDS",
                      help="repeat every N seconds (0 = once; default)")
    hb_p.add_argument("--format", default="mp3", choices=SPEECH_FORMATS, help="audio format for --speak")
    hb_p.add_argument("--out", help="where to save each spoken blip")
    hb_p.add_argument("--no-play", action="store_true", help="with --speak: save only, never play")
    hb_p.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    hb_p.set_defaults(func=cmd_heartbeat)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _env()
    args = build_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
