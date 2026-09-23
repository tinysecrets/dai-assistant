#!/usr/bin/env bash
# ==============================================================================
# Add Clickable Microphone Button to MATE Panel & Register F9 / Ctrl+Alt+D Hotkeys
# ==============================================================================

set -uo pipefail

echo "================================================================="
echo "  CONFIGURING MICROPHONE BUTTON & FAST HOTKEYS (F9, Ctrl+Alt+D)"
echo "================================================================="

# 1. Update Dictator script path in ~/.local/bin
cp -f "$HOME/dai-assistant/bin/dhakidd-dictate" "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || \
cp -f "$HOME/dai/bin/dhakidd-dictate" "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true
chmod +x "$HOME/.local/bin/dhakidd-dictate"

# 2. Register F9, Ctrl+Alt+D, and Pause in MATE Settings Daemon (Media Keys)
if command -v gsettings >/dev/null 2>&1; then
    echo "[1/3] Binding F9 and Ctrl+Alt+D hotkeys..."
    
    # Marco Window Manager fallback keys
    gsettings set org.mate.Marco.global-keybindings run-command-1 'F9' 2>/dev/null || true
    gsettings set org.mate.Marco.keybinding-commands command-1 "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true

    gsettings set org.mate.Marco.global-keybindings run-command-2 '<Control><Alt>d' 2>/dev/null || true
    gsettings set org.mate.Marco.keybinding-commands command-2 "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true

    gsettings set org.mate.Marco.global-keybindings run-command-3 'Pause' 2>/dev/null || true
    gsettings set org.mate.Marco.keybinding-commands command-3 "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true

    # MATE Media-Keys plugin (System-wide listener)
    gsettings set org.mate.SettingsDaemon.plugins.media-keys custom-keybindings \
        "['/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom0/', '/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom1/', '/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom2/']" 2>/dev/null || true

    gsettings set org.mate.SettingsDaemon.plugins.media-keys.custom-keybinding:/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom0/ name 'Dictate-F9' 2>/dev/null || true
    gsettings set org.mate.SettingsDaemon.plugins.media-keys.custom-keybinding:/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom0/ command "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true
    gsettings set org.mate.SettingsDaemon.plugins.media-keys.custom-keybinding:/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom0/ binding 'F9' 2>/dev/null || true

    gsettings set org.mate.SettingsDaemon.plugins.media-keys.custom-keybinding:/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom1/ name 'Dictate-CtrlAltD' 2>/dev/null || true
    gsettings set org.mate.SettingsDaemon.plugins.media-keys.custom-keybinding:/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom1/ command "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true
    gsettings set org.mate.SettingsDaemon.plugins.media-keys.custom-keybinding:/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom1/ binding '<Primary><Alt>d' 2>/dev/null || true

    gsettings set org.mate.SettingsDaemon.plugins.media-keys.custom-keybinding:/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom2/ name 'Dictate-Pause' 2>/dev/null || true
    gsettings set org.mate.SettingsDaemon.plugins.media-keys.custom-keybinding:/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom2/ command "$HOME/.local/bin/dhakidd-dictate" 2>/dev/null || true
    gsettings set org.mate.SettingsDaemon.plugins.media-keys.custom-keybinding:/org/mate/settings-daemon/plugins/media-keys/custom-keybindings/custom2/ binding 'Pause' 2>/dev/null || true

    echo "  -> Hotkeys registered: Tap F9 or Ctrl+Alt+D to toggle dictation."
fi

# 3. Add Clickable Microphone Button to MATE Panel (Taskbar)
echo "[2/3] Adding clickable 🎤 Microphone Button to MATE top panel..."
LAUNCHER_PATH="$HOME/.local/share/applications/dhakidd-dictate-toggle.desktop"
cat << 'EOF' > "$LAUNCHER_PATH"
[Desktop Entry]
Type=Application
Name=🎤 Mic Dictate
Comment=Toggle speech-to-text dictation into active window
Exec=/home/justin/.local/bin/dhakidd-dictate
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Audio;
EOF
chmod +x "$LAUNCHER_PATH"

if command -v gsettings >/dev/null 2>&1; then
    current_objects=$(gsettings get org.mate.panel object-id-list 2>/dev/null || echo "[]")
    if ! echo "$current_objects" | grep -q "dictate_launcher"; then
        python3 - << 'PYEOF'
import subprocess, ast
try:
    out = subprocess.check_output(["gsettings", "get", "org.mate.panel", "object-id-list"], text=True).strip()
    objs = ast.literal_eval(out) if out.startswith("[") else []
    if "dictate_launcher" not in objs:
        objs.append("dictate_launcher")
        subprocess.run(["gsettings", "set", "org.mate.panel", "object-id-list", str(objs)], check=True)
except Exception:
    pass
PYEOF
        gsettings set org.mate.panel.object:/org/mate/panel/objects/dictate_launcher/ object-type 'launcher' 2>/dev/null || true
        gsettings set org.mate.panel.object:/org/mate/panel/objects/dictate_launcher/ toplevel-id 'top' 2>/dev/null || true
        gsettings set org.mate.panel.object:/org/mate/panel/objects/dictate_launcher/ position 12 2>/dev/null || true
        gsettings set org.mate.panel.object:/org/mate/panel/objects/dictate_launcher/ panel-right-stick false 2>/dev/null || true
        gsettings set org.mate.panel.object:/org/mate/panel/objects/dictate_launcher/ launcher-location "$LAUNCHER_PATH" 2>/dev/null || true
        echo "  -> Microphone button pinned directly to MATE panel taskbar!"
    fi
fi

# 4. Copy to Desktop
echo "[3/3] Placing 🎤 Mic Button on Desktop..."
cp -f "$LAUNCHER_PATH" "$HOME/Desktop/🎤 Dictate.desktop" 2>/dev/null || true
chmod +x "$HOME/Desktop/🎤 Dictate.desktop" 2>/dev/null || true

echo ""
echo "================================================================="
echo "  DONE! You now have 3 effortless ways to talk to this chat box:"
echo "  1. Tap F9 key (single keypress - no combo needed!)"
echo "  2. Tap Ctrl+Alt+D"
echo "  3. Click the 🎤 Microphone icon on your top panel taskbar"
echo "================================================================="
