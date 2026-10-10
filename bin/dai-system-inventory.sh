#!/usr/bin/env bash
# ==============================================================================
# DAI System Inventory & Phase 1-4 Audit Script
# Read-only inspection of Debian Desktop (dhakidd)
# NO MODIFICATIONS, NO DELETIONS, NO RESTARTS, NO WRITES OUTSIDE LOG
# ==============================================================================

set -uo pipefail

OUT_FILE="${1:-$HOME/dai-system-inventory.txt}"
exec > >(tee "$OUT_FILE") 2>&1

section() {
    printf "\n=================================================================\n"
    printf "  %s\n" "$1"
    printf "=================================================================\n"
}

section "1. HOST & ENVIRONMENT IDENTIFICATION"
printf "Timestamp:      %s\n" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
printf "Hostname:       %s\n" "$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null)"
printf "Current User:   %s (UID: %s)\n" "$(whoami 2>/dev/null)" "$(id -u 2>/dev/null)"
printf "OS / Distro:    "
if [ -f /etc/os-release ]; then
    grep -E '^(PRETTY_NAME|NAME|VERSION)=' /etc/os-release | tr '\n' ' '
    echo ""
else
    uname -a
fi
printf "Kernel:         %s\n" "$(uname -r 2>/dev/null)"
printf "Desktop / DE:   XDG_CURRENT_DESKTOP='%s' DESKTOP_SESSION='%s' XDG_SESSION_TYPE='%s'\n" \
    "${XDG_CURRENT_DESKTOP:-unset}" "${DESKTOP_SESSION:-unset}" "${XDG_SESSION_TYPE:-unset}"
printf "Working Dir:    %s\n" "$(pwd)"

section "2. ANDROID DEVICE & USB HARDWARE MAP"
echo "--- ADB Devices ---"
adb devices -l 2>&1 || echo "ADB command not available or failed"

echo ""
echo "--- USB Bus Devices (lsusb) ---"
lsusb 2>&1 || echo "lsusb not available"

echo ""
echo "--- USB Kernel Messages (LG G8 / Samsung detection) ---"
dmesg 2>/dev/null | grep -iE 'usb|android|samsung|lge|lg' | tail -n 25 || echo "dmesg restricted or empty"

echo ""
echo "--- Device Role Verification ---"
echo "Target LG G8 serial:      LMG820UM5abe4c22 (Role: Remote Mic ONLY)"
echo "Target Samsung S22 serial: RFCT428ZRSZ      (Role: Phone / scrcpy / ADB ONLY)"
if command -v adb >/dev/null 2>&1 && adb devices | grep -q "LMG820UM5abe4c22"; then
    echo ">> LG G8 (LMG820UM5abe4c22): CONNECTED via ADB"
else
    echo ">> LG G8 (LMG820UM5abe4c22): NOT DETECTED via ADB (Physical connection or USB debug missing)"
fi
if command -v adb >/dev/null 2>&1 && adb devices | grep -q "RFCT428ZRSZ"; then
    echo ">> Samsung S22 (RFCT428ZRSZ): CONNECTED via ADB (Standalone phone, NOT a mic)"
fi

section "3. AUDIO HARDWARE & SOURCES (PHASE 4 AUDIT)"
echo "--- ALSA Sound Cards (/proc/asound/cards) ---"
cat /proc/asound/cards 2>/dev/null || echo "No ALSA cards file"

echo ""
echo "--- ALSA PCM Endpoints (/proc/asound/pcm) ---"
cat /proc/asound/pcm 2>/dev/null || echo "No ALSA pcm file"

echo ""
echo "--- ALSA Capture Devices (arecord -l) ---"
arecord -l 2>&1 || echo "arecord not available"

echo ""
echo "--- PipeWire / PulseAudio Server Info ---"
pactl info 2>&1 | grep -E 'Server Name|Server Version|Default Sink|Default Source' || echo "pactl info failed"

echo ""
echo "--- All Available Audio Sources (pactl list short sources) ---"
pactl list short sources 2>&1 || echo "pactl sources failed"

echo ""
echo "--- Detailed ALC671 Audio Endpoints ---"
pactl list sources 2>/dev/null | grep -E 'Name:|Description:|State:|Sample Specification:|Active Port:|Mute:|Volume:' || echo "No sources detail"

echo ""
echo "--- Bluetooth Audio Devices ---"
if command -v bluetoothctl >/dev/null 2>&1; then
    bluetoothctl devices 2>&1 || echo "No bluetooth devices found"
    bluetoothctl info 2>&1 || true
else
    echo "bluetoothctl not available"
fi

section "4. SYSTEMD USER SERVICES & STATE"
echo "--- Active User Services ---"
systemctl --user list-units --type=service --state=running --no-pager 2>&1 || echo "systemctl user failed"

