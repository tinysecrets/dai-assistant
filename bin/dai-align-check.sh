#!/usr/bin/env bash
# ==============================================================================
# DAI Alignment Check: Dictate, Scrcpy (S22), Browser Search, and DAI Voice
# Read-only audit of configuration, ADB multi-device isolation, and audio sharing
# ==============================================================================

set -uo pipefail

echo "================================================================="
echo "  1. ADB MULTI-DEVICE ISOLATION CHECK"
echo "================================================================="
adb devices -l

echo ""
echo "--- LG G8 (LMG820UM5abe4c22 - Microphone) Status ---"
if adb devices | grep -q "LMG820UM5abe4c22[[:space:]]\+device"; then
    echo ">> LG G8 is ONLINE and AUTHORIZED as device"
elif adb devices | grep -q "LMG820UM5abe4c22[[:space:]]\+unauthorized"; then
    echo ">> LG G8 is UNAUTHORIZED (Tap 'Allow USB Debugging' on phone screen)"
elif adb devices | grep -q "LMG820UM5abe4c22"; then
    echo ">> LG G8 detected in state: $(adb devices | grep "LMG820UM5abe4c22")"
else
    echo ">> LG G8 is NOT LISTED in adb devices"
fi

echo ""
echo "--- Samsung S22 (RFCT428ZRSZ - Phone/scrcpy) Status ---"
if adb devices | grep -q "RFCT428ZRSZ[[:space:]]\+device"; then
    echo ">> Samsung S22 is ONLINE and AUTHORIZED as device"
elif adb devices | grep -q "RFCT428ZRSZ"; then
    echo ">> Samsung S22 detected in state: $(adb devices | grep "RFCT428ZRSZ")"
else
    echo ">> Samsung S22 is NOT LISTED in adb devices"
fi

echo ""
echo "================================================================="
echo "  2. SCRCPY & S22 CONFIGURATION (Device Pinning)"
echo "================================================================="
echo "--- ~/.local/bin/scrcpy ---"
if [ -f "$HOME/.local/bin/scrcpy" ]; then
    cat "$HOME/.local/bin/scrcpy"
else
    echo "Not found"
fi

echo ""
echo "--- ~/.local/bin/s22-bridge-controller ---"
if [ -f "$HOME/.local/bin/s22-bridge-controller" ]; then
    cat "$HOME/.local/bin/s22-bridge-controller"
else
    echo "Not found"
fi

echo ""
echo "--- ~/.config/systemd/user/dhakidd-s22-bridge.service ---"
if [ -f "$HOME/.config/systemd/user/dhakidd-s22-bridge.service" ]; then
    cat "$HOME/.config/systemd/user/dhakidd-s22-bridge.service"
else
    echo "Not found"
fi

echo ""
echo "================================================================="
echo "  3. DICTATION TOOL CONFIGURATION (dhakidd-dictate)"
echo "================================================================="
if [ -f "$HOME/.local/bin/dhakidd-dictate" ]; then
    cat "$HOME/.local/bin/dhakidd-dictate"
else
    echo "Not found"
fi

echo ""
echo "================================================================="
echo "  4. BROWSER & SEARCH CONFIGURATION"
echo "================================================================="
echo "--- ~/.local/bin/dhakidd-three-browser ---"
if [ -f "$HOME/.local/bin/dhakidd-three-browser" ]; then
    cat "$HOME/.local/bin/dhakidd-three-browser"
else
    echo "Not found"
fi

echo ""
echo "--- ~/.local/share/applications/dhakidd-browser.desktop ---"
if [ -f "$HOME/.local/share/applications/dhakidd-browser.desktop" ]; then
    cat "$HOME/.local/share/applications/dhakidd-browser.desktop"
else
    echo "Not found"
fi

echo ""
echo "================================================================="
echo "  5. AUDIO ROUTING & ACTIVE CAPTURE CLIENTS"
echo "================================================================="
echo "Default Source: $(pactl get-default-source 2>/dev/null || echo 'unknown')"
echo ""
echo "--- Currently Active Recording Streams (source-outputs) ---"
pactl list source-outputs 2>/dev/null | grep -E 'Source Output|application.name|media.name|client' || echo "No active audio capture streams right now"

echo ""
echo "--- audiosource-g8 service process tree ---"
systemctl --user status audiosource-g8.service --no-pager -n 5 2>&1 || true
