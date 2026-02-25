#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Luke's Mirage — Golden Image Creator (macOS)
#
# Creates a ready-to-clone "golden" data.qcow2 from a fully configured
# BlueStacks Air instance. The golden image includes:
#   - Kitsune Magisk data (/data/adb/magisk/*)
#   - Zygisk enabled
#   - LSPosed module installed
#   - jorkSpoofer Magisk module + native Zygisk module + LSPosed APK
#   - All modules verified working
#
# The resulting archive can be distributed and used by the jorkSpoofer GUI
# to clone new instances without manual module installation.
#
# Prerequisites:
#   1. BlueStacks Air must be rooted (run root_bluestacks.sh first)
#   2. A single instance must be fully configured with all modules
#   3. BlueStacks Air must be STOPPED before creating the golden image
#
# Usage:
#   ./create_golden_image.sh                     # Interactive (auto-detects source)
#   ./create_golden_image.sh --source Tiramisu64 # Use specific instance
#   ./create_golden_image.sh --verify            # Verify an instance is ready
#   ./create_golden_image.sh --help
#
# Output:
#   golden_image/
#   ├── data.qcow2          (the golden disk image)
#   └── golden_info.json    (metadata: modules, versions, timestamp)
#
# ═══════════════════════════════════════════════════════════════════════════════

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BLUESTACKS="/Applications/BlueStacks.app"
ENGINE_DIR="/Users/Shared/Library/Application Support/BlueStacks/Engine"
DATA_DIR="/Users/Shared/Library/Application Support/BlueStacks"
ADB_EXE="$BLUESTACKS/Contents/MacOS/hd-adb"
CONF_PATH="$DATA_DIR/bluestacks.conf"
OUTPUT_DIR="$SCRIPT_DIR/golden_image"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()   { echo -e "${CYAN}[*]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*"; }
fatal() { err "$*"; exit 1; }

# ── Helpers ──────────────────────────────────────────────────────────────────

get_adb_port() {
    local instance="$1"
    grep "^bst.instance.${instance}.adb_port=" "$CONF_PATH" 2>/dev/null | cut -d= -f2 | tr -d '"'
}

adb_shell() {
    local serial="$1"
    shift
    "$ADB_EXE" -s "$serial" shell "$@" 2>/dev/null
}

list_instances() {
    if [ ! -f "$CONF_PATH" ]; then
        return
    fi
    grep 'adb_port' "$CONF_PATH" 2>/dev/null | sed -n 's/^bst\.instance\.\([^.]*\)\.adb_port=.*/\1/p' | sort -u
}

# ── Verify Instance ─────────────────────────────────────────────────────────

