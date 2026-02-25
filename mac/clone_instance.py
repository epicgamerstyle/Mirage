#!/usr/bin/env python3
"""
BlueStacks Air Multi-Instance Cloner (Luke's Mirage — Mac)

Goal:
- Clone a rooted "golden" BlueStacks Air instance into one or more new instances,
  each with unique ADB ports, Android IDs, and MAC addresses.
- Re-copies the data disk from the golden source and re-registers clones on existing
  MIM-created instances so they pick up new Magisk/Zygisk modules.

How it works on Mac / BlueStacks Air:
- BlueStacks Air uses Apple Hypervisor.framework + QEMU (not VirtualBox).
- Each instance is a folder (Tiramisu64, Tiramisu64_1, Tiramisu64_2, ...) containing
  only data.qcow2 (user data disk) — system images are shared from the app bundle.
- There are NO .bstk XML configs, NO BstkGlobal.xml, NO VirtualBox UUID patching.
- All configuration lives in bluestacks.conf (same key=value format as Windows).
- MimMetaData.json is at the same relative path: Engine/UserData/MimMetaData.json.

Flags:
  --source NAME          Source instance name to clone from (e.g. Tiramisu64). Required.
  --clone NAME [NAME..]  One or more target clone names (e.g. Tiramisu64_4 Tiramisu64_5). Required.
  --engine-dir DIR       Engine directory containing instance folders. Auto-detected if omitted.
  --bluestacks-dir DIR   BlueStacks Air app contents directory.
  --dry-run              Show what would be done without making any changes.

Examples:
  # Re-copy data disk from golden source to MIM clones
  python clone_instance.py --source Tiramisu64 --clone Tiramisu64_4 Tiramisu64_5

  # Preview what would be done
  python clone_instance.py --source Tiramisu64 --clone Tiramisu64_test --dry-run

This script is for macOS only (BlueStacks Air). For Windows BlueStacks 5, use the
Windows version in the parent directory.
BlueStacks must be fully closed before running.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

BLUESTACKS_PROCESSES = ["BlueStacks"]
DEFAULT_BLUESTACKS_DIR = "/Applications/BlueStacks.app/Contents"
DEFAULT_ENGINE_DIR = "/Users/Shared/Library/Application Support/BlueStacks/Engine"
DEFAULT_DATA_DIR = "/Users/Shared/Library/Application Support/BlueStacks"
DISK_FILES = {"data.qcow2"}

# Keys that must be unique per instance (regenerated, not copied from source)
# Keys that should be reset to fresh/empty state for new clones
CONF_RESET_KEYS = {
    "boot_duration": "0",
    "google_account_logins": "",
    "google_login_popup_shown": "0",
    "first_boot": "1",
    "launch_date": "",
    "app_launch_count": "0",
    "gl_win_x": "-1",
    "gl_win_y": "-1",
    # gl_win_height removed — hardcoded to 540 in update_bluestacks_conf()
    "gl_win_screen": "",
    "macro_win_x": "-1",
    "macro_win_y": "-1",
    "macro_win_height": "-1",
    "macro_win_screen": "",
    "nowgg_email": "",
    "nowgg_userAvatarUrl": "",
    "nowgg_userId": "",
    "nowgg_username": "",
    "nowbux_signin_completed": "0",
    "token": "",
    "refresh_token": "",
    "status.session_id": "0",
    "ads_display_time": "",
}


def detect_engine_dir():
    """Auto-detect engine directory on macOS."""
    engine = Path(DEFAULT_ENGINE_DIR)
    if engine.is_dir():
        return engine
    return None


def check_bluestacks_stopped(dry_run=False):
    """Verify no BlueStacks processes are running."""
    if dry_run:
        print("[dry-run] Would check for running BlueStacks processes")
        return
    try:
        for proc in BLUESTACKS_PROCESSES:
            result = subprocess.run(
                ["pgrep", "-x", proc],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                print(f"ERROR: BlueStacks process '{proc}' is still running.")
                print("Stop all BlueStacks instances before cloning.")
                print("  Use: pkill -x BlueStacks")
                sys.exit(1)
    except FileNotFoundError:
        print("WARNING: 'pgrep' not found — are you running this on macOS?")
        sys.exit(1)


def copy_with_progress(src, dst, desc="", quiet=False):
    """Copy a file with progress indicator for large files."""
    size = src.stat().st_size
    copied = 0
    chunk_size = 4 * 1024 * 1024  # 4 MB chunks
    last_pct = -1

    label = desc or src.name
    if size == 0:
        shutil.copy2(src, dst)
        return

    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            buf = fsrc.read(chunk_size)
            if not buf:
                break
            fdst.write(buf)
            copied += len(buf)
            pct = copied * 100 // size
            if quiet:
                if pct >= last_pct + 25 or pct == 100:
                    print(f"Copying {label} — {pct}%")
                    last_pct = pct
            else:
                mb_done = copied // (1024 * 1024)
                mb_total = size // (1024 * 1024)
                print(f"\r  Copying {label}: {mb_done}/{mb_total} MB ({pct}%)", end="", flush=True)
    if not quiet:
        print()
    shutil.copystat(src, dst)


def copy_disk_files(source_dir, clone_dir, dry_run, quiet=False):
    """Copy data.qcow2 from source to clone directory."""
    src = source_dir / "data.qcow2"
    dst = clone_dir / "data.qcow2"
    if not src.exists():
        print(f"ERROR: Source disk not found: {src}")
        sys.exit(1)
    if dry_run:
        size_mb = src.stat().st_size // (1024 * 1024)
        print(f"[dry-run] Would copy data.qcow2 ({size_mb} MB)")
        return
    copy_with_progress(src, dst, "data.qcow2", quiet=quiet)


def copy_non_disk_payload(source_dir, clone_dir, dry_run, quiet=False):
    """Copy non-disk instance payload (AppCache/, Flyers/, etc.) for full clones."""
    payload_items = []
    for src in sorted(source_dir.iterdir(), key=lambda p: p.name.lower()):
        if src.name in DISK_FILES:
            continue
        # Skip log files that are instance-specific
        if src.name == "qvirt.log":
            continue
        payload_items.append(src)

    for src in payload_items:
        dst = clone_dir / src.name
        if dry_run:
            kind = "dir" if src.is_dir() else "file"
            print(f"[dry-run] Would copy {kind}: {src.name}")
            continue

        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            if quiet:
                print(f"Copying {src.name}/...")
            else:
                print(f"  Copied dir: {src.name}/")
        else:
            shutil.copy2(src, dst)
            if quiet:
                print(f"Copying {src.name}...")
            else:
                print(f"  Copied file: {src.name}")


# ---------------------------------------------------------------------------
# bluestacks.conf handling
# ---------------------------------------------------------------------------

def parse_conf(conf_path):
    """Parse bluestacks.conf into an ordered list of (key, value) tuples."""
    lines = []
    with open(conf_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n\r")
            if "=" in line:
                key, _, val = line.partition("=")
                lines.append((key, val))
            else:
                lines.append((line, None))  # blank or comment lines
    return lines


def write_conf(conf_path, lines):
    """Write bluestacks.conf from ordered list of (key, value) tuples."""
    with open(conf_path, "w", encoding="utf-8") as f:
        for key, val in lines:
            if val is None:
                f.write(key + "\n")
            else:
                f.write(f"{key}={val}\n")


def get_conf_value(lines, key):
    """Get a value from the conf lines by key."""
    for k, v in lines:
        if k == key and v is not None:
            return v.strip('"')
    return None


def set_conf_value(lines, key, value):
    """Set a value in the conf lines. Returns True if key existed."""
    for i, (k, v) in enumerate(lines):
        if k == key:
            lines[i] = (key, f'"{value}"')
            return True
    return False


def find_all_adb_ports(lines):
    """Scan conf for all existing adb_port values."""
    ports = []
    for key, val in lines:
        if val is not None and key.endswith(".adb_port") and key.startswith("bst.instance."):
            try:
                ports.append(int(val.strip('"')))
            except ValueError:
                pass
    return ports


def find_next_display_number(lines):
    """Find the next display_name number by scanning existing instances."""
    numbers = []
    pat = re.compile(r"BlueStacks Air (\d+)")
    for key, val in lines:
        if val is not None and key.endswith(".display_name"):
            m = pat.search(val.strip('"'))
            if m:
                numbers.append(int(m.group(1)))
    return max(numbers, default=0) + 1


def find_all_instance_vm_ids(lines):
    """Collect numeric vm_id values from all instance blocks."""
    vm_ids = set()
    for key, val in lines:
        if val is None:
            continue
        if not key.startswith("bst.instance.") or not key.endswith(".vm_id"):
            continue
        try:
            vm_ids.add(int(val.strip('"')))
        except ValueError:
            pass
    return vm_ids


def find_next_vm_id(lines):
    """Pick the next vm_id based on existing per-instance vm_id values and bst.next_vm_id."""
    candidates = []

    vm_ids = find_all_instance_vm_ids(lines)
    if vm_ids:
        candidates.append(max(vm_ids) + 1)

    current_next = get_conf_value(lines, "bst.next_vm_id")
    if current_next is not None:
        try:
            candidates.append(int(current_next))
        except ValueError:
            pass

    return max(candidates, default=1)


def generate_local_mac():
    """Generate a locally-administered unicast MAC address."""
    mac = bytearray(secrets.token_bytes(6))
    mac[0] = (mac[0] | 0x02) & 0xFE
    return ":".join(f"{b:02x}" for b in mac)


def is_mac_suffix(suffix):
    """Best-effort match for MAC-related bluestacks.conf suffixes."""
    s = suffix.lower()
    if "macro" in s:
        return False
    explicit = {
        "mac", "mac_address", "status.mac_address",
        "wifi_mac", "wifi.mac", "wifi_mac_address",
        "bluetooth_mac", "bt_mac", "bt.mac", "eth_mac", "ethernet_mac",
    }
    if s in explicit:
        return True
    return (
        s.endswith(".mac")
        or s.endswith("_mac")
        or s.endswith(".mac_address")
        or s.endswith("_mac_address")
    )


def assign_clone_mac_values(clone_block):
    """Assign fresh MAC values to all discovered MAC fields.

    On BlueStacks Air (Mac), instances do NOT store MAC addresses in
    bluestacks.conf — QEMU assigns them at runtime.  If the source
    instance has no MAC fields we must NOT inject a spurious
    ``mac_address`` key; doing so causes BlueStacks to reject the
    conf as corrupted.
    """
    mac_assignments = {}
    for suffix in sorted(clone_block.keys()):
        if is_mac_suffix(suffix):
            mac_assignments[suffix] = generate_local_mac()

    # Only write back if the source actually had MAC fields.
    for suffix, value in mac_assignments.items():
        clone_block[suffix] = value
    return mac_assignments


def extract_instance_block(lines, instance_name):
    """Extract all keys for a given instance as a dict."""
    prefix = f"bst.instance.{instance_name}."
    block = {}
    for key, val in lines:
        if key.startswith(prefix) and val is not None:
            suffix = key[len(prefix):]
            block[suffix] = val.strip('"')
    return block


def remove_instance_block(lines, instance_name):
    """Remove all keys for a given instance from lines. Returns new list."""
    prefix = f"bst.instance.{instance_name}."
    return [(k, v) for k, v in lines if not k.startswith(prefix)]


def insert_instance_block(lines, instance_name, block):
    """Insert an instance block into lines, placed after the last existing instance block."""
    prefix = f"bst.instance.{instance_name}."

    last_instance_idx = -1
    for i, (key, _) in enumerate(lines):
        if key.startswith("bst.instance."):
            last_instance_idx = i

    new_lines = []
    for suffix in sorted(block.keys()):
        new_lines.append((f"{prefix}{suffix}", f'"{block[suffix]}"'))

    if last_instance_idx >= 0:
        return lines[:last_instance_idx + 1] + new_lines + lines[last_instance_idx + 1:]
    else:
        return lines + new_lines


def update_bluestacks_conf(conf_path, source_name, clone_name, fix_mode, dry_run, display_name_override=None):
    """Add/update clone's instance block in bluestacks.conf."""
    if not conf_path.is_file():
        print(f"  WARNING: bluestacks.conf not found at {conf_path}")
        print(f"  Skipping conf registration — BlueStacks will auto-generate defaults on first boot.")
        return

    lines = parse_conf(conf_path)

    # In fix mode, if the clone already has conf entries (e.g. created via MIM),
    # leave them alone — BS's own registration is authoritative.
    if fix_mode:
        existing = extract_instance_block(lines, clone_name)
        if existing:
            print(f"  Clone '{clone_name}' already configured in bluestacks.conf (keeping existing)")
            return

    # Check if clone already has entries
    existing_block = extract_instance_block(lines, clone_name)
    if existing_block and not fix_mode:
        print(f"  Clone '{clone_name}' already exists in bluestacks.conf (skipping)")
        print(f"  Use --fix to overwrite.")
        return

    # Copy source instance block as base
    source_block = extract_instance_block(lines, source_name)
    if not source_block:
        print(f"  WARNING: Source '{source_name}' not found in bluestacks.conf")
        print(f"  Skipping conf registration.")
        return

    # Start with source's settings (preserves cpus, ram, dpi, graphics, etc.)
    clone_block = dict(source_block)

    # --- Unique identity fields ---
    # ADB port: find highest existing port, add 10
    all_ports = find_all_adb_ports(lines)
    next_port = max(all_ports, default=5555) + 10
    clone_block["adb_port"] = str(next_port)
    clone_block["status.adb_port"] = str(next_port)

    # Display name
    next_num = find_next_display_number(lines)
    if display_name_override:
        clone_block["display_name"] = display_name_override
    else:
        clone_block["display_name"] = f"BlueStacks Air {next_num}"

    # Android identity — new unique values
    clone_block["android_google_ad_id"] = str(uuid.uuid4())
    clone_block["android_id"] = secrets.token_hex(8)

    # vm_id assignment — only if source had one (Mac/BlueStacks Air doesn't use vm_id)
    clone_vm_id = None
    vm_id_suffixes = [k for k in clone_block if k == "vm_id" or k.endswith(".vm_id")]
    if vm_id_suffixes:
        clone_vm_id = find_next_vm_id(lines)
        for key in vm_id_suffixes:
            clone_block[key] = str(clone_vm_id)

    # MAC assignment/randomization (no-op on Mac — QEMU assigns MACs at runtime)
    mac_assignments = assign_clone_mac_values(clone_block)
    first_mac = next(iter(mac_assignments.values()), None)

    # --- Reset transient/session/account state ---
    for key, default_val in CONF_RESET_KEYS.items():
        clone_block[key] = default_val

    # --- Hardcode resolution for all clones ---
    # Every clone MUST be 894x540 @ 240 dpi. Do not copy from source or use
    # BS defaults — the spoofing workflow requires this exact resolution.
    clone_block["fb_width"] = "894"
    clone_block["fb_height"] = "540"
    clone_block["dpi"] = "240"
    clone_block["gl_win_height"] = "540"

    # --- Ensure root + ADB access are enabled ---
    # Magisk/Zygisk requires root access. Source instance may have "0" by
    # default, which silently blocks all su/root operations.
    clone_block["enable_root_access"] = "1"

    if dry_run:
        print(f"  [dry-run] Would add {len(clone_block)} keys to bluestacks.conf:")
        print(f"    display_name = {clone_block['display_name']}")
        print(f"    adb_port     = {clone_block['adb_port']}")
        print(f"    android_id   = {clone_block['android_id']}")
        print(f"    ad_id        = {clone_block['android_google_ad_id']}")
        if clone_vm_id is not None:
            print(f"    vm_id        = {clone_vm_id}")
        if first_mac:
            if len(mac_assignments) == 1:
                print(f"    mac          = {first_mac}")
            else:
                print(f"    mac_fields   = {len(mac_assignments)} (e.g. {first_mac})")
        return

    # Remove old clone entries if --fix
    if existing_block:
        lines = remove_instance_block(lines, clone_name)

    # Insert new block
    lines = insert_instance_block(lines, clone_name, clone_block)

    # Bump bst.next_vm_id only if vm_id was used
    if clone_vm_id is not None:
        next_global_vm_id = clone_vm_id + 1
        if not set_conf_value(lines, "bst.next_vm_id", str(next_global_vm_id)):
            lines.append(("bst.next_vm_id", f'"{next_global_vm_id}"'))

    # --- Add clone to bst.installed_images ---
    # MIM uses this list to discover instances and look up their image traits.
    # Missing entries cause MIM to skip the instance entirely.
    for i, (key, val) in enumerate(lines):
        if key == "bst.installed_images":
            current = val.strip('"')
            names = [n.strip() for n in current.split(",") if n.strip()]
            if clone_name not in names:
                names.append(clone_name)
            lines[i] = ("bst.installed_images", f'"{",".join(names)}"')
            break
    else:
        # Key doesn't exist at all — create it with source + clone
        lines.append(("bst.installed_images", f'"{source_name},{clone_name}"'))

    # --- Ensure global ADB/root access keys are enabled ---
    # BS may write these global keys as "0" by default, blocking ADB
    # connections and root shells. Only modify if the key already exists
    # (NEVER add keys — BS validates every key on boot).
    set_conf_value(lines, "bst.enable_adb_access", "1")
    set_conf_value(lines, "enable_root_access", "1")

    write_conf(conf_path, lines)
    print(f"  Updated bluestacks.conf:")
    print(f"    display_name = {clone_block['display_name']}")
    print(f"    adb_port     = {clone_block['adb_port']}")
    print(f"    android_id   = {clone_block['android_id']}")
    if clone_vm_id is not None:
        print(f"    vm_id        = {clone_vm_id}")
    if first_mac:
        if len(mac_assignments) == 1:
            print(f"    mac          = {first_mac}")
        else:
            print(f"    mac_fields   = {len(mac_assignments)}")


