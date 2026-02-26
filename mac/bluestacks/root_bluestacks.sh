#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Luke's Mirage — BlueStacks Air Root Tool (macOS)
#
# Patches initrd_hvf.img to inject Kitsune Magisk into the BlueStacks Air
# boot process. This enables root access, Zygisk, and Magisk module support
# across ALL instances.
#
# Usage:
#   ./root_bluestacks.sh                  # Interactive mode (auto-downloads Kitsune Magisk)
#   ./root_bluestacks.sh --apk magisk.apk # Use a specific Magisk APK
#   ./root_bluestacks.sh --unroot         # Restore original (unrooted) initrd
#   ./root_bluestacks.sh --status         # Check if currently rooted
#
# After rooting:
#   1. Launch BlueStacks Air
#   2. Install the Kitsune Magisk APK via ADB:
#      hd-adb install -r -d -g /path/to/kitsune-magisk.apk
#   3. Open Kitsune Mask app — it should show the main dashboard (no setup needed)
#   4. Enable Zygisk: Settings → Zygisk → reboot
#   5. Install spoofing modules via ADB
#
# Notes:
#   - BlueStacks Air must be CLOSED before rooting
#   - SIP bypass is handled automatically via AppleScript Finder automation
#   - BlueStacks updates will overwrite the patch — re-run after updating
# ═══════════════════════════════════════════════════════════════════════════════

set -e
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# JORK_DATA_DIR: writable location for backups/cache (set by frozen .app, defaults to SCRIPT_DIR)
JORK_DATA_DIR="${JORK_DATA_DIR:-$SCRIPT_DIR}"
mkdir -p "$JORK_DATA_DIR/backups" "$JORK_DATA_DIR/cache" 2>/dev/null || true
BLUESTACKS="/Applications/BlueStacks.app"
INITRD_PATH="$BLUESTACKS/Contents/img/initrd_hvf.img"
MAGISK_RC="$SCRIPT_DIR/magisk.rc"
ARCH="arm64-v8a"
KITSUNE_API="https://api.github.com/repos/1q23lyc45/KitsuneMagisk/releases/latest"
GUI_MODE=0

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

# ── Cleanup ─────────────────────────────────────────────────────────────────

TEMP_DIR=""
cleanup() {
    if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
}
trap cleanup EXIT

# ── Checks ──────────────────────────────────────────────────────────────────

check_bluestacks_installed() {
    if [ ! -d "$BLUESTACKS" ]; then
        fatal "BlueStacks Air not found at $BLUESTACKS"
    fi
    if [ ! -f "$INITRD_PATH" ]; then
        fatal "initrd_hvf.img not found at $INITRD_PATH"
    fi
    # Try both plist keys (CFBundleVersion for GUI consistency, ShortVersionString as fallback)
    local version
    version=$(defaults read "$BLUESTACKS/Contents/Info.plist" CFBundleVersion 2>/dev/null || \
              defaults read "$BLUESTACKS/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || \
              echo "unknown")
    log "BlueStacks Air version: $version"

    REQUIRED_VERSION="5.21.755.7538"
    if [ "$version" != "$REQUIRED_VERSION" ]; then
        warn "BlueStacks version mismatch: found $version, expected $REQUIRED_VERSION"
        if [ "$GUI_MODE" = "1" ]; then
            warn "GUI mode — proceeding with version $version"
        else
            echo ""
            echo "Rooting a different version may produce an unstable or broken instance."
            read -p "Continue anyway? (y/N) " answer
            case "$answer" in
                [yY]|[yY][eE][sS]) echo "Proceeding with version $version..." ;;
                *) echo "Aborting."; exit 1 ;;
            esac
        fi
    fi
}

