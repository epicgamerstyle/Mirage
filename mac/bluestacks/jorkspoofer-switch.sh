#!/system/bin/sh
# jorkspoofer-switch.sh - On-device profile switcher for jorkSpoofer
# Pushed to /data/adb/jorkspoofer-switch.sh
#
# Usage (from root shell):
#   sh /data/adb/jorkspoofer-switch.sh list
#   sh /data/adb/jorkspoofer-switch.sh current
#   sh /data/adb/jorkspoofer-switch.sh apply <profile-name>
#   sh /data/adb/jorkspoofer-switch.sh apply <profile-name> --restart-zygote

MODDIR="/data/adb/modules/jorkspoofer"
ACTION="${1:-help}"

if [ "$ACTION" = "list" ]; then
    for f in "$MODDIR"/profiles/*.conf; do
        case "$f" in
            */active.conf) continue ;;
        esac
        [ ! -f "$f" ] && continue
        fname="${f##*/}"
        fname="${fname%.conf}"
        pname=""
        while IFS= read -r line; do
            case "$line" in
                PROFILE_NAME=*)
                    pname="${line#PROFILE_NAME=}"
                    pname="${pname#\"}"
                    pname="${pname%\"}"
                    break
                    ;;
            esac
        done < "$f"
        echo "${fname}|${pname}"
    done

elif [ "$ACTION" = "current" ]; then
    pname=""
    pdevice=""
    while IFS= read -r line; do
        case "$line" in
            PROFILE_NAME=*)
                pname="${line#PROFILE_NAME=}"
                pname="${pname#\"}"
                pname="${pname%\"}"
                ;;
            PROFILE_DEVICE=*)
                pdevice="${line#PROFILE_DEVICE=}"
                pdevice="${pdevice#\"}"
                pdevice="${pdevice%\"}"
                ;;
        esac
    done < "$MODDIR/profiles/active.conf"
    echo "${pdevice}|${pname}"