echo ""
echo "--- Audio / Phone / DAI Unit Files ---"
systemctl --user list-unit-files --type=service --no-pager 2>&1 | grep -iE 'dai|g8|audio|scrcpy|deskflow|mic' || echo "No matching unit files"

echo ""
echo "--- audiosource-g8.service Definition & Status ---"
systemctl --user cat audiosource-g8.service 2>&1 || echo "audiosource-g8.service not found"
echo "Status:"
systemctl --user status audiosource-g8.service --no-pager -n 5 2>&1 || true

echo ""
echo "--- g8-mic-router.service Definition & Status ---"
systemctl --user cat g8-mic-router.service 2>&1 || echo "g8-mic-router.service not found"
echo "Status:"
systemctl --user status g8-mic-router.service --no-pager -n 5 2>&1 || true

echo ""
echo "--- DAI Spine / Assistant Service Definition & Status ---"
systemctl --user cat dai-spine.service 2>&1 || echo "dai-spine.service not found"
echo "Status:"
systemctl --user status dai-spine.service --no-pager -n 5 2>&1 || true

echo ""
echo "--- Deskflow Service Definition & Status ---"
systemctl --user list-units --type=service --no-pager | grep -i deskflow || echo "No active deskflow unit"
systemctl --user cat deskflow.service 2>&1 || true

section "5. DESKTOP LAUNCHERS & STARTUP APPLICATIONS"
echo "--- User Desktop Directory (~/Desktop) ---"
ls -la "$HOME/Desktop" 2>&1 || echo "No ~/Desktop"

echo ""
echo "--- Desktop Launchers Content ---"
for f in "$HOME/Desktop"/*.desktop; do
    if [ -f "$f" ]; then
        printf "\n== File: %s ==\n" "$f"
        cat "$f"
    fi
done

echo ""
echo "--- ~/.local/share/applications/ (Custom Apps) ---"
ls -la "$HOME/.local/share/applications" 2>&1 || echo "No ~/.local/share/applications"
for f in "$HOME/.local/share/applications"/*dai*.desktop "$HOME/.local/share/applications"/*g8*.desktop; do
    if [ -f "$f" ]; then
        printf "\n== File: %s ==\n" "$f"
        cat "$f"
    fi
done

echo ""
echo "--- Autostart Entries (~/.config/autostart) ---"
ls -la "$HOME/.config/autostart" 2>&1 || echo "No ~/.config/autostart"

section "6. SCRIPT INVENTORY & DUPLICATE ANALYSIS (PHASE 2)"
echo "--- Scripts in ~/.local/bin/ ---"
ls -la "$HOME/.local/bin" 2>&1 || echo "No ~/.local/bin"

printf "\n--- Content of ~/.local/bin/g8-mic-router ---\n"
if [ -f "$HOME/.local/bin/g8-mic-router" ]; then
    cat "$HOME/.local/bin/g8-mic-router"
else
    echo "File not found"
fi

printf "\n--- Scripts in ~/bin/ (if exists) ---\n"
ls -la "$HOME/bin" 2>&1 || echo "No ~/bin"

printf "\n--- audiosource Project Directory (~/workspace/audiosource) ---\n"
ls -la "$HOME/workspace/audiosource" 2>&1 || echo "No ~/workspace/audiosource"

printf "\n--- DAI Assistant Directory & Symlinks ---\n"
ls -ld "$HOME/dai" "$HOME/dai-assistant" 2>&1 || true

section "7. LIVE MICROPHONE CHAIN VERIFICATION (PHASE 3)"
printf "Default PulseAudio Source: %s\n" "$(pactl get-default-source 2>/dev/null || echo 'unknown')"

echo "Testing 2-second capture from android-87f1610 (LG G8 PipeWire node)..."
if pactl list short sources 2>/dev/null | grep -q "android-87f1610"; then
    timeout 2s parec --device="android-87f1610" --raw > /tmp/dai-inv-g8.raw 2>/dev/null || true
    G8_BYTES=$(wc -c < /tmp/dai-inv-g8.raw 2>/dev/null || echo 0)
    rm -f /tmp/dai-inv-g8.raw
    printf ">> android-87f1610 result: %s bytes captured in 2s\n" "$G8_BYTES"
else
    printf ">> android-87f1610: Node NOT PRESENT in PulseAudio/PipeWire\n"
fi

echo "Testing 2-second capture from default source..."
timeout 2s parec --raw > /tmp/dai-inv-def.raw 2>/dev/null || true
DEF_BYTES=$(wc -c < /tmp/dai-inv-def.raw 2>/dev/null || echo 0)
rm -f /tmp/dai-inv-def.raw
printf ">> Default source result: %s bytes captured in 2s\n" "$DEF_BYTES"

section "INVENTORY AUDIT COMPLETE"
printf "Inventory log written to: %s\n" "$OUT_FILE"
