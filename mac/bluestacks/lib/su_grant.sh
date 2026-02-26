#!/system/bin/sh
# su_grant.sh — Auto-grant su access to Luke's Mirage apps
# Called from service.sh after boot completes.
# Resolves UIDs dynamically so it works on cloned instances
# where UIDs may differ from the original.

MAGISK_DB="/data/adb/magisk.db"

# Apps that need root access
SU_GRANT_PACKAGES="
com.jorkspoofer.checker
com.lukesmirage.statuscheck
"

auto_grant_su() {
    if [ ! -f "$MAGISK_DB" ]; then
        log_info "su_grant: magisk.db not found — skipping"
        return 1
    fi

    for pkg in $SU_GRANT_PACKAGES; do
        # Skip empty lines
        [ -z "$pkg" ] && continue

        # Look up UID from package manager
        uid=$(pm list packages -U 2>/dev/null | grep "^package:${pkg} " | sed "s/.*uid://")
        if [ -z "$uid" ]; then
            log_info "su_grant: $pkg not installed — skipping"
            continue
        fi

        # Check current policy
        current=$(sqlite3 "$MAGISK_DB" "SELECT policy FROM policies WHERE uid=$uid;" 2>/dev/null)

        if [ "$current" = "2" ]; then
            log_info "su_grant: $pkg (uid $uid) already allowed"
            continue
        fi

        # Insert or update to allow (policy=2), no notification
        if [ -z "$current" ]; then
            sqlite3 "$MAGISK_DB" "INSERT INTO policies (uid, policy, until, logging, notification) VALUES ($uid, 2, 0, 1, 0);" 2>/dev/null
            log_info "su_grant: $pkg (uid $uid) — INSERTED allow policy"
        else
            sqlite3 "$MAGISK_DB" "UPDATE policies SET policy=2, notification=0 WHERE uid=$uid;" 2>/dev/null
            log_info "su_grant: $pkg (uid $uid) — UPDATED $current → allow"
        fi
    done
}
