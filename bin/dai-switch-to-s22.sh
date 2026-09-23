#!/usr/bin/env bash
# ==============================================================================
# S22 Ultra Universal Microphone & Dictator Setup
# Retires LG G8, activates S22 self-healing mic (USB + Wi-Fi/Tailscale),
# fixes Super+D keybinding, and adds clickable Dictate launcher.
# ==============================================================================

set -uo pipefail

# 1. Target Machine Verification
HOSTNAME_NOW="$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo '')"
USER_NOW="$(whoami 2>/dev/null || echo '')"

if [[ "$HOSTNAME_NOW" != "dhakidd" ]] || [[ "$USER_NOW" != "justin" ]]; then
    echo "ERROR: Target must be 'justin@dhakidd'. Current: '$USER_NOW@$HOSTNAME_NOW'. Aborting."
    exit 1
fi

echo "================================================================="
echo "  ACTIVATING S22 ULTRA AS UNIVERSAL SELF-HEALING MICROPHONE"
echo "================================================================="

ROOT_DIR="$HOME/dai-assistant"
[ -d "$ROOT_DIR" ] || ROOT_DIR="$HOME/dai"

# --- 2. Retire LG G8 Mic Services & Cleanup ---
echo "[1/5] Retiring old LG G8 services..."
systemctl --user stop audiosource-g8.service g8-mic-router.service 2>/dev/null || true
systemctl --user disable audiosource-g8.service g8-mic-router.service 2>/dev/null || true

# Unload any stale LG G8 pulse modules
for id in $(pactl list short modules 2>/dev/null | grep -E 'android-87f1610' | awk '{print $1}'); do
    pactl unload-module "$id" 2>/dev/null || true
done

# Kill any stuck ffmpeg dictation processes and reset lock
killall -q ffmpeg 2>/dev/null || true
rm -f "$HOME/.local/state/dhakidd-dictate/recording.pid" "$HOME/.local/state/dhakidd-dictate/lock"

# --- 3. Install & Start s22-mic.service ---
echo "[2/5] Installing persistent S22 mic service (s22-mic.service)..."
mkdir -p "$HOME/.config/systemd/user"

cat << EOF > "$HOME/.config/systemd/user/s22-mic.service"
[Unit]
Description=Samsung S22 Ultra Universal Microphone Streamer
After=pipewire.service pipewire-pulse.service
Wants=pipewire-pulse.service

[Service]
Type=simple
ExecStart=$ROOT_DIR/bin/s22-mic-streamer
Restart=always
RestartSec=2
Environment=HOME=%h
Environment=DISPLAY=:0

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now s22-mic.service
echo "  -> s22-mic.service installed and started."

# --- 4. Fix Super+D Dictation Keybinding in MATE ---
echo "[3/5] Fixing Super+D and Super+X dictation keybindings in MATE..."
if command -v gsettings >/dev/null 2>&1; then
    # Disable default 'show-desktop' taking over Super+D
    gsettings set org.mate.Marco.global-keybindings show-desktop '' 2>/dev/null || true

    # Map Super+D (Mod4+d) to dhakidd-dictate
    gsettings set org.mate.Marco.global-keybindings run-command-1 '<Mod4>d' 2>/dev/null || true
    gsettings set org.mate.Marco.keybinding-commands command-1 "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true

    # Also map Super+X (Mod4+x) as backup instant hotkey
    gsettings set org.mate.Marco.global-keybindings run-command-2 '<Mod4>x' 2>/dev/null || true
    gsettings set org.mate.Marco.keybinding-commands command-2 "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true
    echo "  -> Keybindings updated: Super+D and Super+X both toggle Dictation."
fi

# --- 5. Clickable Desktop Launchers (Phase 6) ---
echo "[4/5] Creating clean clickable launchers on Desktop..."
mkdir -p "$HOME/.local/share/applications" "$HOME/Desktop" "$HOME/Desktop/00_COMMAND_CENTER"

# Clickable Dictate Launcher (No keyboard needed)
cat << 'EOF' > "$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop"
[Desktop Entry]
Type=Application
Name=🎤 Dictate (Toggle)
Comment=Toggle Whisper speech-to-text dictation into active window
Exec=/home/justin/.local/bin/dhakidd-dictate
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Audio;
EOF
chmod +x "$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop"
cp -f "$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop" "$HOME/Desktop/00_COMMAND_CENTER/"
cp -f "$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop" "$HOME/Desktop/"

# DAI Gemini Live Launcher
cat << 'EOF' > "$HOME/.local/share/applications/dai-gemini.desktop"
[Desktop Entry]
Type=Application
Name=D-A-I Gemini Live
Comment=Continuous sovereign voice assistant (Ava Neural, S22 mic)
Exec=/home/justin/dai-assistant/bin/dai gemini
Icon=applications-multimedia
Terminal=true
Categories=Utility;Audio;AI;
EOF
chmod +x "$HOME/.local/share/applications/dai-gemini.desktop"
cp -f "$HOME/.local/share/applications/dai-gemini.desktop" "$HOME/Desktop/00_COMMAND_CENTER/"
cp -f "$HOME/.local/share/applications/dai-gemini.desktop" "$HOME/Desktop/"

# --- 6. Live Test S22 Microphone Stream ---
echo "[5/5] Waiting 3 seconds for S22 audio stream to lock in..."
sleep 3

systemctl --user status s22-mic.service --no-pager -n 4 2>&1 || true

echo ""
echo "Testing 2-second capture from S22 microphone (s22-mic)..."
timeout 2s parec --device="s22-mic" --raw > /tmp/dai-s22-test.raw 2>/dev/null || true
BYTES=$(wc -c < /tmp/dai-s22-test.raw 2>/dev/null || echo 0)
rm -f /tmp/dai-s22-test.raw

echo ""
if [ "$BYTES" -gt 1000 ]; then
    echo "================================================================="
    echo "  SUCCESS: S22 MIC IS CAPTURING LIVE ($BYTES bytes in 2s)!"
    echo "================================================================="
    echo "  1. S22 Ultra is now the system microphone for EVERYTHING."
    echo "  2. Super+D (or clicking '🎤 Dictate') types your voice into any window."
    echo "  3. DAI Assistant (./bin/dai gemini) listens through S22 mic."
    echo "================================================================="
else
    echo "================================================================="
    echo "  S22 MIC NODE ACTIVE (Waiting for audio stream)"
    echo "================================================================="
    echo "  Check phone screen: if an ADB or Audio permission prompt appeared, tap ALLOW."
    echo "================================================================="
fi