verify_instance() {
    local instance="$1"
    local adb_port
    adb_port=$(get_adb_port "$instance")

    if [ -z "$adb_port" ]; then
        fatal "Instance '$instance' not found in bluestacks.conf or has no adb_port"
    fi

    local serial="127.0.0.1:$adb_port"
    local instance_dir="$ENGINE_DIR/$instance"

    echo ""
    echo -e "${BOLD}Verifying instance: $instance (port $adb_port)${NC}"
    echo ""

    # Check data.qcow2 exists
    if [ -f "$instance_dir/data.qcow2" ]; then
        local size
        size=$(du -h "$instance_dir/data.qcow2" | cut -f1)
        ok "data.qcow2 exists ($size)"
    else
        fatal "data.qcow2 not found at $instance_dir/"
    fi

    # Try to connect via ADB
    local connected=false
    if "$ADB_EXE" connect "$serial" 2>/dev/null | grep -q "connected"; then
        connected=true
        ok "ADB connected to $serial"
    else
        warn "Could not connect via ADB — instance may not be running"
        warn "Start the instance and re-run with --verify to do a full check"
        return 1
    fi

    # Check boot completed
    local boot
    boot=$(adb_shell "$serial" "getprop sys.boot_completed" | tr -d '\r')
    if [ "$boot" = "1" ]; then
        ok "Instance is fully booted"
    else
        warn "Instance not fully booted (sys.boot_completed=$boot)"
        return 1
    fi

    # Check root access
    local root_check
    root_check=$(adb_shell "$serial" "su -c id" | tr -d '\r')
    if echo "$root_check" | grep -q "uid=0"; then
        ok "Root access (su) working"
    else
        err "Root access FAILED — Magisk not working"
        return 1
    fi

    # Check SDK version
    local sdk
    sdk=$(adb_shell "$serial" "getprop ro.build.version.sdk" | tr -d '\r')
    log "Android SDK version: $sdk"

    # Check Magisk version
    local magisk_ver
    magisk_ver=$(adb_shell "$serial" "su -c '/data/adb/magisk/magisk64 -v'" | tr -d '\r')
    if [ -n "$magisk_ver" ]; then
        ok "Magisk version: $magisk_ver"
    else
        warn "Could not determine Magisk version"
    fi

    # Check Zygisk
    local zygisk
    zygisk=$(adb_shell "$serial" "su -c 'cat /data/adb/magisk/db.json 2>/dev/null'" | tr -d '\r')
    if echo "$zygisk" | grep -qE 'zygisk.*(true|1)'; then
        ok "Zygisk is enabled"
    else
        warn "Zygisk status unclear — check Magisk settings"
    fi

    # Check installed Magisk modules
    echo ""
    log "Installed Magisk modules:"
    local modules
    modules=$(adb_shell "$serial" "su -c 'ls /data/adb/modules/ 2>/dev/null'" | tr -d '\r')
    if [ -n "$modules" ]; then
        echo "$modules" | while read -r mod; do
            if [ -n "$mod" ]; then
                local mod_name
                mod_name=$(adb_shell "$serial" "su -c 'cat /data/adb/modules/$mod/module.prop 2>/dev/null'" | grep "^name=" | cut -d= -f2 | tr -d '\r')
                local mod_ver
                mod_ver=$(adb_shell "$serial" "su -c 'cat /data/adb/modules/$mod/module.prop 2>/dev/null'" | grep "^version=" | cut -d= -f2 | tr -d '\r')
                ok "  $mod: ${mod_name:-unknown} v${mod_ver:-?}"
            fi
        done
    else
        warn "  No Magisk modules found"
    fi

    # Check jorkSpoofer specifically
    echo ""
    if adb_shell "$serial" "su -c 'test -d /data/adb/modules/jorkspoofer && echo yes'" | grep -q "yes"; then
        ok "jorkSpoofer Magisk module: INSTALLED"
    else
        warn "jorkSpoofer Magisk module: NOT FOUND"
    fi

    if adb_shell "$serial" "su -c 'test -d /data/adb/modules/jorkspoofer-native && echo yes'" | grep -q "yes"; then
        ok "jorkSpoofer-Native (Zygisk GL): INSTALLED"
    else
        warn "jorkSpoofer-Native (Zygisk GL): NOT FOUND"
    fi

    # Check LSPosed
    if adb_shell "$serial" "su -c 'test -d /data/adb/lspd && echo yes'" | grep -q "yes"; then
        ok "LSPosed: INSTALLED"
    elif adb_shell "$serial" "su -c 'test -d /data/adb/modules/lsposed && echo yes'" | grep -q "yes"; then
        ok "LSPosed module: INSTALLED"
    else
        warn "LSPosed: NOT FOUND"
    fi

    # Check jorkSpoofer profiles
    local profile_count
    profile_count=$(adb_shell "$serial" "su -c 'ls /data/adb/modules/jorkspoofer/profiles/*.conf 2>/dev/null | wc -l'" | tr -d '\r' | tr -d ' ')
    if [ -n "$profile_count" ] && [ "$profile_count" -gt 0 ] 2>/dev/null; then
        ok "Device profiles: $profile_count profiles available"
        # Check how many match SDK 33
        local sdk33_count
        sdk33_count=$(adb_shell "$serial" "su -c 'grep -l PROFILE_BUILD_VERSION_SDK=\\\"$sdk\\\" /data/adb/modules/jorkspoofer/profiles/*.conf 2>/dev/null | wc -l'" | tr -d '\r' | tr -d ' ')
        log "  Profiles matching SDK $sdk: ${sdk33_count:-0}"
    else
        warn "No device profiles found"
    fi

    echo ""
    return 0
}