check_bluestacks_stopped() {
    if pgrep -x BlueStacks >/dev/null 2>&1; then
        if [ "$GUI_MODE" = "1" ]; then
            log "BlueStacks is running — auto-stopping for rooting..."
            pkill -x BlueStacks 2>/dev/null || true
            sleep 2
            # Force kill if still alive
            if pgrep -x BlueStacks >/dev/null 2>&1; then
                pkill -9 -x BlueStacks 2>/dev/null || true
                sleep 2
            fi
            # Also kill related processes
            for proc in HD-MultiInstanceManager hd-adb HD-DiskCompaction HD-LogCollector; do
                pkill -x "$proc" 2>/dev/null || true
            done
            sleep 1
            if pgrep -x BlueStacks >/dev/null 2>&1; then
                fatal "Could not stop BlueStacks. Please quit it manually and try again."
            fi
            ok "BlueStacks stopped automatically"
        else
            fatal "BlueStacks is still running. Quit it first (Cmd+Q or pkill -x BlueStacks)"
        fi
    else
        ok "BlueStacks is not running"
    fi
}

check_dependencies() {
    for cmd in cpio gzip unzip curl; do
        if ! command -v "$cmd" &>/dev/null; then
            fatal "Required command not found: $cmd"
        fi
    done
}

# ── Status Check ────────────────────────────────────────────────────────────

check_root_status() {
    check_bluestacks_installed
    TEMP_DIR=$(mktemp -d)
    local initrd_dir="$TEMP_DIR/initrd"
    mkdir -p "$initrd_dir"

    # Unpack initrd
    if gzip -t "$INITRD_PATH" >/dev/null 2>&1; then
        gzip -dc "$INITRD_PATH" | (cd "$initrd_dir" && cpio -id 2>/dev/null)
    else
        (cd "$initrd_dir" && cpio -id < "$INITRD_PATH" 2>/dev/null)
    fi

    if [ -d "$initrd_dir/boot/magisk" ] && [ -f "$initrd_dir/boot/magisk.rc" ]; then
        ok "BlueStacks Air is ROOTED (Magisk files found in initrd)"
        if [ -f "$JORK_DATA_DIR/backups/initrd_hvf.img.bak" ]; then
            ok "Backup exists at $JORK_DATA_DIR/backups/initrd_hvf.img.bak"
        elif [ -f "$INITRD_PATH.bak" ]; then
            ok "Backup exists at $INITRD_PATH.bak"
        else
            warn "No backup found — unroot will not be possible without reinstalling BlueStacks"
        fi
        return 0
    else
        log "BlueStacks Air is NOT rooted (stock initrd)"
        return 1
    fi
}

# ── Download Kitsune Magisk ─────────────────────────────────────────────────

download_kitsune_magisk() {
    local dest="$1"
    log "Fetching latest Kitsune Magisk release..."

    local api_response dl_url
    api_response=$(curl -fsSL "$KITSUNE_API" 2>&1) || true

    # Check for GitHub rate limiting
    if echo "$api_response" | grep -qi "rate limit\|API rate"; then
        warn "GitHub API rate limited. Trying direct download..."
        api_response=""
    fi

    # Parse JSON with python3 (robust) or grep fallback
    if [ -n "$api_response" ]; then
        dl_url=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
    for asset in data.get('assets', []):
        url = asset.get('browser_download_url', '')
        if url.endswith('.apk') and 'release' in url.lower():
            print(url)
            sys.exit(0)
    # Fallback: any APK
    for asset in data.get('assets', []):
        url = asset.get('browser_download_url', '')
        if url.endswith('.apk'):
            print(url)
            sys.exit(0)
except Exception:
    pass
" <<< "$api_response" 2>/dev/null) || dl_url=""
    fi

    # Fallback: grep parsing if python3 failed
    if [ -z "$dl_url" ] && [ -n "$api_response" ]; then
        dl_url=$(echo "$api_response" | grep -oE '"browser_download_url"\s*:\s*"[^"]*\.apk"' | head -1 | grep -oE 'https://[^"]+') || dl_url=""
    fi

    if [ -z "$dl_url" ]; then
        # Check if we have a cached APK from a previous run
        local cached_apk="$JORK_DATA_DIR/cache/kitsune-magisk-cached.apk"
        if [ -f "$cached_apk" ] && [ -s "$cached_apk" ]; then
            warn "Could not reach GitHub API — using cached Kitsune Magisk APK"
            cp "$cached_apk" "$dest"
            return 0
        fi
        fatal "Could not find Kitsune Magisk download URL (GitHub may be rate-limited). Download manually and use --apk flag."
    fi

    log "Downloading from: $dl_url"
    if [ "$GUI_MODE" = "1" ]; then
        curl -fL "$dl_url" -o "$dest" -s
    else
        curl -fL "$dl_url" -o "$dest" --progress-bar
    fi
    if [ ! -f "$dest" ] || [ ! -s "$dest" ]; then
        fatal "Download failed or file is empty"
    fi

    # Cache the APK for future runs (in case GitHub gets rate-limited later)
    cp "$dest" "$JORK_DATA_DIR/cache/kitsune-magisk-cached.apk" 2>/dev/null || true

    ok "Downloaded Kitsune Magisk APK ($(du -h "$dest" | cut -f1))"
}

