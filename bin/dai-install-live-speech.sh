#!/usr/bin/env bash
# ==============================================================================
# S22 Ultra Real-Time Live Speech Typing Setup (Word-by-Word Streaming)
# Words pop up on screen live as you speak them.
# Designed for slow speech, pauses, and thinking time. NEVER cuts off mid-sentence.
# ==============================================================================

set -uo pipefail

echo "================================================================="
echo "  ACTIVATING S22 REAL-TIME LIVE VOICE TYPING (Word-by-Word)"
echo "================================================================="

ROOT_DIR="$HOME/dai-assistant"
[ -d "$ROOT_DIR" ] || ROOT_DIR="$HOME/dai"

# 1. Update Dictator to the Live Streaming Engine
cp -f "$ROOT_DIR/bin/s22-live-speech.py" "$HOME/.local/bin/dhakidd-dictate"
chmod +x "$HOME/.local/bin/dhakidd-dictate"

# 2. Wire F9 Hardware Key
if ! command -v xbindkeys >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq xbindkeys 2>/dev/null || true
fi

cat << 'EOF' > "$HOME/.xbindkeysrc"
# Dedicated F9 Live Voice Typing Toggle
"/home/justin/.local/bin/dhakidd-dictate"
    F9

# Dedicated Pause Key
"/home/justin/.local/bin/dhakidd-dictate"
    Pause
EOF

killall -q xbindkeys 2>/dev/null || true
nohup xbindkeys >/dev/null 2>&1 &

# 3. Create Desktop Launcher
LAUNCHER_PATH="$HOME/.local/share/applications/dhakidd-live-speech.desktop"
cat << 'EOF' > "$LAUNCHER_PATH"
[Desktop Entry]
Type=Application
Name=🎙️ Live Voice Typing
Comment=Words appear on screen live as you speak (never cuts you off)
Exec=/home/justin/.local/bin/dhakidd-dictate
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Audio;
EOF
chmod +x "$LAUNCHER_PATH"

cp -f "$LAUNCHER_PATH" "$HOME/Desktop/🎙️ Live Voice Typing.desktop"
chmod +x "$HOME/Desktop/🎙️ Live Voice Typing.desktop"
gio set "$HOME/Desktop/🎙️ Live Voice Typing.desktop" metadata::trusted yes 2>/dev/null || true

# 4. Clean up any stale state
rm -f "$HOME/.local/state/dhakidd-dictate/live_speech.pid" "$HOME/.local/state/dhakidd-dictate/recording.pid"

echo ""
echo "================================================================="
echo "  LIVE REAL-TIME STREAMING SPEECH IS READY!"
echo "================================================================="
echo "  How it works:"
echo "  1. Click your cursor inside this chat box."
echo "  2. Tap F9 (or click '🎙️ Live Voice Typing' on your Desktop)."
echo "  3. Speak at your own pace into your Samsung S22."
echo "     -> Every word appears on screen LIVE as you say it."
echo "     -> Take all the pauses you want; it will NEVER cut you off."
echo "  4. When you are completely finished, tap F9 to turn it off."
echo "================================================================="
