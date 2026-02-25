#!/usr/bin/env bash
# switch_profile.sh — Switch device profile from Mac (Luke's Mirage)
#
# Usage:
#   bash switch_profile.sh                 # List available profiles
#   bash switch_profile.sh google-pixel7   # Switch to Pixel 7 profile
#   bash switch_profile.sh --random        # Random profile
#   bash switch_profile.sh --current       # Show current profile
#
# After switching, the script:
#   1. Copies the new profile to active.conf
#   2. Re-runs post-fs-data.sh + service.sh to hot-reload props
#   3. Copies new active.conf to /data/local/tmp/ for apps
#   4. Optionally restarts zygote so running apps pick up changes
#
# NOTE: Already-running apps keep the OLD profile until they restart.
#       Use --restart-zygote to force-restart all apps (soft reboot).

set -euo pipefail

ADB="/Applications/BlueStacks.app/Contents/MacOS/hd-adb"
MODDIR="/data/adb/modules/jorkspoofer"

# ── Helpers ──
adb_shell() { "$ADB" shell "$@" 2>/dev/null; }
adb_su()    { "$ADB" shell "su -c '$*'" 2>/dev/null; }

red()    { printf '\033[1;31m%s\033[0m' "$*"; }
green()  { printf '\033[1;32m%s\033[0m' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m' "$*"; }
cyan()   { printf '\033[1;36m%s\033[0m' "$*"; }
bold()   { printf '\033[1m%s\033[0m' "$*"; }

# ── Check ADB ──
if ! "$ADB" devices 2>/dev/null | grep -q 'device$'; then
    echo "$(red ERROR): BlueStacks not connected via ADB"
    exit 1
fi

# ── Extract quoted value from PROFILE_KEY="value" line (reads from stdin) ──
extract_val() { sed -n 's/.*="\{0,1\}\([^"]*\)"\{0,1\}/\1/p' | head -1 | tr -d '\r'; }

# ── List available profiles ──
list_profiles() {
    echo ""
    echo "$(bold 'Available Device Profiles:')"
    echo "$(cyan '────────────────────────────────────────────────')"

    # Get current profile name from on-device script (format: "device|Name")
    local current_name
    current_name=$(adb_su "sh /data/adb/jorkspoofer-switch.sh current" | tr -d '\r' | cut -d'|' -f2)

    local profiles
    profiles=$(adb_su "sh /data/adb/jorkspoofer-switch.sh list" | tr -d '\r')

    local i=1
    while IFS='|' read -r fname pname; do
        [ -z "$fname" ] && continue
        if [ "$pname" = "$current_name" ]; then
            printf "  $(green '>') %-3s $(green '%-30s') %s\n" "$i." "$fname" "<- active"
        else
            printf "    %-3s $(cyan '%-30s') %s\n" "$i." "$fname" "$pname"
        fi
        i=$((i + 1))
    done <<< "$profiles"

    echo ""
    echo "$(bold 'Usage:')  bash switch_profile.sh <profile-name>"
    echo "$(bold 'Example:') bash switch_profile.sh google-pixel7"
    echo ""
}

# ── Show current profile ──
show_current() {
    local name
    name=$(adb_su "grep PROFILE_NAME $MODDIR/profiles/active.conf" | extract_val)
    local device
    device=$(adb_su "grep PROFILE_DEVICE $MODDIR/profiles/active.conf" | extract_val)
    local fp
    fp=$(adb_su "grep PROFILE_BUILD_FINGERPRINT $MODDIR/profiles/active.conf" | extract_val)

    echo ""
    echo "$(bold 'Current Profile:')"
    echo "  Name:        $(green "$name")"
    echo "  Device:      $(cyan "$device")"
    echo "  Fingerprint: $fp"
    echo ""
}

# ── Hot-reload profile (no reboot needed) ──
hot_reload() {
    local profile_name="$1"

    echo ""
    echo "$(bold 'Switching to:') $(cyan "$profile_name")"
    echo ""

    # Use the on-device script which handles everything in one shot
    echo -n "  [1/2] Applying profile... "
    local result
    result=$("$ADB" shell "su -c 'sh /data/adb/jorkspoofer-switch.sh apply ${profile_name}'" 2>/dev/null | tr -d '\r')

    if echo "$result" | grep -q "^OK"; then
        echo "$(green '✓')"
    elif echo "$result" | grep -q "ERROR"; then
        echo "$(red '✗')"
        echo "  $(red "$result")"
        echo "  Run without arguments to see available profiles."
        exit 1
    else
        echo "$(yellow '?')"
        echo "  Output: $result"
    fi

    # Verify
    echo -n "  [2/2] Verifying... "
    local model brand device
    model=$("$ADB" shell "getprop ro.product.model" 2>/dev/null | tr -d '\r')
    brand=$("$ADB" shell "getprop ro.product.brand" 2>/dev/null | tr -d '\r')
    device=$("$ADB" shell "getprop ro.product.device" 2>/dev/null | tr -d '\r')
    echo "$(green '✓')"

    echo ""
    echo "$(green '  Profile switched successfully!')"
    echo "  Model:  $(bold "$model")"
    echo "  Brand:  $(bold "$brand")"
    echo "  Device: $(bold "$device")"
    echo ""
    echo "  $(yellow 'Note:') Already-running apps still see the old profile."
    echo "  New apps will see the new profile immediately."
    echo ""
    echo "  To reboot with new profile: $(cyan 'bash switch_profile.sh --reboot')"
    echo ""
}

# ── Restart BlueStacks (kill + relaunch from Mac side) ──
reboot_device() {
    echo ""
    echo "$(yellow 'Restarting BlueStacks...')"

    # Find the running instance name
    local instance
    instance=$(ps aux | grep '[B]lueStacks --instance' | sed 's/.*--instance //' | head -1)
    instance="${instance:-Tiramisu64}"

    # Kill BlueStacks
    echo -n "  [1/3] Stopping BlueStacks... "
    pkill -f 'BlueStacks --instance' 2>/dev/null
    sleep 2
    # Force kill if still running
    pkill -9 -f 'BlueStacks --instance' 2>/dev/null
    sleep 1
    echo "$(green '✓')"

    # Relaunch with correct instance (open -a doesn't pass --instance)
    echo -n "  [2/3] Launching BlueStacks ($instance)... "
    /Applications/BlueStacks.app/Contents/MacOS/BlueStacks --instance "$instance" &>/dev/null &
    disown
    echo "$(green '✓')"

    # Wait for ADB
    echo -n "  [3/3] Waiting for ADB... "
    "$ADB" kill-server 2>/dev/null
    sleep 1
    "$ADB" start-server 2>/dev/null
    local connected=false
    for i in $(seq 1 30); do
        sleep 2
        if "$ADB" devices 2>/dev/null | grep -q 'device$'; then
            connected=true
            break
        fi
    done
    if $connected; then
        echo "$(green '✓')"
        echo ""
        echo "$(green 'BlueStacks restarted!') All apps will load with the new profile."
    else
        echo "$(yellow '?')"
        echo ""
        echo "$(yellow 'BlueStacks is starting...') ADB may take a moment to connect."
        echo "  Run: $ADB devices   to check when it's ready."
    fi
    echo ""
}

# ── Random profile ──
random_profile() {
    local profiles
    profiles=$(adb_su "ls $MODDIR/profiles/*.conf" | grep -v active.conf)
    local count
    count=$(echo "$profiles" | wc -l | tr -d ' ')
    local idx
    idx=$((RANDOM % count + 1))
    local chosen
    chosen=$(echo "$profiles" | sed -n "${idx}p")
    local name
    name=$(basename "$chosen" .conf 2>/dev/null | tr -d '\r')
    echo "$(yellow 'Random selection:') $name"
    hot_reload "$name"
}

# ── Main ──
case "${1:-}" in
    "")
        list_profiles
        ;;
    --current|-c)
        show_current
        ;;
    --random|-r)
        random_profile
        ;;
    --reboot|--restart-zygote|--rz)
        reboot_device
        ;;
    --list|-l)
        list_profiles
        ;;
    --help|-h)
        echo "Usage: bash switch_profile.sh [profile-name|--random|--current|--reboot]"
        ;;
    *)
        hot_reload "$1"
        ;;
esac
