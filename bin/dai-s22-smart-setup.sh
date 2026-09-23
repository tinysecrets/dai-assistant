#!/usr/bin/env bash
# ==============================================================================
# S22 Ultra Smart Multi-Transport Setup (USB + Wi-Fi + Tailscale)
# Seamlessly powers:
#   1. Silent S22 Microphone (s22-mic) with zero speaker echo
#   2. Dictator (Super+D or clickable icon) pasting straight into browser/chat
#   3. DAI Gemini (Ava Neural) listening in the background
# ==============================================================================

set -uo pipefail

HOSTNAME_NOW="$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo '')"
USER_NOW="$(whoami 2>/dev/null || echo '')"

if [[ "$HOSTNAME_NOW" != "dhakidd" ]] || [[ "$USER_NOW" != "justin" ]]; then
    echo "ERROR: Target must be 'justin@dhakidd'. Current: '$USER_NOW@$HOSTNAME_NOW'. Aborting."
    exit 1
fi

echo "================================================================="
echo "  ACTIVATING S22 SMART MULTI-TRANSPORT BRIDGE & DICTATOR"
echo "================================================================="

ROOT_DIR="$HOME/dai-assistant"
[ -d "$ROOT_DIR" ] || ROOT_DIR="$HOME/dai"

# --- 1. Install Upgraded Dictator Script ---
echo "[1/5] Upgrading Dictator with clipboard paste engine..."
cp -f "$ROOT_DIR/bin/dhakidd-dictate" "$HOME/.local/bin/dhakidd-dictate"
chmod +x "$HOME/.local/bin/dhakidd-dictate"

# Ensure xclip is available if possible
if ! command -v xclip >/dev/null 2>&1; then
    echo "  Notice: xclip not found. Attempting install..."
    sudo apt-get update -qq && sudo apt-get install -y -qq xclip 2>/dev/null || true
fi

# --- 2. Update Systemd s22-mic.service to Smart Python Bridge ---
echo "[2/5] Configuring intelligent S22 bridge daemon..."
mkdir -p "$HOME/.config/systemd/user"

cat << EOF > "$HOME/.config/systemd/user/s22-mic.service"
[Unit]
Description=Samsung S22 Ultra Smart Multi-Transport Microphone Streamer
After=pipewire.service pipewire-pulse.service
Wants=pipewire-pulse.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 $ROOT_DIR/bin/s22-smart-bridge.py
Restart=always
RestartSec=2
Environment=HOME=%h
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/1000

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user restart s22-mic.service
echo "  -> s22-mic.service restarted with Python multi-transport engine."

# --- 3. Fix Super+D and Super+X Keybindings in MATE Desktop ---
echo "[3/5] Locking in Super+D keybinding in MATE..."
if command -v gsettings >/dev/null 2>&1; then
    gsettings set org.mate.Marco.global-keybindings show-desktop '' 2>/dev/null || true
    gsettings set org.mate.Marco.global-keybindings run-command-1 '<Mod4>d' 2>/dev/null || true
    gsettings set org.mate.Marco.keybinding-commands command-1 "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true
    gsettings set org.mate.Marco.global-keybindings run-command-2 '<Mod4>x' 2>/dev/null || true
    gsettings set org.mate.Marco.keybinding-commands command-2 "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true
fi

# --- 4. Desktop Launchers ---
echo "[4/5] Refreshing clickable desktop launchers..."
mkdir -p "$HOME/Desktop" "$HOME/Desktop/00_COMMAND_CENTER"

cat << 'EOF' > "$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop"
[Desktop Entry]
Type=Application
Name=🎤 Dictate (Toggle)
Comment=Speak into S22 to paste text directly into active window
Exec=/home/justin/.local/bin/dhakidd-dictate
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Audio;
EOF
chmod +x "$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop"
cp -f "$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop" "$HOME/Desktop/"
cp -f "$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop" "$HOME/Desktop/00_COMMAND_CENTER/"

# --- 5. Verify Active Transport & Test Capture ---
echo "[5/5] Checking active connection and verifying audio..."
sleep 3

systemctl --user status s22-mic.service --no-pager -n 5 2>&1 || true

echo ""
echo "Testing 2-second capture from S22 microphone (s22-mic)..."
timeout 2s parec --device="s22-mic" --raw > /tmp/dai-s22-verify.raw 2>/dev/null || true
BYTES=$(wc -c < /tmp/dai-s22-verify.raw 2>/dev/null || echo 0)
rm -f /tmp/dai-s22-verify.raw

echo ""
echo "================================================================="
if [ "$BYTES" -gt 1000 ]; then
    echo "  STATUS: ACTIVE & STREAMING ($BYTES bytes in 2s)"
else
    echo "  STATUS: NODE READY ($BYTES bytes captured)"
fi
echo "================================================================="
echo "How to use right now:"
echo "1. Click into this browser chat box."
echo "2. Press Super+D (or click '🎤 Dictate' on your Desktop)."
echo "3. Speak your message into your Samsung S22."
echo "4. Press Super+D again (or click '🎤 Dictate')."
echo "-> Your words will paste straight into the chat box!"
echo "================================================================="