# ── Extract Magisk Binaries ────────────────────────────────────────────────

extract_magisk_binaries() {
    local apk_path="$1"
    local bin_dir="$2"

    log "Extracting Magisk binaries from APK..."
    local apk_extract="$TEMP_DIR/magisk_apk"
    mkdir -p "$apk_extract"
    unzip -oq "$apk_path" -d "$apk_extract"

    mkdir -p "$bin_dir"

    # ── Native binaries (lib/arm64-v8a/lib*.so → renamed) ──
    local required_bins=("magisk64" "magiskinit" "magiskpolicy")
    for bin_name in "${required_bins[@]}"; do
        local src="$apk_extract/lib/$ARCH/lib${bin_name}.so"
        if [ ! -f "$src" ]; then
            fatal "Binary not found in APK: lib/$ARCH/lib${bin_name}.so"
        fi
        cp "$src" "$bin_dir/$bin_name"
        chmod 700 "$bin_dir/$bin_name"
    done

    # Optional native binaries
    local optional_bins=("magisk32" "magiskboot" "busybox")
    for bin_name in "${optional_bins[@]}"; do
        local src="$apk_extract/lib/$ARCH/lib${bin_name}.so"
        if [ -f "$src" ]; then
            cp "$src" "$bin_dir/$bin_name"
            chmod 700 "$bin_dir/$bin_name"
            log "Extracted optional binary: $bin_name"
        fi
    done

    # ── Assets (stub.apk, scripts, main.jar) ──
    local asset_files=("stub.apk" "util_functions.sh" "boot_patch.sh" "addon.d.sh" "main.jar")
    local assets_found=()
    for asset in "${asset_files[@]}"; do
        local src="$apk_extract/assets/$asset"
        if [ -f "$src" ]; then
            cp "$src" "$bin_dir/$asset"
            # Scripts get 700, data files get 644
            case "$asset" in
                *.sh) chmod 700 "$bin_dir/$asset" ;;
                *)    chmod 644 "$bin_dir/$asset" ;;
            esac
            assets_found+=("$asset")
        fi
    done

    if [ ${#assets_found[@]} -eq 0 ]; then
        warn "No asset files found in APK"
    else
        log "Extracted assets: ${assets_found[*]}"
    fi

    # Also save the full APK for ADB sideloading
    cp "$apk_path" "$bin_dir/kitsune-magisk.apk"
    chmod 644 "$bin_dir/kitsune-magisk.apk"

    ok "Extracted: ${required_bins[*]} + ${#optional_bins[@]} optional bins + ${#assets_found[@]} assets + full APK"
}

# ── Patch initrd ────────────────────────────────────────────────────────────

patch_initrd() {
    local magisk_bin_dir="$1"
    local output_path="$2"

    log "Unpacking initrd_hvf.img..."
    local initrd_dir="$TEMP_DIR/initrd"
    mkdir -p "$initrd_dir"

    if gzip -t "$INITRD_PATH" >/dev/null 2>&1; then
        gzip -dc "$INITRD_PATH" | (cd "$initrd_dir" && cpio -id 2>/dev/null)
    else
        (cd "$initrd_dir" && cpio -id < "$INITRD_PATH" 2>/dev/null)
    fi

    # Verify stage2.sh exists
    if [ ! -f "$initrd_dir/boot/stage2.sh" ]; then
        fatal "boot/stage2.sh not found in initrd — unexpected initrd format"
    fi

    # Check if already patched
    if [ -d "$initrd_dir/boot/magisk" ]; then
        warn "initrd appears to already be patched (boot/magisk/ exists)"
        warn "Re-patching with fresh Magisk binaries..."
        rm -rf "$initrd_dir/boot/magisk"
        rm -f "$initrd_dir/boot/magisk.rc"
    fi

    # Inject Magisk files
    log "Injecting Magisk binaries into ramdisk..."
    cp -r "$magisk_bin_dir" "$initrd_dir/boot/magisk"
    chmod 700 "$initrd_dir/boot/magisk"/*

    # Copy magisk.rc
    if [ ! -f "$MAGISK_RC" ]; then
        fatal "magisk.rc not found at $MAGISK_RC"
    fi
    cp "$MAGISK_RC" "$initrd_dir/boot/magisk.rc"

    # Handle 32-bit variant
    if [ -f "$initrd_dir/boot/magisk/magisk32" ]; then
        sed -e 's/magisk64/magisk32/g' "$initrd_dir/boot/magisk.rc" > "$initrd_dir/boot/magisk.rc.tmp"
        mv "$initrd_dir/boot/magisk.rc.tmp" "$initrd_dir/boot/magisk.rc"
        log "Adjusted magisk.rc for 32-bit magisk"
    fi

    # Patch stage2.sh — idempotent: strip any previous magisk patch, then
    # remove `exec /init`, append Magisk init + exec /init.
    # Without the idempotency guard, re-running this script would append
    # additional copies of the magisk.rc block (each run's sed strips the
    # previous `exec /init`, leaving the old magisk stanza in place).
    log "Patching boot/stage2.sh..."
    # 1. Strip any existing magisk patch (everything from "Installing magisk.rc"
    #    to EOF) so re-runs don't accumulate duplicate blocks.
    sed -e '/^log_echo "Installing magisk.rc"/,$d' \
        "$initrd_dir/boot/stage2.sh" > "$initrd_dir/boot/stage2.sh.clean"
    # 2. Strip the original `exec /init` from the cleaned content.
    sed -e 's/exec \/init//' \
        "$initrd_dir/boot/stage2.sh.clean" > "$initrd_dir/boot/stage2.sh.tmp"
    rm -f "$initrd_dir/boot/stage2.sh.clean"
    # 3. Append the magisk init block exactly once + exec /init.
    cat << 'PATCH_EOF' >> "$initrd_dir/boot/stage2.sh.tmp"
log_echo "Installing magisk.rc"
cat /boot/magisk.rc >> /init.bst.rc
die_if_error "Cannot install magisk.rc"

exec /init
PATCH_EOF
    mv "$initrd_dir/boot/stage2.sh.tmp" "$initrd_dir/boot/stage2.sh"
    chmod +x "$initrd_dir/boot/stage2.sh"
    ok "stage2.sh patched"

    # Repack
    log "Repacking initrd_hvf.img..."
    (cd "$initrd_dir" && find . | cpio -H newc -o 2>/dev/null) | gzip > "$output_path"
    ok "Patched initrd written to: $output_path"
}

# ── Finder Automation (SIP Bypass) ─────────────────────────────────────────
#
# macOS SIP (System Integrity Protection) blocks direct writes to
# /Applications/BlueStacks.app/Contents/ via cp, mv, etc.
# However, AppleScript Finder commands (duplicate, move, set name) are
# treated as user-initiated Finder operations and bypass SIP restrictions.
# This lets us install/restore files inside the app bundle without
# requiring the user to disable SIP or use sudo.
#

install_to_app_bundle() {
    local src_file="$1"
    local dest_dir="$BLUESTACKS/Contents/img"
    local dest_file="$dest_dir/initrd_hvf.img"

    # Strategy: rename original → .old, copy patched in with correct name, delete .old
    # This avoids issues with Finder's "with replacing" flag across macOS versions.
    # All operations use AppleScript Finder automation to bypass SIP.

    # Step 1: Rename the current initrd out of the way
    log "Moving original initrd aside via Finder..."
    if [ -f "$dest_file" ]; then
        if ! osascript -e "
            tell application \"Finder\"
                set targetFile to POSIX file \"$dest_file\" as alias
                set name of targetFile to \"initrd_hvf.img.old\"
            end tell
        " >/dev/null 2>&1; then
            # Fallback: try direct cp (works if SIP is disabled)
            warn "Finder automation failed, trying direct copy..."
            if cp "$src_file" "$dest_file" 2>/dev/null; then
                ok "Installed via direct copy (SIP appears disabled)"
                return 0
            fi
            fatal "Could not install patched initrd. Finder automation and direct copy both failed."
        fi
    fi

    # Step 2: Rename the source file to initrd_hvf.img so it lands with the right name
    local staged_file="$TEMP_DIR/initrd_hvf.img"
    if [ "$src_file" != "$staged_file" ]; then
        cp "$src_file" "$staged_file"
    fi

    # Step 3: Copy the patched file into the app bundle
    log "Copying patched initrd into app bundle via Finder..."
    osascript -e "
        tell application \"Finder\"
            set srcFile to POSIX file \"$staged_file\" as alias
            set destFolder to POSIX file \"$dest_dir/\" as alias
            duplicate srcFile to destFolder
        end tell
    " >/dev/null 2>&1

    # Step 4: Clean up the .old file (moves to Trash, but we have our local backup)
    local old_path="$dest_dir/initrd_hvf.img.old"
    if [ -f "$old_path" ]; then
        log "Cleaning up old initrd..."
        osascript -e "
            tell application \"Finder\"
                set targetFile to POSIX file \"$old_path\" as alias
                delete targetFile
            end tell
        " >/dev/null 2>&1 || true
    fi

    # Step 5: Verify installation
    if [ -f "$dest_file" ]; then
        local new_size
        new_size=$(stat -f%z "$dest_file" 2>/dev/null || echo "unknown")
        ok "Patched initrd installed successfully (${new_size} bytes)"
    else
        fatal "Installation verification failed — initrd_hvf.img not found after install"
    fi
}

restore_from_backup_via_finder() {
    local backup_file="$1"
    local dest_dir="$BLUESTACKS/Contents/img"
    local dest_file="$dest_dir/initrd_hvf.img"

    log "Restoring original initrd via Finder automation..."

    # Rename current (rooted) initrd out of the way
    if [ -f "$dest_file" ]; then
        osascript -e "
            tell application \"Finder\"
                set targetFile to POSIX file \"$dest_file\" as alias
                set name of targetFile to \"initrd_hvf.img.rooted\"
            end tell
        " >/dev/null 2>&1 || true
    fi

    # Copy backup into app bundle
    osascript -e "
        tell application \"Finder\"
            set srcFile to POSIX file \"$backup_file\" as alias
            set destFolder to POSIX file \"$dest_dir/\" as alias
            duplicate srcFile to destFolder with replacing
        end tell
    " >/dev/null 2>&1

    # Rename the copied backup to initrd_hvf.img
    local landed_name
    landed_name=$(basename "$backup_file")
    if [ "$landed_name" != "initrd_hvf.img" ]; then
        local landed_path="$dest_dir/$landed_name"
        osascript -e "
            tell application \"Finder\"
                set targetFile to POSIX file \"$landed_path\" as alias
                set name of targetFile to \"initrd_hvf.img\"
            end tell
        " >/dev/null 2>&1
    fi

    # Clean up the .rooted file
    local rooted_path="$dest_dir/initrd_hvf.img.rooted"
    if [ -f "$rooted_path" ]; then
        osascript -e "
            tell application \"Finder\"
                set targetFile to POSIX file \"$rooted_path\" as alias
                delete targetFile
            end tell
        " >/dev/null 2>&1 || true
    fi

    # Verify
    if [ -f "$dest_file" ]; then
        ok "Original initrd restored successfully"
    else
        fatal "Restore verification failed — initrd_hvf.img not found after restore"
    fi
}

# ── Root Command ────────────────────────────────────────────────────────────

do_root() {
    local apk_path="$1"

    echo ""
    echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  Luke's Mirage — BlueStacks Air Root Tool${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
    echo ""

    check_bluestacks_installed
    check_bluestacks_stopped
    check_dependencies

    TEMP_DIR=$(mktemp -d)

    # Get Magisk APK
    if [ -n "$apk_path" ] && [ -f "$apk_path" ]; then
        log "Using provided APK: $apk_path"
    else
        apk_path="$TEMP_DIR/kitsune-magisk.apk"
        download_kitsune_magisk "$apk_path"
    fi

    # Extract binaries
    local magisk_bin_dir="$TEMP_DIR/magisk_bin"
    extract_magisk_binaries "$apk_path" "$magisk_bin_dir"

    # Backup original initrd (always keep a local copy too)
    local local_backup="$JORK_DATA_DIR/backups/initrd_hvf.img.bak"
    if [ ! -f "$local_backup" ]; then
        log "Backing up original initrd..."
        cp "$INITRD_PATH" "$local_backup"
        ok "Backup saved to $local_backup"
    else
        ok "Backup already exists at $local_backup"
    fi

    # Patch
    local patched_initrd="$TEMP_DIR/initrd_hvf.img"
    patch_initrd "$magisk_bin_dir" "$patched_initrd"

    # Install patched initrd into app bundle
    # Uses AppleScript Finder automation to bypass SIP — no sudo, no user interaction
    echo ""
    log "Installing patched initrd into app bundle..."
    install_to_app_bundle "$patched_initrd"

    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  ROOT COMPLETE!${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  Next steps:"
    echo "  1. Launch BlueStacks Air"
    echo "  2. Install Kitsune Magisk APK via ADB:"
    echo "     hd-adb install -r -d -g /path/to/kitsune-magisk.apk"
    echo "     (The full APK is also embedded in the initrd at /boot/magisk/kitsune-magisk.apk)"
    echo "  3. Open Kitsune Mask app — should show main dashboard (no setup needed)"
    echo "  4. Enable Zygisk: Kitsune Mask → Settings → Zygisk → reboot"
    echo "  5. Install spoofing modules"
    echo ""
    echo -e "  ${YELLOW}NOTE: BlueStacks updates will overwrite the patch. Re-run this script after updating.${NC}"
    echo ""

    # Offer to launch BlueStacks (skip in GUI mode)
    if [ "$GUI_MODE" = "1" ]; then
        log "Rooting complete (GUI mode — skipping launch prompt)"
    else
        read -p "  Launch BlueStacks Air now? [Y/n] " -r reply
        if [[ -z "$reply" || "$reply" =~ ^[Yy]$ ]]; then
            open -n "$BLUESTACKS"
            log "BlueStacks Air is launching..."
        fi
    fi
}

# ── Unroot Command ──────────────────────────────────────────────────────────

do_unroot() {
    echo ""
    echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  Luke's Mirage — BlueStacks Air Unroot${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
    echo ""

    check_bluestacks_installed
    check_bluestacks_stopped

    # Find backup (prefer local copy since that's where we save it)
    local backup=""
    if [ -f "$JORK_DATA_DIR/backups/initrd_hvf.img.bak" ]; then
        backup="$JORK_DATA_DIR/backups/initrd_hvf.img.bak"
    elif [ -f "$INITRD_PATH.bak" ]; then
        backup="$INITRD_PATH.bak"
    else
        fatal "No backup found. Reinstall BlueStacks Air to restore the original initrd."
    fi

    restore_from_backup_via_finder "$backup"

    echo ""
    ok "BlueStacks Air has been UNROOTED."
    echo "  Magisk data inside instances (/data/adb/) is untouched."
    echo "  Re-run root_bluestacks.sh to re-root."
    echo ""
}

# ── Main ────────────────────────────────────────────────────────────────────

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Commands:"
    echo "  (default)        Root BlueStacks Air (auto-downloads Kitsune Magisk)"
    echo "  --unroot         Restore original unrooted initrd from backup"
    echo "  --status         Check if BlueStacks Air is currently rooted"
    echo ""
    echo "Options:"
    echo "  --apk PATH       Use a specific Kitsune Magisk APK file"
    echo "  --help           Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0                          # Root with auto-downloaded Kitsune Magisk"
    echo "  $0 --apk ~/Downloads/kitsune-magisk.apk"
    echo "  $0 --status"
    echo "  $0 --unroot"
}

APK_PATH=""
ACTION="root"

while [ $# -gt 0 ]; do
    case "$1" in
        --apk)
            APK_PATH="$2"
            shift 2
            ;;
        --unroot)
            ACTION="unroot"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        --gui)
            GUI_MODE=1
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

case "$ACTION" in
    root)
        do_root "$APK_PATH"
        ;;
    unroot)
        do_unroot
        ;;
    status)
        check_root_status
        ;;
esac