# ── Create Golden Image ──────────────────────────────────────────────────────

create_golden_image() {
    local instance="$1"
    local instance_dir="$ENGINE_DIR/$instance"
    local data_qcow2="$instance_dir/data.qcow2"

    echo ""
    echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  Luke's Mirage — Golden Image Creator${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
    echo ""

    # Verify source
    if [ ! -f "$data_qcow2" ]; then
        fatal "data.qcow2 not found at $instance_dir/"
    fi

    # Check BlueStacks is stopped
    if pgrep -x BlueStacks >/dev/null 2>&1; then
        fatal "BlueStacks is still running. Quit it first so the disk is consistent."
    fi
    ok "BlueStacks is not running"

    # ── Pre-flight verification ──
    # Start instance temporarily to check critical components
    log "Running pre-flight checks on $instance..."

    local adb_port
    adb_port=$(get_adb_port "$instance")
    if [ -n "$adb_port" ]; then
        # Start instance for verification
        log "Starting instance for verification..."
        open -na "$BLUESTACKS" --args --instance "$instance" &
        local serial="127.0.0.1:$adb_port"
        local booted=false

        for i in $(seq 1 45); do
            sleep 2
            "$ADB_EXE" connect "$serial" >/dev/null 2>&1
            local bc
            bc=$(adb_shell "$serial" "getprop sys.boot_completed" | tr -d '\r')
            if [ "$bc" = "1" ]; then
                booted=true
                break
            fi
        done

        if $booted; then
            sleep 3  # Let Magisk finish init

            # Check 1: Native module version >= v2.0.0
            local native_ver
            native_ver=$(adb_shell "$serial" "su -c 'cat /data/adb/modules/jorkspoofer-native/module.prop 2>/dev/null'" | grep "^version=" | cut -d= -f2 | tr -d '\r')
            if [ -n "$native_ver" ]; then
                # Extract major version number
                local major
                major=$(echo "$native_ver" | sed 's/[^0-9].*//')
                if [ "${major:-0}" -ge 2 ]; then
                    ok "Native module version: $native_ver"
                else
                    err "Native module version $native_ver is too old (need >= v2.0.0)"
                    warn "Install LukesMirage-Native-v2.0.0.zip before creating golden image"
                fi
            else
                err "Native module (jorkspoofer-native) NOT INSTALLED"
            fi

            # Check 2: strings table exists in magisk.db
            local strings_check
            strings_check=$(adb_shell "$serial" "su -c '/sbin/magisk --sqlite \"SELECT count(*) FROM sqlite_master WHERE type=\\\"table\\\" AND name=\\\"strings\\\"\"'" | tr -d '\r' | tr -d ' ')
            if [ "$strings_check" = "1" ]; then
                ok "Magisk strings table: exists"
            else
                warn "Magisk strings table: MISSING (su dialog may crash)"
                warn "This will be created on next boot via magisk.rc"
            fi

            # Check 3: Required Magisk modules
            local required_modules="jorkspoofer jorkspoofer-native rezygisk zygisk_lsposed"
            local missing=""
            for mod in $required_modules; do
                local exists
                exists=$(adb_shell "$serial" "su -c 'test -d /data/adb/modules/$mod && echo yes'" | tr -d '\r')
                if [ "$exists" = "yes" ]; then
                    ok "Module: $mod"
                else
                    err "Module MISSING: $mod"
                    missing="$missing $mod"
                fi
            done

            if [ -n "$missing" ]; then
                fatal "Missing required modules:$missing — install them before creating golden image"
            fi

            # Stop the instance
            log "Stopping instance after verification..."
            echo "Syncing filesystems..."
            "$ADB_EXE" -s "$serial" shell "su -c sync" 2>/dev/null || true
            sleep 1
            pkill -f "BlueStacks.*--instance $instance" 2>/dev/null || true
            sleep 3
            # Make sure it's fully stopped
            if pgrep -x BlueStacks >/dev/null 2>&1; then
                pkill -x BlueStacks 2>/dev/null || true
                sleep 2
            fi
            ok "Pre-flight checks passed"
        else
            warn "Could not boot instance for pre-flight — proceeding without verification"
            echo "Killing BlueStacks after failed pre-flight..."
            pkill -x BlueStacks 2>/dev/null || true
            pkill -x HD-MultiInstanceManager 2>/dev/null || true
            sleep 3
        fi
    else
        warn "No ADB port found for $instance — skipping pre-flight verification"
    fi

    # Create output directory
    mkdir -p "$OUTPUT_DIR"

    # Get size info
    local src_size
    src_size=$(du -h "$data_qcow2" | cut -f1)
    log "Source: $data_qcow2 ($src_size)"
    log "Destination: $OUTPUT_DIR/"
    echo ""

    # Copy data.qcow2
    log "Copying data.qcow2 (this may take a while)..."
    cp "$data_qcow2" "$OUTPUT_DIR/data.qcow2"
    ok "data.qcow2 copied"

    # Create metadata file
    local timestamp
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local bs_version
    bs_version=$(defaults read "$BLUESTACKS/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || echo "unknown")

    cat > "$OUTPUT_DIR/golden_info.json" << JSONEOF
{
    "created": "$timestamp",
    "source_instance": "$instance",
    "bluestacks_version": "$bs_version",
    "platform": "mac",
    "android_version": "13",
    "sdk_level": "33",
    "instance_type": "Tiramisu64",
    "disk_format": "qcow2",
    "notes": "Pre-rooted golden image with Kitsune Magisk, Zygisk, and jorkSpoofer modules"
}
JSONEOF
    ok "Metadata saved to golden_info.json"

    # Optionally compress
    echo ""
    read -p "  Compress golden image with 7z? (recommended for distribution) [Y/n] " -r reply
    if [[ -z "$reply" || "$reply" =~ ^[Yy]$ ]]; then
        local archive="$OUTPUT_DIR/jSpoof-golden-mac-a13.7z"
        local sevenz=""

        # Find 7z binary
        if [ -f "$BLUESTACKS/Contents/MacOS/7zz" ]; then
            sevenz="$BLUESTACKS/Contents/MacOS/7zz"
        elif command -v 7z &>/dev/null; then
            sevenz="7z"
        elif command -v 7zz &>/dev/null; then
            sevenz="7zz"
        fi

        if [ -n "$sevenz" ]; then
            log "Compressing with $sevenz..."
            "$sevenz" a -t7z -mx=5 "$archive" "$OUTPUT_DIR/data.qcow2" "$OUTPUT_DIR/golden_info.json" >/dev/null
            local archive_size
            archive_size=$(du -h "$archive" | cut -f1)
            ok "Archive created: $archive ($archive_size)"
        else
            warn "7z not found — skipping compression"
            warn "Install via: brew install p7zip"
        fi
    fi

    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  GOLDEN IMAGE CREATED!${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  Output: $OUTPUT_DIR/"
    echo ""
    echo "  To use this golden image:"
    echo "  1. Place data.qcow2 in the jorkSpoofer images directory"
    echo "  2. Use the jorkSpoofer GUI to clone from this source"
    echo "  3. Run randomize_instances.py to give each clone unique identifiers"
    echo ""
    echo "  To distribute:"
    if [ -f "$OUTPUT_DIR/jSpoof-golden-mac-a13.7z" ]; then
        echo "  Upload jSpoof-golden-mac-a13.7z to a GitHub release or shared drive"
    else
        echo "  Compress data.qcow2 + golden_info.json into a .7z archive"
    fi
    echo ""
}

# ── Setup Guide ──────────────────────────────────────────────────────────────

show_setup_guide() {
    echo ""
    echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  Golden Image Setup Guide${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  To create a golden image, you need a fully configured BlueStacks"
    echo "  Air instance. Follow these steps:"
    echo ""
    echo "  ${BOLD}Step 1: Root BlueStacks Air${NC}"
    echo "    ./root_bluestacks.sh"
    echo ""
    echo "  ${BOLD}Step 2: Launch BlueStacks Air and set up the base instance${NC}"
    echo "    - Complete the Android setup wizard"
    echo "    - Install the Kitsune Magisk APK (drag onto BS window)"
    echo "    - Open Kitsune Mask → accept 'Requires Additional Setup' → reboot"
    echo "    - Open Kitsune Mask → Settings → enable Zygisk → reboot"
    echo ""
    echo "  ${BOLD}Step 3: Install jorkSpoofer modules via ADB${NC}"
    echo "    hd-adb connect 127.0.0.1:<port>"
    echo "    hd-adb -s 127.0.0.1:<port> push jorkspoofer-magisk.zip /sdcard/"
    echo "    hd-adb -s 127.0.0.1:<port> shell su -c 'magisk --install-module /sdcard/jorkspoofer-magisk.zip'"
    echo "    # Repeat for jorkspoofer-native (Zygisk GL module)"
    echo "    # Install LSPosed module zip the same way"
    echo "    # Install jorkSpoofer LSPosed APK via: hd-adb install jorkspoofer-lsposed.apk"
    echo ""
    echo "  ${BOLD}Step 4: Verify everything works${NC}"
    echo "    ./create_golden_image.sh --verify"
    echo ""
    echo "  ${BOLD}Step 5: Stop BlueStacks and create the golden image${NC}"
    echo "    # Quit BlueStacks Air (Cmd+Q)"
    echo "    ./create_golden_image.sh --source Tiramisu64"
    echo ""
}

# ── Main ────────────────────────────────────────────────────────────────────

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Commands:"
    echo "  (default)              Interactive mode (auto-detects source instance)"
    echo "  --source NAME          Create golden image from specific instance"
    echo "  --verify               Verify an instance has all required modules"
    echo "  --guide                Show step-by-step setup guide"
    echo "  --help                 Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --guide                      # Show setup instructions"
    echo "  $0 --verify                     # Check if default instance is ready"
    echo "  $0 --source Tiramisu64          # Create golden image from Tiramisu64"
}

SOURCE_INSTANCE=""
ACTION="create"

while [ $# -gt 0 ]; do
    case "$1" in
        --source)
            SOURCE_INSTANCE="$2"
            shift 2
            ;;
        --verify)
            ACTION="verify"
            shift
            ;;
        --guide)
            ACTION="guide"
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            err "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

