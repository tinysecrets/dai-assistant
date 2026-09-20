#!/usr/bin/env python3
"""Omni Daemon - runs healing loop and handles subprocess commands."""

import sys
import os
import time
import signal
import argparse
from pathlib import Path

# Add dai-assistant lib to path
DAI_ROOT = Path(os.environ.get("DAI_ROOT", Path.home() / "workspace/projects/active/dai-assistant")).resolve()
if str(DAI_ROOT) not in sys.path:
    sys.path.insert(0, str(DAI_ROOT))

from lib.dai.omni.healing import create_healer
from lib.dai.omni.memory import OmniMemory

def main():
    parser = argparse.ArgumentParser(prog="omni-daemon")
    parser.add_argument("--interval", type=int, default=60, help="Healing interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fixed")
    args = parser.parse_args()

    # Write PID file for external management
    pid_file = Path(os.environ.get("DAI_ROOT", Path.home() / "workspace/projects/active/dai-assistant")) / "state/omni" / "daemon.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    healer = create_healer(DAI_ROOT)
    memory = OmniMemory(DAI_ROOT)
    healer.set_memory(memory)

    def handler(signum, frame):
        print("\nReceived shutdown signal, stopping...")
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    if args.once:
        print("Running one healing pass...")
        actions = healer.run_once()
        print(f"Completed: {len(actions)} actions taken")
        return

    print(f"Starting omni daemon (healing every {args.interval}s)")
    try:
        while True:
            start = time.time()
            actions = healer.run_once()
            if actions:
                print(f"Healing pass: {len(actions)} actions taken")
            else:
                print("Healing pass: no issues detected")
            elapsed = time.time() - start
            sleep_time = max(0, args.interval - elapsed)
            time.sleep(sleep_time)
    except KeyboardInterrupt:
        print("\nDaemon stopped")

if __name__ == "__main__":
    main()