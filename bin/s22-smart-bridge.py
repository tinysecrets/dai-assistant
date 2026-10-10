#!/usr/bin/env python3
"""
S22 Ultra Intelligent Multi-Transport Audio & Connection Daemon
Automatically auto-discovers and connects Samsung S22 Ultra across:
  1. Direct USB Cable (RFCT428ZRSZ)
  2. Local Wi-Fi (Wireless ADB tcpip 5555)
  3. Tailscale Mesh Network (PHONE_TS_IP)

Streams S22 Ultra microphone silently into PipeWire virtual node 's22-mic'.
Guaranteed ZERO speaker echo (--no-playback).
Shares mic concurrently between Dictator (Super+D), DAI Gemini, and Browser.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

USB_SERIAL = "RFCT428ZRSZ"
PIPE_FILE = "/tmp/s22_mic_pipe"
CONF_FILE = os.path.expanduser("~/.config/dhakidd-bridge.conf")
CACHE_FILE = os.path.expanduser("~/.cache/dhakidd-bridge/s22-last-ip.txt")

_stop_event = False


def log(msg: str) -> None:
    print(f"[{time.strftime('%F %T')}] [S22 Smart Bridge]: {msg}", flush=True)


def get_configured_tailscale_ip() -> str | None:
    if os.path.exists(CONF_FILE):
        try:
            with open(CONF_FILE) as f:
                content = f.read()
            m = re.search(r'PHONE_TS_IP=["\']?([0-9.]+)', content)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    # Fallback to tailscale status
    if shutil.which("tailscale"):
        try:
            out = subprocess.check_output(
                ["tailscale", "status"], text=True, stderr=subprocess.DEVNULL
            )
            for line in out.splitlines():
                if any(k in line.lower() for k in ["s22", "galaxy", "phone"]):
                    parts = line.split()
                    if parts and re.match(r"^[0-9.]+$", parts[0]):
                        return parts[0]
        except Exception:
            pass
    return None


def get_cached_wifi_ip() -> str | None:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                ip = f.read().strip()
            if re.match(r"^[0-9.]+$", ip):
                return ip
        except Exception:
            pass
    return None


def cache_wifi_ip(ip: str) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            f.write(ip.strip())
    except Exception:
        pass


def setup_pipewire_source() -> None:
    """Setup silent PipeWire pipe-source node 's22-mic'."""
    # Clean up stale modules
    try:
        out = subprocess.check_output(["pactl", "list", "short", "modules"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "s22-mic" in line or "s22_mic" in line:
                mod_id = line.split()[0]
                subprocess.run(["pactl", "unload-module", mod_id], stderr=subprocess.DEVNULL)
    except Exception:
        pass

    try:
        if os.path.exists(PIPE_FILE):
            os.remove(PIPE_FILE)
        os.mkfifo(PIPE_FILE)
    except Exception as e:
        log(f"Notice preparing FIFO pipe: {e}")

    try:
        subprocess.run(
            [
                "pactl", "load-module", "module-pipe-source",
                "source_name=s22-mic", "channels=1", "format=s16le",
                "rate=48000", f"file={PIPE_FILE}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(["pactl", "set-source-mute", "s22-mic", "false"], stderr=subprocess.DEVNULL)
        subprocess.run(["pactl", "set-source-volume", "s22-mic", "100%"], stderr=subprocess.DEVNULL)
        subprocess.run(["pactl", "set-default-source", "s22-mic"], stderr=subprocess.DEVNULL)
        log("Silent PipeWire virtual mic 's22-mic' active.")
    except Exception as e:
        log(f"Error configuring PipeWire source: {e}")


def get_active_adb_device() -> str | None:
    """Detect S22 via USB, Tailscale, Wi-Fi, or auto-connect."""
    try:
        out = subprocess.check_output(["adb", "devices", "-l"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return None

    # 1. Prefer direct USB
    for line in out.splitlines():
        if USB_SERIAL in line and "device" in line and "unauthorized" not in line:
            # While connected via USB, cache phone's Wi-Fi IP and enable wireless port 5555
            try:
                ip_out = subprocess.check_output(
                    ["adb", "-s", USB_SERIAL, "shell", "ip", "-4", "addr", "show", "wlan0"],
                    text=True, stderr=subprocess.DEVNULL, timeout=2
                )
                m = re.search(r"inet\s+([0-9.]+)", ip_out)
                if m:
                    cache_wifi_ip(m.group(1))
                subprocess.run(["adb", "-s", USB_SERIAL, "tcpip", "5555"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            except Exception:
                pass
            return USB_SERIAL

    # 2. Check Tailscale IP
    ts_ip = get_configured_tailscale_ip()
    if ts_ip:
        for line in out.splitlines():
            if line.startswith(f"{ts_ip}:") and "device" in line:
                return line.split()[0]
        # Attempt auto-connect over Tailscale
        try:
            subprocess.run(["adb", "connect", f"{ts_ip}:5555"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            time.sleep(0.5)
            chk = subprocess.check_output(["adb", "devices"], text=True, stderr=subprocess.DEVNULL)
            for line in chk.splitlines():
                if line.startswith(f"{ts_ip}:") and "device" in line:
                    return line.split()[0]
        except Exception:
            pass

    # 3. Check Local Wi-Fi Cached IP
    wf_ip = get_cached_wifi_ip()
    if wf_ip and wf_ip != ts_ip:
        for line in out.splitlines():
            if line.startswith(f"{wf_ip}:") and "device" in line:
                return line.split()[0]
        try:
            subprocess.run(["adb", "connect", f"{wf_ip}:5555"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            time.sleep(0.5)
            chk = subprocess.check_output(["adb", "devices"], text=True, stderr=subprocess.DEVNULL)
            for line in chk.splitlines():
                if line.startswith(f"{wf_ip}:") and "device" in line:
                    return line.split()[0]
        except Exception:
            pass

    # 4. Any other non-G8 active device
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and "LMG820" not in line and parts[0] != "List":
            return parts[0]

    return None


def run_streamer_loop() -> None:
    setup_pipewire_source()

    # Keep a non-blocking open fd on FIFO pipe so PipeWire never sees EOF
    pipe_fd = os.open(PIPE_FILE, os.O_RDWR | os.O_NONBLOCK)

    log("Ready. Entering auto-discovery loop...")

    while not _stop_event:
        dev = get_active_adb_device()
        if not dev:
            time.sleep(2)
            continue

        transport = "USB" if dev == USB_SERIAL else ("Tailscale" if dev.startswith("100.") else "Wi-Fi Wireless")
        log(f"Locked onto Samsung S22 Ultra via [{transport}] ({dev}). Streaming mic silently...")

        cmd = [
            "scrcpy",
            "-s", dev,
            "--no-video",
            "--no-window",
            "--no-playback",             # CRITICAL: ZERO speaker playback / ZERO echo
            "--audio-source=mic",        # Microphone capture
            "--audio-codec=raw",
            "--record-format=wav",
            f"--record={PIPE_FILE}",
            "--audio-buffer=50",
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while proc.poll() is None and not _stop_event:
                time.sleep(1)
            if proc.poll() is not None:
                log("S22 stream connection changed. Auto-healing...")
        except Exception as e:
            log(f"Stream error: {e}")

        time.sleep(2)

    try:
        os.close(pipe_fd)
    except Exception:
        pass


def sig_handler(sig, frame):
    global _stop_event
    _stop_event = True
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    run_streamer_loop()
