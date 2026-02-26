#!/bin/bash
# promo_guard.sh - Protects custom boot promotions from BlueStacks overwrites
#
# Runs as a LaunchAgent (com.lukesmirage.promoguard), checks every 30 seconds.
#
# When BlueStacks IS running:
#   - Verifies custom load.jpg is in place for ALL instances
#   - Repairs any that BlueStacks has overwritten (size mismatch)
#   - Locks all Promotions files with chflags uchg
#
# When BlueStacks is NOT running:
#   - Unlocks all Promotions files (safe for cloning/maintenance)
#
# Handles newly cloned instances automatically.

BS_ENGINE="/Users/Shared/Library/Application Support/BlueStacks/Engine"
MIRAGE_DIR="$HOME/.config/lukesmirage"
CUSTOM_IMAGE="$MIRAGE_DIR/load.jpg"
LOG_FILE="/tmp/mirage-promo-guard.log"
MAX_LOG_LINES=500

write_clean_json() {
    local target="$1"
    printf '%s\n' \
        '{' \
        '    "boot_promotion_obj": {' \
        '        "boot_promotion_display_time": 30000,' \
        '        "boot_promotion_id": "mirage-custom",' \
        '        "boot_promotion_images": [' \
        '            {' \
        '                "button_text": "",' \
        '                "extra_payload": {' \
        '                    "click_generic_action": "None",' \
        '                    "hash_tags": "#oem:nxt_mac2"' \
        '                },' \
        '                "id": "mirage_custom",' \
        '                "image_url": "BootPromotion_us_prod_2025_04_02_05_41_37.jpg",' \
        '                "order": "1",' \
        '                "promo_button_click_status_text": ""' \
        '            }' \
        '        ],' \
        '        "last_modified_time": "Wed Feb 26 00:00:00 2026"' \
        '    },' \
        '    "boot_promotion_orientation": "landscape",' \
        '    "promotion_images_display_date": {' \
        '        "BootPromotion_us_prod_2025_04_02_05_41_37.jpg": "Wed Feb 26 00:00:00 2026"' \
        '    }' \
        '}' > "$target"
}

log_msg() {
    local ts
    ts=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$ts] $1" >> "$LOG_FILE"
    local lc
    lc=$(wc -l < "$LOG_FILE" 2>/dev/null)
    if [ "$lc" -gt "$MAX_LOG_LINES" ] 2>/dev/null; then
        tail -n 200 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
    fi
}

is_bluestacks_running() {
    pgrep -qf "BlueStacks" 2>/dev/null
}

get_custom_size() {
    stat -f%z "$CUSTOM_IMAGE" 2>/dev/null
}

lock_instance() {
    local dir="$1"
    for f in "$dir"/BootPromotion_*.jpg "$dir/Promotions.json"; do
        [ -f "$f" ] && chflags uchg "$f" 2>/dev/null
    done
}

unlock_instance() {
    local dir="$1"
    for f in "$dir"/BootPromotion_*.jpg "$dir/Promotions.json"; do
        [ -f "$f" ] && chflags nouchg "$f" 2>/dev/null
    done
}

repair_instance() {
    local dir="$1"
    local instance_name
    instance_name=$(basename "$(dirname "$dir")")
    local custom_size
    custom_size=$(get_custom_size)

    if [ -z "$custom_size" ] || [ "$custom_size" = "0" ]; then
        return 1
    fi

    local needs_repair=false

    for f in "$dir"/BootPromotion_*.jpg; do
        [ -f "$f" ] || continue
        local fsize
        fsize=$(stat -f%z "$f" 2>/dev/null)
        if [ "$fsize" != "$custom_size" ]; then
            needs_repair=true
            break
        fi
    done

    if [ -f "$dir/Promotions.json" ]; then
        if ! grep -q "mirage_custom" "$dir/Promotions.json" 2>/dev/null; then
            needs_repair=true
        fi
    fi

    if $needs_repair; then
        log_msg "REPAIR: $instance_name - restoring custom promotions"
        unlock_instance "$dir"
        for f in "$dir"/BootPromotion_*.jpg; do
            [ -f "$f" ] && cp "$CUSTOM_IMAGE" "$f"
        done
        write_clean_json "$dir/Promotions.json"
        lock_instance "$dir"
        log_msg "REPAIR: $instance_name - restored and locked"
        return 0
    fi

    return 1
}

run_cycle() {
    local running
    if is_bluestacks_running; then
        running="yes"
    else
        running="no"
    fi

    for promo_dir in "$BS_ENGINE"/Tiramisu64_*/Promotions; do
        [ -d "$promo_dir" ] || continue

        if [ "$running" = "yes" ]; then
            repair_instance "$promo_dir"
            lock_instance "$promo_dir"
        else
            unlock_instance "$promo_dir"
        fi
    done
}

# --- Main ---
log_msg "Promo guard started (PID $$)"

if is_bluestacks_running; then
    log_msg "BlueStacks detected running - locking promotions"
else
    log_msg "BlueStacks not running - promotions unlocked for maintenance"
fi

prev_state=""
while true; do
    if is_bluestacks_running; then
        curr_state="running"
    else
        curr_state="stopped"
    fi

    if [ "$curr_state" != "$prev_state" ]; then
        if [ "$curr_state" = "running" ]; then
            log_msg "BlueStacks started - locking and guarding promotions"
        else
            log_msg "BlueStacks stopped - unlocking promotions for maintenance"
        fi
        prev_state="$curr_state"
    fi

    run_cycle
    sleep 30
done
