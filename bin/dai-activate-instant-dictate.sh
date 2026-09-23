#!/usr/bin/env bash
# ==============================================================================
# Activate Instant S22 Dictator: Auto-Silence Detection (VAD) + F9 Hardware Key
# Single trigger: speak into S22, pauses automatically, pastes directly to chat.
# ZERO STOP BUTTON NEEDED. ZERO REPETITION LOOPS.
# ==============================================================================

set -uo pipefail

echo "================================================================="
echo "  ACTIVATING S22 INSTANT VAD DICTATOR (Auto-Stop on Silence)"
echo "================================================================="

ROOT_DIR="$HOME/dai-assistant"
[ -d "$ROOT_DIR" ] || ROOT_DIR="$HOME/dai"

# 1. Copy enhanced scripts
cp -f "$ROOT_DIR/bin/dhakidd-dictate" "$HOME/.local/bin/dhakidd-dictate"
chmod +x "$HOME/.local/bin/dhakidd-dictate"

# 2. Configure xbindkeys for 100% reliable F9 hardware key capture
if ! command -v xbindkeys >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq xbindkeys 2>/dev/null || true
fi

cat << 'EOF' > "$HOME/.xbindkeysrc"
# Dedicated F9 Instant Dictation Key
"/home/justin/.local/bin/dhakidd-dictate"
    F9

# Dedicated Pause Key
"/home/justin/.local/bin/dhakidd-dictate"
    Pause
EOF

# Restart xbindkeys cleanly
killall -q xbindkeys 2>/dev/null || true
nohup xbindkeys >/dev/null 2>&1 &

# 3. Refresh Desktop Icon and Mark Trusted
LAUNCHER_PATH="$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop"
cat << 'EOF' > "$LAUNCHER_PATH"
[Desktop Entry]
Type=Application
Name=🎤 Instant Dictate
Comment=Tap F9 or click here, speak into S22, and it pastes automatically
Exec=/home/justin/.local/bin/dhakidd-dictate
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Audio;
EOF
chmod +x "$LAUNCHER_PATH"
cp -f "$LAUNCHER_PATH" "$HOME/Desktop/🎤 Instant Dictate.desktop"
chmod +x "$HOME/Desktop/🎤 Instant Dictate.desktop"
gio set "$HOME/Desktop/🎤 Instant Dictate.desktop" metadata::trusted yes 2>/dev/null || true

# 4. Clean up any stuck locks or pidfiles
rm -f "$HOME/.local/state/dhakidd-dictate/recording.pid"

echo ""
echo "================================================================="
echo "  INSTANT AUTO-STOP DICTATOR IS ACTIVE!"
echo "================================================================="
echo "  1. Click your cursor inside the browser chat box."
echo "  2. Tap F9 ONCE (or double-click '🎤 Instant Dictate' on Desktop)."
echo "  3. Speak what you want to say into your Samsung S22."
echo "  4. STOP TALKING. (DO NOT PRESS ANY KEY TO STOP)."
echo "  -> The moment you pause for 0.8s, your words drop right into the box!"
echo "================================================================="