elif [ "$ACTION" = "apply" ]; then
    profile_name="${2:-}"
    if [ -z "$profile_name" ]; then
        echo "ERROR: No profile name specified"
        exit 1
    fi

    src="$MODDIR/profiles/${profile_name}.conf"
    if [ ! -f "$src" ]; then
        src="$MODDIR/profiles/custom/${profile_name}.conf"
        if [ ! -f "$src" ]; then
            echo "ERROR: Profile not found: $profile_name"
            exit 1
        fi
    fi

    # Copy to active.conf
    cp "$src" "$MODDIR/profiles/active.conf"
    chmod 644 "$MODDIR/profiles/active.conf"

    # Copy to world-readable location
    cp "$MODDIR/profiles/active.conf" /data/local/tmp/jorkspoofer_active.conf
    chmod 644 /data/local/tmp/jorkspoofer_active.conf

    # Source libs and re-apply properties
    cd "$MODDIR"
    . ./lib/utils.sh
    . ./lib/profile.sh
    . ./lib/props.sh
    . ./lib/identifiers.sh
    log_init /dev/null
    load_profile "$MODDIR/profiles/active.conf"
    spoof_build_properties

    # Re-bind /proc/cpuinfo with new profile's CPU info
    if [ -n "$PROFILE_CPU_CORES" ] || [ -n "$PROFILE_SOC_NAME" ]; then
        CORES="${PROFILE_CPU_CORES:-8}"
        CPU_PART_LITTLE="${PROFILE_CPU_PART_LITTLE:-0xd05}"
        CPU_PART_BIG="${PROFILE_CPU_PART_BIG:-0xd41}"
        {
            i=0
            half=$((CORES / 2))
            while [ "$i" -lt "$CORES" ]; do
                if [ "$i" -lt "$half" ]; then PART="$CPU_PART_LITTLE"; else PART="$CPU_PART_BIG"; fi
                printf "processor\t: %d\n" "$i"
                printf "BogoMIPS\t: %s\n" "${PROFILE_CPU_BOGOMIPS:-1804.80}"
                printf "Features\t: %s\n" "${PROFILE_CPU_FEATURES:-fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp}"
                printf "CPU implementer\t: 0x41\nCPU architecture: 8\nCPU variant\t: 0x1\n"
                printf "CPU part\t: %s\nCPU revision\t: 1\n\n" "$PART"
                i=$((i + 1))
            done
            printf "Hardware\t: %s\nRevision\t: 0000\nSerial\t\t: %s\n" \
                "${PROFILE_SOC_NAME:-Qualcomm}" "${PROFILE_SERIAL:-0000000000000000}"
        } > "${MODDIR}/cache/cpuinfo"
        mount --bind "${MODDIR}/cache/cpuinfo" /proc/cpuinfo 2>/dev/null
    fi

    # Re-bind /proc/version with new kernel string
    if [ -n "$PROFILE_KERNEL_VERSION" ]; then
        printf "%s\n" "$PROFILE_KERNEL_VERSION" > "${MODDIR}/cache/version"
        mount --bind "${MODDIR}/cache/version" /proc/version 2>/dev/null
    fi

    # ── Update hostname to match new profile ──
    if [ -n "$PROFILE_DEVICE" ]; then
        hostname "$PROFILE_DEVICE" 2>/dev/null
    fi

    # ── Update native_status so Checker app sees new profile immediately ──
    STATUS_FILE="/data/adb/jorkspoofer/native_status"
    if [ -f "$STATUS_FILE" ]; then
        # Read existing boot-time values we want to preserve
        _native_ver=$(grep "^native_version=" "$STATUS_FILE" | cut -d= -f2)
        _native_vc=$(grep "^native_versioncode=" "$STATUS_FILE" | cut -d= -f2)
        _native_bt=$(grep "^native_boot_time=" "$STATUS_FILE" | cut -d= -f2-)
        _native_bw=$(grep "^native_boot_wait=" "$STATUS_FILE" | cut -d= -f2)
        _native_zp=$(grep "^native_zygisk_provider=" "$STATUS_FILE" | cut -d= -f2)
        _native_zl=$(grep "^native_zygisk_loaded=" "$STATUS_FILE" | cut -d= -f2)
        _native_mr=$(grep "^native_mounts_restored=" "$STATUS_FILE" | cut -d= -f2)
        _native_br=$(grep "^native_bst_nodes_rehidden=" "$STATUS_FILE" | cut -d= -f2)
        # Preserve verification results
        _v_net=$(grep "^verify_network_interfaces=" "$STATUS_FILE" | cut -d= -f2)
        _v_bst=$(grep "^verify_bst_devices=" "$STATUS_FILE" | cut -d= -f2)
        _v_cfg=$(grep "^verify_config_access=" "$STATUS_FILE" | cut -d= -f2)
        _v_zyg=$(grep "^verify_zygisk=" "$STATUS_FILE" | cut -d= -f2)

        # Re-verify hostname and timezone for new profile
        _cur_host=$(hostname 2>/dev/null)
        _cur_tz=$(getprop persist.sys.timezone 2>/dev/null)
        _v_host="fail"; [ "$_cur_host" = "$PROFILE_DEVICE" ] && _v_host="pass"
        _v_tz="fail"; [ "$_cur_tz" = "$PROFILE_TIMEZONE" ] && _v_tz="pass"

        # Recount score
        _pass=0; _total=6
        for _v in $_v_host $_v_tz $_v_net $_v_bst $_v_cfg $_v_zyg; do
            case "$_v" in pass|hook) _pass=$((_pass + 1)) ;; skip) _total=$((_total - 1)) ;; esac
        done
        _v_score="${_pass}/${_total}"
        [ "$_pass" -eq "$_total" ] && _native_state="healthy" || _native_state="degraded"

        {
            echo "# Luke's Mirage — Native Layer Status"
            echo "# Updated: $(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 'unknown')"
            echo ""
            echo "# ── Module Info ──"
            echo "native_version=${_native_ver:-v2.0.0}"
            echo "native_versioncode=${_native_vc:-20}"
            echo "native_state=$_native_state"
            echo "native_boot_time=${_native_bt:-unknown}"
            echo "native_boot_wait=${_native_bw:-0s}"
            echo ""
            echo "# ── Active Profile ──"
            echo "native_config=/data/local/tmp/jorkspoofer_active.conf"
            echo "native_profile_name=$PROFILE_NAME"
            echo "native_profile_model=$PROFILE_MODEL"
            echo "native_profile_brand=$PROFILE_BRAND"
            echo "native_profile_device=$PROFILE_DEVICE"
            echo "native_profile_gl_renderer=${PROFILE_GL_RENDERER:-unknown}"
            echo "native_profile_gl_vendor=${PROFILE_GL_VENDOR:-unknown}"
            echo ""
            echo "# ── Runtime Values ──"
            echo "native_hostname=$(hostname 2>/dev/null)"
            echo "native_timezone=$(getprop persist.sys.timezone 2>/dev/null)"
            echo "native_zygisk_provider=${_native_zp:-ReZygisk}"
            echo "native_zygisk_loaded=${_native_zl:-1}"
            echo "native_mounts_restored=${_native_mr:-0}"
            echo "native_bst_nodes_rehidden=${_native_br:-0}"
            echo ""
            echo "# ── Verification Results ──"
            echo "verify_hostname=$_v_host"
            echo "verify_timezone=$_v_tz"
            echo "verify_network_interfaces=${_v_net:-hook}"
            echo "verify_bst_devices=${_v_bst:-pass}"
            echo "verify_config_access=${_v_cfg:-pass}"
            echo "verify_zygisk=${_v_zyg:-pass}"
            echo "verify_score=$_v_score"
        } > "$STATUS_FILE" 2>/dev/null
        chmod 0644 "$STATUS_FILE" 2>/dev/null
    fi

    model=$(getprop ro.product.model)
    echo "OK|${profile_name}|${model}"

else
    echo "Usage: sh $0 list|current|apply <profile-name> [--restart-zygote]"
    exit 1
fi
