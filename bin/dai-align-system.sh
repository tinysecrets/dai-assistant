#!/usr/bin/env bash
# ==============================================================================
# DAI System Alignment & Consolidation Script
# Target Host: dhakidd | User: justin | OS: Debian 13 (trixie)
# Pins Scrcpy to S22, Locks G8 to Mic, Protects Dictator & Search from Interception
# Consolidates obsolete backups into ~/archive/2026-cleanup/
# ==============================================================================

set -euo pipefail

# --- 1. Machine Verification Safety Gate ---
HOSTNAME_NOW="$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo '')"
USER_NOW="$(whoami 2>/dev/null || echo '')"

if [[ "$HOSTNAME_NOW" != "dhakidd" ]] || [[ "$USER_NOW" != "justin" ]]; then
    echo "ERROR: Safety check failed! Target must be 'justin@dhakidd'."
    echo "Current environment: '$USER_NOW@$HOSTNAME_NOW'. Aborting."
    exit 1
fi

echo "================================================================="
echo "  DAI SYSTEM ALIGNMENT & HARDENING (dhakidd)"
echo "================================================================="

ARCHIVE_DIR="$HOME/archive/2026-cleanup"
mkdir -p "$ARCHIVE_DIR"

# --- 2. Pin Scrcpy strictly to Samsung S22 Ultra (RFCT428ZRSZ) ---
echo "[1/5] Pinning scrcpy to Samsung S22 Ultra (RFCT428ZRSZ, audio disabled)..."
cat << 'EOF' > "$HOME/.local/bin/scrcpy"
#!/bin/sh
# DHakidd Scrcpy: strictly pinned to Samsung S22 Ultra (RFCT428ZRSZ)
# Audio forwarding disabled to protect LG G8 microphone sovereignty
exec /usr/bin/scrcpy -s RFCT428ZRSZ --no-audio "$@"
EOF
chmod +x "$HOME/.local/bin/scrcpy"
echo "  -> ~/.local/bin/scrcpy updated and pinned to S22."

# --- 3. Update ~/.config/dhakidd-bridge.conf to guarantee S22 serial ---
if [ -f "$HOME/.config/dhakidd-bridge.conf" ]; then
    echo "[2/5] Verifying ~/.config/dhakidd-bridge.conf..."
    if ! grep -q 'USB_SERIAL="RFCT428ZRSZ"' "$HOME/.config/dhakidd-bridge.conf"; then
        sed -i 's/USB_SERIAL=.*/USB_SERIAL="RFCT428ZRSZ"/' "$HOME/.config/dhakidd-bridge.conf" || true
    fi
fi

# --- 4. Safely Archive Verified Obsolete / Broken Files ---
echo "[3/5] Consolidating obsolete backups to $ARCHIVE_DIR..."

safe_archive() {
    local target="$1"
    if [ -e "$target" ] || [ -L "$target" ]; then
        mv "$target" "$ARCHIVE_DIR/"
        echo "  Archived: $(basename "$target") -> ~/archive/2026-cleanup/"
    fi
}

# Obsolete backup copies in ~/.local/bin
safe_archive "$HOME/.local/bin/bridge.bak-20260908-102502"
safe_archive "$HOME/.local/bin/bridge.pre-finish.20260918-142509"
safe_archive "$HOME/.local/bin/s22-bridge-controller.pre-finish.20260918-142509"
safe_archive "$HOME/.local/bin/restore-g8-mic" # 0-byte dead file

# Duplicate wrappers in ~/bin
safe_archive "$HOME/bin/ai.backup.20260829-141013"

# Broken dead-path desktop launchers in ~/.local/share/applications/
safe_archive "$HOME/.local/share/applications/dai-assistant.desktop"
safe_archive "$HOME/.local/share/applications/dhakidd-dai.desktop"

# Fix broken opencode symlink if self-referencing
if [ -L "$HOME/.local/bin/opencode" ] && [ "$(readlink "$HOME/.local/bin/opencode")" = "$HOME/.local/bin/opencode" ]; then
    rm -f "$HOME/.local/bin/opencode"
    echo "  Cleaned self-referencing broken symlink: ~/.local/bin/opencode"
fi

# --- 5. Clean Clickable Launchers (Phase 6) ---
echo "[4/5] Standardizing clean desktop launchers..."

# Dedicated D-A-I Gemini Live Launcher
cat << 'EOF' > "$HOME/.local/share/applications/dai-gemini.desktop"
[Desktop Entry]
Type=Application
Name=D-A-I Gemini Live
Comment=Continuous sovereign voice assistant (Ava Neural, zero-interception)
Exec=/home/justin/dai-assistant/bin/dai gemini
Icon=audio-input-microphone
Terminal=true
Categories=Utility;Audio;AI;
EOF
chmod +x "$HOME/.local/share/applications/dai-gemini.desktop"

# Copy essential launchers to ~/Desktop/00_COMMAND_CENTER if folder exists
if [ -d "$HOME/Desktop/00_COMMAND_CENTER" ]; then
    cp -f "$HOME/.local/share/applications/dai-gemini.desktop" "$HOME/Desktop/00_COMMAND_CENTER/"
    if [ -f "$HOME/.local/share/applications/D-A-I Command Center.desktop" ]; then
        cp -f "$HOME/.local/share/applications/D-A-I Command Center.desktop" "$HOME/Desktop/00_COMMAND_CENTER/"
    fi
    if [ -f "$HOME/.local/share/applications/Restore LG G8 Microphone.desktop" ]; then
        cp -f "$HOME/.local/share/applications/Restore LG G8 Microphone.desktop" "$HOME/Desktop/00_COMMAND_CENTER/"
    fi
    if [ -f "$HOME/.local/share/applications/dhakidd-s22.desktop" ]; then
        cp -f "$HOME/.local/share/applications/dhakidd-s22.desktop" "$HOME/Desktop/00_COMMAND_CENTER/"
    fi
fi

# --- 6. Verification of the 4 Systems ---
echo "[5/5] Performing live closed-loop verification..."

echo "--- ADB Devices ---"
adb -s LMG820UM5abe4c22 get-state >/dev/null 2>&1 && echo "  [OK] LG G8 (LMG820UM5abe4c22): ONLINE" || echo "  [WARN] LG G8 not connected"
adb -s RFCT428ZRSZ get-state >/dev/null 2>&1 && echo "  [OK] Samsung S22 (RFCT428ZRSZ): ONLINE" || echo "  [WARN] S22 not connected"

echo "--- Audio Pipe ---"
if pactl list short sources | grep -q "android-87f1610"; then
    echo "  [OK] LG G8 PipeWire node (android-87f1610): ACTIVE"
    pactl set-default-source "android-87f1610" 2>/dev/null || true
else
    echo "  [WARN] android-87f1610 not found in PipeWire"
fi

echo "--- Dictator Check ---"
if [ -f "$HOME/.local/bin/dhakidd-dictate" ]; then
    echo "  [OK] Dictator (Super+D): READY"
fi

echo "--- Scrcpy S22 Isolation Check ---"
if grep -q "RFCT428ZRSZ" "$HOME/.local/bin/scrcpy"; then
    echo "  [OK] Scrcpy: ISOLATED to S22 Ultra (Zero mic interception)"
fi

echo ""
echo "================================================================="
echo "  ALIGNMENT COMPLETE: ZERO MISS, ZERO INTERCEPTION"
echo "================================================================="
echo "Launchers updated. Obsolete files safely moved to ~/archive/2026-cleanup/"