# ---------------------------------------------------------------------------
# MimMetaData.json handling
# ---------------------------------------------------------------------------

def _next_mim_org_id(org_entries):
    ids = []
    for entry in org_entries:
        if not isinstance(entry, dict):
            continue
        try:
            ids.append(int(entry.get("ID")))
        except (TypeError, ValueError):
            pass
    return (max(ids) + 1) if ids else 1


def update_mim_metadata(mim_path, clone_name, dry_run, display_name=None):
    """Register a new instance in MimMetaData.json Organization list.

    Args:
        mim_path:     Path to MimMetaData.json
        clone_name:   Engine folder name (e.g. Tiramisu64_14) — used for InstanceName
        dry_run:      If True, print what would happen without writing
        display_name: Human-readable name shown in MIM (e.g. "My Clone").
                      Falls back to clone_name if not provided.
    """
    if not mim_path.is_file():
        print(f"  WARNING: MimMetaData.json not found at {mim_path} (skipping)")
        return

    try:
        with open(mim_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  WARNING: Failed to read MimMetaData.json: {e}")
        print("  Skipping MIM metadata update.")
        return

    org = data.get("Organization")
    if not isinstance(org, list):
        org = []
        data["Organization"] = org

    for entry in org:
        if isinstance(entry, dict) and entry.get("InstanceName") == clone_name:
            print(f"  MimMetaData.json already contains '{clone_name}' (skipping)")
            return

    # "Name" is what MIM displays; "InstanceName" is the engine folder name
    shown_name = display_name if display_name else clone_name
    new_entry = {
        "ID": _next_mim_org_id(org),
        "Name": shown_name,
        "IsFolder": False,
        "ParentFolder": -1,
        "IsOpen": False,
        "IsVisible": True,
        "InstanceName": clone_name,
    }

    if dry_run:
        print("  [dry-run] Would append to MimMetaData.json Organization:")
        print(f"    ID={new_entry['ID']} Name={new_entry['Name']} InstanceName={clone_name}")
        return

    org.append(new_entry)
    try:
        with open(mim_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.write("\n")
        # Ensure file stays world-writable (BS convention is 666;
        # fresh installs sometimes create it with 644 which blocks
        # future writes from other processes).
        os.chmod(str(mim_path), 0o666)
    except OSError as e:
        print(f"  WARNING: Failed to write MimMetaData.json: {e}")
        return

    if display_name and display_name != clone_name:
        print(f"  Updated MimMetaData.json: added '{clone_name}' as '{shown_name}' (ID={new_entry['ID']})")
    else:
        print(f"  Updated MimMetaData.json: added '{clone_name}' (ID={new_entry['ID']})")


def remove_mim_metadata(mim_path, instance_name, dry_run):
    """Remove an instance from MimMetaData.json Organization list."""
    if not mim_path.is_file():
        print(f"  WARNING: MimMetaData.json not found at {mim_path} (skipping)")
        return

    try:
        with open(mim_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  WARNING: Failed to read MimMetaData.json: {e}")
        return

    org = data.get("Organization")
    if not isinstance(org, list):
        print(f"  MimMetaData.json has no Organization list (skipping)")
        return

    new_org = [e for e in org if not (isinstance(e, dict) and e.get("InstanceName") == instance_name)]
    removed = len(org) - len(new_org)

    if removed == 0:
        print(f"  MimMetaData.json: '{instance_name}' not found (already removed)")
        return

    if dry_run:
        print(f"  [dry-run] Would remove '{instance_name}' from MimMetaData.json")
        return

    data["Organization"] = new_org
    try:
        with open(mim_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.write("\n")
    except OSError as e:
        print(f"  WARNING: Failed to write MimMetaData.json: {e}")
        return

    print(f"  Removed '{instance_name}' from MimMetaData.json")


def delete_instance(instance_name, engine_dir, dry_run, quiet=False):
    """Delete a single BlueStacks instance.

    Steps:
    1. Remove instance block from bluestacks.conf
    2. Remove entry from MimMetaData.json
    3. Delete the engine folder (Tiramisu64_xx/)

    Caller is responsible for:
    - Unlocking/re-locking bluestacks.conf (uchg flag)
    - Stopping/restarting MIM
    - Ensuring BlueStacks is not running this instance
    """
    engine = Path(engine_dir)
    conf_path = engine.parent / "bluestacks.conf"
    mim_meta_path = engine / "UserData" / "MimMetaData.json"
    instance_dir = engine / instance_name

    if not quiet:
        print(f"\n{'='*60}")
        print(f"Deleting instance: {instance_name}")
        print(f"{'='*60}")

    # Step 1: Remove from bluestacks.conf
    if conf_path.is_file():
        if quiet:
            print(f"Removing {instance_name} from bluestacks.conf...")
        else:
            print(f"\nRemoving from bluestacks.conf...")
        lines = parse_conf(conf_path)
        block = extract_instance_block(lines, instance_name)
        if block:
            new_lines = remove_instance_block(lines, instance_name)
            if dry_run:
                print(f"  [dry-run] Would remove {len(block)} keys for '{instance_name}'")
            else:
                write_conf(conf_path, new_lines)
                print(f"  Removed {len(block)} keys for '{instance_name}'")
        else:
            print(f"  '{instance_name}' not found in bluestacks.conf (already clean)")
    else:
        print(f"  WARNING: bluestacks.conf not found at {conf_path}")

    # Step 2: Remove from MimMetaData.json
    if quiet:
        print(f"Removing {instance_name} from MimMetaData.json...")
    else:
        print(f"\nRemoving from MimMetaData.json...")
    remove_mim_metadata(mim_meta_path, instance_name, dry_run)

    # Step 3: Delete the engine folder
    if instance_dir.is_dir():
        if quiet:
            print(f"Deleting engine folder...")
        else:
            print(f"\nDeleting engine folder: {instance_dir}")

        if dry_run:
            # Calculate size for reporting
            total_size = sum(f.stat().st_size for f in instance_dir.rglob('*') if f.is_file())
            size_mb = total_size // (1024 * 1024)
            print(f"  [dry-run] Would delete {instance_dir} ({size_mb} MB)")
        else:
            shutil.rmtree(instance_dir)
            print(f"  Deleted {instance_dir}")
    else:
        print(f"  Engine folder not found: {instance_dir} (already deleted)")

    if not quiet:
        print(f"\nDone! {instance_name} removed.")


# ---------------------------------------------------------------------------
# Main clone logic
# ---------------------------------------------------------------------------

def ensure_file_editable(path, label, required=True):
    """Check that a file exists and is writable (openable in r+b mode).

    If required=True (default), aborts with sys.exit(1) on failure.
    If required=False, prints a warning and returns False.
    Returns True if the file is editable.
    """
    if not path.is_file():
        # Try fixing directory permissions first — fresh BS installs sometimes
        # create files with restrictive permissions that make is_file() fail.
        try:
            parent = path.parent
            if parent.is_dir():
                os.chmod(str(parent), 0o777)
                os.chmod(str(path), 0o666)
        except Exception:
            pass
        # Re-check after permission fix attempt
        if not path.is_file():
            if required:
                print(f"ERROR: Required file not found: {path}")
                print(f"  Missing required {label}.")
                sys.exit(1)
            else:
                print(f"  WARNING: {label} not found at {path} (skipping)")
                return False
    try:
        # Ensure the file is world-read-writable (BS convention is 666)
        os.chmod(str(path), 0o666)
    except Exception:
        pass
    try:
        with open(path, "r+b"):
            pass
    except OSError as e:
        if required:
            print(f"ERROR: Required file is not editable: {path}")
            print(f"  Cannot edit {label}: {e}")
            sys.exit(1)
        else:
            print(f"  WARNING: {label} is not writable: {e} (skipping)")
            return False
    return True


# ---------------------------------------------------------------------------
# Base-image template — the complete bluestacks.conf block for a new instance.
# Generated from a known-good "Base test 2" (Tiramisu64_2) instance.
# All identity fields (android_id, google_ad_id, adb_port, display_name)
# are placeholders that get replaced at creation time.
# ---------------------------------------------------------------------------

_BASE_INSTANCE_TEMPLATE: dict[str, str] = {
    "abi_list": "arm64",
    "ads_app_activity": "com.uncube.gamevantage.ui.activities.MainActivity",
    "ads_app_package": "com.uncube.gamevantage",
    "ads_display_time": "",
    "ads_limit_min_pixels": "900",
    "ads_screen_width": "178",
    "ads_screen_width_percentage": "20",
    "airplane_mode_active": "0",
    "airplane_mode_active_time": "",
    "android_sound_while_tapping": "1",
    "app_launch_count": "0",
    "astc_decoding_mode": "software",
    "autohide_notifications": "0",
    "boot_duration": "0",
    "camera_backend": "ffmpeg",
    "camera_device": "Windows default",
    "camera_rotation_angle": "0",
    "cpus": "4",
    "custom_resolution_selected": "1",
    "device_carrier_code": "se_310410",
    "device_country_code": "840",
    "device_custom_brand": "",
    "device_custom_manufacturer": "",
    "device_custom_model": "",
    "device_profile_code": "stou",
    "dpi": "240",
    "eco_mode_max_fps": "5",
    "enable_fps_display": "0",
    "enable_fullscreen_all_apps": "0",
    "enable_high_fps": "0",
    "enable_logcat_redirection": "0",
    "enable_notifications": "1",
    "enable_root_access": "1",
    "enable_vsync": "1",
    "fb_height": "540",
    "fb_width": "894",
    "first_boot": "0",
    "game_controls_enabled": "1",
    "gl_win_height": "540",
    "gl_win_screen": "",
    "gl_win_x": "-1",
    "gl_win_y": "-1",
    "google_account_logins": "",
    "google_login_popup_shown": "0",
    "graphics_engine": "aga",
    "graphics_renderer": "vlcn",
    "grm_ignored_rules": "",
    "launch_date": "",
    "libc_mem_allocator": "",
    "macro_win_height": "-1",
    "macro_win_screen": "",
    "macro_win_x": "-1",
    "macro_win_y": "-1",
    "max_fps": "60",
    "mic_enabled": "0",
    "nowbux_signin_completed": "0",
    "nowgg_email": "",
    "nowgg_userAvatarUrl": "",
    "nowgg_userId": "",
    "nowgg_username": "",
    "pin_to_top": "0",
    "ram": "4096",
    "refresh_token": "",
    "show_nowbux_rewards_red_dot_onboarding": "1",
    "show_sidebar": "1",
    "split_ad_enabled": "0",
    "split_ad_show_times": "-1",
    "token": "",
    "vulkan_supported": "1",
    # Identity fields — filled at creation time:
    "display_name": "",
    "android_id": "",
    "android_google_ad_id": "",
    "adb_port": "",
    "status.adb_port": "",
    "status.session_id": "0",
    "status.ip_addr_prefix_len": "24",
    "status.ip_gateway_addr": "10.0.2.2",
    "status.ip_guest_addr": "10.0.2.15",
}


def create_instance_from_base(
    base_image_path,
    instance_name,
    display_name,
    engine_dir=None,
    quiet=False,
    on_progress=None,
):
    """Create a brand-new BlueStacks instance from a pre-built base image.

    This is the *turnkey* path for initial setup — no existing BlueStacks
    instance or MIM is required.  The function:

      1. Creates the Engine/<instance_name>/ directory
      2. Copies/decompresses base-image.qcow2 → data.qcow2
      3. Creates stub subdirectories BlueStacks expects
      4. Generates a full bluestacks.conf block with unique identity
      5. Adds the instance to bst.installed_images
      6. Registers the instance in MimMetaData.json

    Args:
        base_image_path: Path to the compressed base-image.qcow2.
        instance_name:   Engine folder name (e.g. "Tiramisu64").
        display_name:    Human-readable name shown in MIM & GUI.
        engine_dir:      Override for Engine directory; auto-detected if None.
        quiet:           Suppress verbose output.
        on_progress:     Optional callback(msg: str) for GUI progress updates.

    Returns:
        {"ok": True, "instance_name": ..., "adb_port": ..., "path": ...}
        or {"ok": False, "error": "..."}
    """
    def _emit(msg):
        if on_progress:
            on_progress(msg)
        if not quiet:
            print(f"  [base-create] {msg}")

    try:
        base_image_path = Path(base_image_path)
        if not base_image_path.is_file():
            return {"ok": False, "error": f"Base image not found: {base_image_path}"}

        # Auto-detect engine directory
        if engine_dir:
            engine_dir = Path(engine_dir)
        else:
            engine_dir = detect_engine_dir()
            if not engine_dir:
                return {"ok": False, "error": "Could not detect BlueStacks Engine directory"}

        conf_path = engine_dir.parent / "bluestacks.conf"
        mim_meta_path = engine_dir / "UserData" / "MimMetaData.json"

        # Validate bluestacks.conf exists (BlueStacks must be installed)
        if not conf_path.is_file():
            return {"ok": False, "error": f"bluestacks.conf not found at {conf_path} — is BlueStacks installed?"}

        # Check bluestacks.conf is writable
        try:
            with open(conf_path, "r+b"):
                pass
        except OSError as e:
            return {"ok": False, "error": f"bluestacks.conf is not writable: {e}"}

        instance_dir = engine_dir / instance_name

        # Idempotency: don't overwrite existing instances
        if instance_dir.is_dir() and (instance_dir / "data.qcow2").is_file():
            return {"ok": False, "error": f"Instance '{instance_name}' already exists at {instance_dir}"}

        # ── Step 1: Create directory structure ──
        _emit(f"Creating instance directory: {instance_name}")
        instance_dir.mkdir(parents=True, exist_ok=True)

        # BlueStacks expects these subdirectories even if empty
        for subdir in ["AppCache", "Flyers", "Onboardings", "Promotions", "nowBux", "topbar"]:
            (instance_dir / subdir).mkdir(exist_ok=True)

        # ── Step 2: Copy base image ──
        _emit("Copying base image (this may take a moment)...")
        dst_qcow2 = instance_dir / "data.qcow2"
        copy_with_progress(base_image_path, dst_qcow2, "data.qcow2", quiet=quiet)
        _emit(f"Image copied ({dst_qcow2.stat().st_size // (1024*1024)} MB)")

        # ── Step 3: Generate unique identity ──
        lines = parse_conf(conf_path)

        # Find next available ADB port (highest existing + 10)
        all_ports = find_all_adb_ports(lines)
        adb_port = max(all_ports, default=5555) + 10

        # Build instance block from template
        block = dict(_BASE_INSTANCE_TEMPLATE)
        block["display_name"] = display_name
        block["android_id"] = secrets.token_hex(8)
        block["android_google_ad_id"] = str(uuid.uuid4())
        block["adb_port"] = str(adb_port)
        block["status.adb_port"] = str(adb_port)

        _emit(f"Assigned ADB port {adb_port}, android_id {block['android_id'][:8]}...")

        # ── Step 4: Update bluestacks.conf ──
        _emit("Registering in bluestacks.conf...")

        # Remove any stale entries for this instance name
        existing = extract_instance_block(lines, instance_name)
        if existing:
            lines = remove_instance_block(lines, instance_name)

        # Insert the new block
        lines = insert_instance_block(lines, instance_name, block)

        # ── Step 5: Add to bst.installed_images ──
        for i, (key, val) in enumerate(lines):
            if key == "bst.installed_images":
                current = val.strip('"')
                names = [n.strip() for n in current.split(",") if n.strip()]
                if instance_name not in names:
                    names.append(instance_name)
                lines[i] = ("bst.installed_images", f'"{",".join(names)}"')
                break
        else:
            # No installed_images key exists — create it
            lines.append(("bst.installed_images", f'"{instance_name}"'))

        write_conf(conf_path, lines)
        _emit("bluestacks.conf updated ✓")

        # ── Step 6: Register in MimMetaData.json ──
        # Fix permissions first — fresh BS installs create this with 644
        # which blocks our write. BS convention is 666.
        try:
            if mim_meta_path.is_file():
                os.chmod(str(mim_meta_path), 0o666)
            elif mim_meta_path.parent.is_dir():
                os.chmod(str(mim_meta_path.parent), 0o777)
        except Exception:
            pass
        if mim_meta_path.is_file():
            _emit("Registering in MimMetaData.json...")
            update_mim_metadata(mim_meta_path, instance_name, dry_run=False,
                                display_name=display_name)
            try:
                os.chmod(str(mim_meta_path), 0o666)
            except Exception:
                pass
        else:
            _emit("MimMetaData.json not found (MIM will auto-detect)")

        _emit(f"Instance '{display_name}' ({instance_name}) created successfully!")

        return {
            "ok": True,
            "instance_name": instance_name,
            "display_name": display_name,
            "adb_port": adb_port,
            "path": str(instance_dir),
        }

    except Exception as e:
        return {"ok": False, "error": f"Instance creation failed: {e}"}


def clone_instance(source_name, clone_name, engine_dir, bluestacks_dir, fix_mode, dry_run,
                   quiet=False, source_dir_override=None, display_name_override=None):
    """Clone a single instance.

    On Mac/BlueStacks Air, cloning involves:
    1. Copy data.qcow2 from source to new instance directory
    2. Copy non-disk payload (AppCache/, Flyers/, etc.) for full clones
    3. Update bluestacks.conf with new instance block
    4. Update MimMetaData.json for MIM visibility

    No VirtualBox XML (.bstk), no BstkGlobal.xml, no disk UUID patching needed.
    """
    if not quiet:
        print(f"\n{'='*60}")
        print(f"Cloning {source_name} -> {clone_name}")
        print(f"{'='*60}")

    if source_dir_override:
        source_dir = Path(source_dir_override)
    else:
        source_dir = engine_dir / source_name

    clone_dir = engine_dir / clone_name
    conf_path = engine_dir.parent / "bluestacks.conf"
    mim_meta_path = engine_dir / "UserData" / "MimMetaData.json"

    # Validate paths
    if not source_dir.is_dir():
        print(f"ERROR: Source directory not found: {source_dir}")
        sys.exit(1)

    # Require bluestacks.conf to be editable (hard requirement).
    # MimMetaData.json is optional — MIM can auto-detect instances without it.
    ensure_file_editable(conf_path, "bluestacks.conf", required=True)
    mim_editable = ensure_file_editable(mim_meta_path, "MimMetaData.json", required=False)

    # Idempotency check
    if not fix_mode and clone_dir.is_dir():
        print(f"ERROR: Clone directory already exists: {clone_dir}")
        if not quiet:
            print("Use --fix to update an existing clone, or remove the directory first.")
        sys.exit(1)

    # Step 1: Create / verify clone directory
    if fix_mode:
        if not clone_dir.is_dir():
            print(f"ERROR: Target directory doesn't exist: {clone_dir}")
            sys.exit(1)
        if not quiet:
            print(f"\n[fix mode] Using existing directory: {clone_dir}")
    else:
        if dry_run:
            print(f"[dry-run] Would create directory: {clone_dir}")
        else:
            clone_dir.mkdir(parents=True, exist_ok=True)
            if not quiet:
                print(f"\nCreated directory: {clone_dir}")

    # Step 2: Copy data disk
    if quiet:
        pass
    else:
        print("\nCopying data disk...")
    copy_disk_files(source_dir, clone_dir, dry_run, quiet=quiet)

    # Step 2b: Full clone mode copies additional instance payload
    if not fix_mode:
        if quiet:
            print("Copying instance payload...")
        else:
            print("\nCopying non-disk instance payload...")
        copy_non_disk_payload(source_dir, clone_dir, dry_run, quiet=quiet)

    # Step 3: Update bluestacks.conf
    # (No VBox UUID patching needed on Mac — QCOW2 disks don't have embedded UUIDs)
    if quiet:
        print("Updating bluestacks.conf...")
    else:
        print("\nConfiguring bluestacks.conf...")
    update_bluestacks_conf(
        conf_path, source_name, clone_name, fix_mode, dry_run,
        display_name_override=display_name_override
    )

    # Step 4: Full clone mode — add to MIM organization metadata
    # Skip if MimMetaData.json wasn't editable (non-fatal — MIM auto-detects)
    if not fix_mode and mim_editable:
        if quiet:
            print("Updating MimMetaData.json...")
        else:
            print("\nUpdating MimMetaData.json...")
        update_mim_metadata(mim_meta_path, clone_name, dry_run,
                            display_name=display_name_override)
        # Ensure MimMetaData.json stays world-writable (BS convention)
        try:
            os.chmod(str(mim_meta_path), 0o666)
        except Exception:
            pass

    if not quiet:
        print(f"\nDone! {clone_name} is ready.")


def main():
    parser = argparse.ArgumentParser(
        description="Clone rooted BlueStacks Air instances (macOS).",
        epilog="Example: python clone_instance.py --source Tiramisu64 --clone Tiramisu64_4 Tiramisu64_5"
    )
    parser.add_argument("--source", required=True,
                        help="Source instance name (e.g. Tiramisu64)")
    parser.add_argument("--clone", required=True, nargs="+",
                        help="One or more target clone names")
    parser.add_argument("--engine-dir",
                        help="Engine directory path (auto-detected if omitted)")
    parser.add_argument("--bluestacks-dir", default=DEFAULT_BLUESTACKS_DIR,
                        help=f"BlueStacks Air app contents (default: {DEFAULT_BLUESTACKS_DIR})")
    parser.add_argument("--new", action="store_true",
                        help=argparse.SUPPRESS)  # hidden: full clone without MIM
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making changes")

    args = parser.parse_args()

    # Resolve engine dir
    if args.engine_dir:
        engine_dir = Path(args.engine_dir)
    else:
        engine_dir = detect_engine_dir()
        if engine_dir is None:
            print("ERROR: Could not auto-detect engine directory.")
            print("Expected: " + DEFAULT_ENGINE_DIR)
            print("Specify it with --engine-dir")
            sys.exit(1)

    if not engine_dir.is_dir():
        print(f"ERROR: Engine directory not found: {engine_dir}")
        sys.exit(1)

    print(f"Engine dir:     {engine_dir}")
    print(f"BlueStacks dir: {args.bluestacks_dir}")
    print(f"Source:         {args.source}")
    print(f"Clone(s):       {', '.join(args.clone)}")
    fix_mode = not args.new
    if args.new:
        print(f"Mode:           NEW (full clone, no MIM)")
    if args.dry_run:
        print(f"Mode:           DRY RUN")

    # Safety check
    check_bluestacks_stopped(args.dry_run)

    for clone_name in args.clone:
        if clone_name == args.source:
            print(f"\nERROR: Cannot clone '{clone_name}' onto itself!")
            sys.exit(1)
        clone_instance(
            args.source, clone_name, engine_dir,
            args.bluestacks_dir, fix_mode, args.dry_run
        )

    print(f"\n{'='*60}")
    print(f"All {len(args.clone)} clone(s) completed successfully!")
    print(f"Launch BlueStacks Air to verify the new instance(s) appear.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