# Checks common to all actions
if [ "$ACTION" != "guide" ]; then
    if [ ! -d "$BLUESTACKS" ]; then
        fatal "BlueStacks Air not found at $BLUESTACKS"
    fi
    if [ ! -f "$ADB_EXE" ]; then
        warn "hd-adb not found at $ADB_EXE — ADB verification will be limited"
    fi
fi

case "$ACTION" in
    guide)
        show_setup_guide
        ;;
    verify)
        if [ -z "$SOURCE_INSTANCE" ]; then
            # Auto-detect: use first instance found
            instances=($(list_instances))
            if [ ${#instances[@]} -eq 0 ]; then
                fatal "No instances found in bluestacks.conf"
            fi
            SOURCE_INSTANCE="${instances[0]}"
            log "Auto-detected instance: $SOURCE_INSTANCE"
        fi
        verify_instance "$SOURCE_INSTANCE"
        ;;
    create)
        if [ -z "$SOURCE_INSTANCE" ]; then
            instances=($(list_instances))
            if [ ${#instances[@]} -eq 0 ]; then
                fatal "No instances found in bluestacks.conf"
            fi
            if [ ${#instances[@]} -eq 1 ]; then
                SOURCE_INSTANCE="${instances[0]}"
                log "Auto-detected instance: $SOURCE_INSTANCE"
            else
                echo "Available instances:"
                for i in "${!instances[@]}"; do
                    echo "  $((i+1)). ${instances[$i]}"
                done
                read -p "Select instance number: " -r num
                SOURCE_INSTANCE="${instances[$((num-1))]}"
            fi
        fi
        create_golden_image "$SOURCE_INSTANCE"
        ;;
esac
