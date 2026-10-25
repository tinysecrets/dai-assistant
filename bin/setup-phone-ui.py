#!/usr/bin/env python3
"""Create the private token and activate the per-user DAI phone UI service."""
import os
import secrets
import subprocess
from pathlib import Path

root = Path.home()
config = root / ".config" / "dai-phone-ui"
token_file = config / "token"
unit_file = root / ".config" / "systemd" / "user" / "dai-phone-ui.service"
app_file = root / "dai" / "services" / "phone-ui" / "server.py"
html_file = root / "dai" / "services" / "phone-ui" / "index.html"

for p in (app_file, html_file, unit_file):
    if not p.is_file():
        raise SystemExit(f"Missing required file: {p}")
config.mkdir(parents=True, exist_ok=True)
if not token_file.exists():
    token_file.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
os.chmod(config, 0o700)
os.chmod(token_file, 0o600)
os.chmod(app_file, 0o700)
subprocess.run(["/usr/bin/systemctl", "--user", "daemon-reload"], check=True)
subprocess.run(["/usr/bin/systemctl", "--user", "enable", "--now", "dai-phone-ui.service"], check=True)
print("DAI phone UI setup completed.")
print("URL: http://100.105.171.108:8767/")
print("Token stored with mode 600; it was NOT printed.")
print("To display it locally when ready to pair the S22, run: ~/dai/bin/show-phone-ui-token.sh")
print("Service status:")
subprocess.run(["/usr/bin/systemctl", "--user", "--no-pager", "--full", "status", "dai-phone-ui.service"], check=False)
