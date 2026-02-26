#!/system/bin/sh
# magisk_bootstrap.sh — First-boot setup for Magisk on BlueStacks Air
#
# This script runs at boot-complete and ensures:
# 1. Magisk binaries are in /data/adb/magisk/
# 2. The Magisk database exists with su auto-grant enabled
# 3. The stub APK is available for the Magisk Manager app
#
# After the first successful boot, this script is a no-op (checks for existing files).

MAGISK_DIR="/data/adb/magisk"
DB_DIR="/data/adb"

# Ensure directories exist
mkdir -p "$MAGISK_DIR" 2>/dev/null
chmod 700 "$DB_DIR" 2>/dev/null
chmod 755 "$MAGISK_DIR" 2>/dev/null

# Copy binaries from /sbin if not already present in /data/adb/magisk
for bin in magisk64 magisk32 magiskpolicy magiskinit busybox; do
    if [ -f "/sbin/$bin" ] && [ ! -f "$MAGISK_DIR/$bin" ]; then
        cp "/sbin/$bin" "$MAGISK_DIR/$bin"
        chmod 700 "$MAGISK_DIR/$bin"
    fi
done

# Create magisk symlink if missing
if [ ! -f "$MAGISK_DIR/magisk" ]; then
    ln -sf ./magisk64 "$MAGISK_DIR/magisk"
fi

# Create su symlink if missing
if [ ! -f "$MAGISK_DIR/su" ]; then
    ln -sf ./magisk "$MAGISK_DIR/su"
fi

# Copy stub.apk if available in boot
if [ -f "/boot/magisk/stub.apk" ] && [ ! -f "$MAGISK_DIR/stub.apk" ]; then
    cp "/boot/magisk/stub.apk" "$MAGISK_DIR/stub.apk"
    chmod 644 "$MAGISK_DIR/stub.apk"
fi

# Create/update the Magisk database with su auto-grant policy
# The database lives at /data/adb/magisk.db
MAGISK_DB="$DB_DIR/magisk.db"

if [ ! -f "$MAGISK_DB" ]; then
    # Use magisk's built-in sqlite to create the database
    # Set su_access to 2 (auto-grant) and multiuser_mode to 0 (owner only)
    /sbin/magisk --sqlite "CREATE TABLE IF NOT EXISTS settings (key TEXT, value INT, PRIMARY KEY(key))" 2>/dev/null
    /sbin/magisk --sqlite "INSERT OR REPLACE INTO settings VALUES('su_access', 2)" 2>/dev/null
    /sbin/magisk --sqlite "INSERT OR REPLACE INTO settings VALUES('multiuser_mode', 0)" 2>/dev/null
    /sbin/magisk --sqlite "INSERT OR REPLACE INTO settings VALUES('mnt_ns', 0)" 2>/dev/null
    /sbin/magisk --sqlite "INSERT OR REPLACE INTO settings VALUES('denylist', 0)" 2>/dev/null
    /sbin/magisk --sqlite "INSERT OR REPLACE INTO settings VALUES('zygisk', 0)" 2>/dev/null

    # Create policies table — grant root to ADB shell (UID 2000) and common app UIDs
    /sbin/magisk --sqlite "CREATE TABLE IF NOT EXISTS policies (uid INT, policy INT, until INT, logging INT, notification INT, PRIMARY KEY(uid))" 2>/dev/null
    # UID 2000 = shell (ADB), policy 2 = ALLOW
    /sbin/magisk --sqlite "INSERT OR REPLACE INTO policies VALUES(2000, 2, 0, 1, 0)" 2>/dev/null
    # UID 0 = root
    /sbin/magisk --sqlite "INSERT OR REPLACE INTO policies VALUES(0, 2, 0, 1, 0)" 2>/dev/null
fi

# ── Auto-grant su access to Luke's Mirage apps ──
# Runs every boot (not just first boot) so it works on cloned instances
# where UIDs change. Resolves UIDs dynamically via package manager.
SU_GRANT_PACKAGES="com.jorkspoofer.checker com.lukesmirage.statuscheck"

for pkg in $SU_GRANT_PACKAGES; do
    uid=$(pm list packages -U 2>/dev/null | grep "^package:${pkg} " | sed "s/.*uid://")
    [ -z "$uid" ] && continue
    /sbin/magisk --sqlite "INSERT OR REPLACE INTO policies VALUES($uid, 2, 0, 1, 0)" 2>/dev/null
done
