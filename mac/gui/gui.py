#!/usr/bin/env python3
"""
Luke's Mirage GUI — Mac / BlueStacks Air edition (based on jorkSpoofer).

pywebview-based frontend for clone_instance.py and randomize_instances.py (Mac versions).
Renders the HTML/CSS prototype in a native window with full Python backend integration.
All backend operations (clone, randomize, detect, scan) are exposed to JavaScript via
pywebview's js_api bridge.

Run directly:  python gui.py
With debug:    python gui.py --debug
Or via the launcher:  ./jorkSpoofer.command

Requires: pip install pywebview
"""

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import webview

# ─── Path resolution (supports both dev and PyInstaller frozen .app) ──────────
# BUNDLE_DIR = read-only assets (HTML, APKs, shell scripts, bin/)
# DATA_DIR   = writable user data (settings, downloads, cache)
if getattr(sys, 'frozen', False):
    # PyInstaller .app bundle — assets in sys._MEIPASS, writable data elsewhere
    BUNDLE_DIR = sys._MEIPASS
    DATA_DIR = os.path.join(
        os.path.expanduser("~/Library/Application Support"),
        "LukesMirage"
    )
else:
    # Development — both point to the same directory
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BUNDLE_DIR

os.makedirs(DATA_DIR, exist_ok=True)

# Keep SCRIPT_DIR as alias for BUNDLE_DIR (backward compat with submodule imports)
SCRIPT_DIR = BUNDLE_DIR
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import clone_instance
import randomize_instances
import vpn_manager

APP_TITLE = "Luke's Mirage — Instance Manager v2.1 (Mac)"
SETTINGS_FILE = os.path.join(DATA_DIR, ".jorkspoofer_gui.json")

DEBUG = False

# ─── Promo Guard auto-deploy ─────────────────────────────────────────────────
MIRAGE_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "lukesmirage")
PROMO_GUARD_PLIST_LABEL = "com.lukesmirage.promoguard"


def setup_promo_guard():
    """Deploy promo_guard + load.jpg on every launch if missing.

    The .pkg postinstall is supposed to handle this, but it can fail
    (e.g. user detection issues when running as root).  The GUI app
    runs as the real user, so this is the reliable fallback.
    """
    try:
        os.makedirs(MIRAGE_CONFIG_DIR, exist_ok=True)

        # Locate source assets — in frozen app they're in BUNDLE_DIR,
        # in dev mode they're in ../bluestacks/
        if getattr(sys, "frozen", False):
            guard_src = os.path.join(BUNDLE_DIR, "promo_guard.sh")
            image_src = os.path.join(BUNDLE_DIR, "load.jpg")
        else:
            bs_dir = os.path.join(os.path.dirname(BUNDLE_DIR), "bluestacks")
            guard_src = os.path.join(bs_dir, "lib", "promo_guard.sh")
            image_src = os.path.join(bs_dir, "load.jpg")

        guard_dst = os.path.join(MIRAGE_CONFIG_DIR, "promo_guard.sh")
        image_dst = os.path.join(MIRAGE_CONFIG_DIR, "load.jpg")

        # Always refresh both files so updates propagate
        if os.path.isfile(guard_src):
            shutil.copy2(guard_src, guard_dst)
            os.chmod(guard_dst, 0o755)
        if os.path.isfile(image_src):
            shutil.copy2(image_src, image_dst)

        # ── Ensure LaunchAgent is installed and loaded ──
        launch_agents = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
        os.makedirs(launch_agents, exist_ok=True)
        plist_path = os.path.join(launch_agents, f"{PROMO_GUARD_PLIST_LABEL}.plist")

        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PROMO_GUARD_PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{guard_dst}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/mirage-promo-guard-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mirage-promo-guard-stderr.log</string>
</dict>
</plist>"""

        # Write/update plist
        write_plist = True
        if os.path.isfile(plist_path):
            with open(plist_path, "r") as f:
                if f.read().strip() == plist_content.strip():
                    write_plist = False

        if write_plist:
            subprocess.run(
                ["launchctl", "unload", plist_path],
                capture_output=True, timeout=5,
            )
            with open(plist_path, "w") as f:
                f.write(plist_content)

        # Make sure it's loaded
        check = subprocess.run(
            ["launchctl", "list", PROMO_GUARD_PLIST_LABEL],
            capture_output=True, timeout=5,
        )
        if check.returncode != 0:
            subprocess.run(
                ["launchctl", "load", plist_path],
                capture_output=True, timeout=5,
            )
    except Exception as exc:
        print(f"[promo_guard] setup warning: {exc}", file=sys.stderr)


# ─── BlueStacks ad suppression (host-side) ───────────────────────────────────
# These settings live in bluestacks.conf (NOT inside the qcow2), so they must
# be patched on every host.  The golden image handles the Android-side via a
# Magisk boot script; this handles the BlueStacks host-side.

_AD_KILL_SETTINGS = {
    "bst.enable_programmatic_ads": "0",
    "bst.feature.show_gp_ads": "0",
    "bst.feature.show_programmatic_ads_preference": "0",
    "bst.feature.send_programmatic_ads_boot_stats": "0",
    "bst.feature.send_programmatic_ads_click_stats": "0",
    "bst.feature.send_programmatic_ads_fill_stats": "0",
    "bst.feature.programmatic_ads": "0",
    "bst.enable_android_ads_test_app": "0",
}


def disable_host_ads():
    """Patch bluestacks.conf to suppress all ad / telemetry settings.

    Runs at GUI startup.  Safe to call repeatedly — only writes if values
    actually need changing.  Handles the uchg lock that BS sometimes sets.
    """
    conf_path = Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf")
    if not conf_path.is_file():
        return

    try:
        lines = conf_path.read_text().splitlines()
        changed = False
        seen_keys = set()

        for i, line in enumerate(lines):
            for key, desired in _AD_KILL_SETTINGS.items():
                if line.startswith(f'{key}='):
                    seen_keys.add(key)
                    expected = f'{key}="{desired}"'
                    if line.strip() != expected:
                        lines[i] = expected
                        changed = True

        # Append any settings not yet present
        for key, desired in _AD_KILL_SETTINGS.items():
            if key not in seen_keys:
                lines.append(f'{key}="{desired}"')
                changed = True

        if not changed:
            return

        # Remove uchg if present, write, optionally re-lock
        was_locked = "uchg" in subprocess.run(
            ["ls", "-lO", str(conf_path)], capture_output=True, text=True, timeout=5
        ).stdout
        if was_locked:
            subprocess.run(["chflags", "nouchg", str(conf_path)], capture_output=True, timeout=5)

        conf_path.write_text("\n".join(lines) + "\n")

        if was_locked:
            subprocess.run(["chflags", "uchg", str(conf_path)], capture_output=True, timeout=5)

        print("[ads] Host-side ad settings patched in bluestacks.conf", file=sys.stderr)
    except Exception as exc:
        print(f"[ads] warning: {exc}", file=sys.stderr)


@contextlib.contextmanager
def conf_unlocked(conf_path):
    """Context manager to temporarily unlock bluestacks.conf (remove macOS uchg flag).

    Handles the unlock via osascript with admin privileges, yields,
    then re-locks the file ONLY if it was originally locked.
    On fresh installs BS never sets uchg — adding it would prevent BS
    from writing first-run keys → 'Failed to read configuration file'.
    """
    conf_path = str(conf_path)
    safe_path = shlex.quote(conf_path)

    # Check if file has uchg flag
    ls = subprocess.run(["ls", "-lO", conf_path], capture_output=True, text=True, timeout=5)
    was_locked = "uchg" in ls.stdout

    if was_locked:
        cp = subprocess.run(
            ["osascript", "-e",
             f'do shell script "chflags nouchg {safe_path}" with administrator privileges'],
            capture_output=True, text=True, timeout=30
        )
        if cp.returncode != 0:
            raise PermissionError(f"Failed to unlock bluestacks.conf: {cp.stderr.strip()}")

    try:
        yield
    finally:
        # Only re-lock if it was originally locked — never introduce uchg
        # on a fresh install or BS can't write its runtime keys.
        if was_locked:
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f'do shell script "chflags uchg {safe_path}" with administrator privileges'],
                    capture_output=True, text=True, timeout=30
                )
            except Exception:
                pass  # Best-effort re-lock


def dbg(msg):
    if DEBUG:
        print(f"[DBG] {msg}", file=sys.__stderr__, flush=True)


# ─── Stdout capture for log forwarding ───────────────────────────────────────

class LogCapture:
    """Captures stdout/stderr writes and forwards them to the webview log panel."""

    def __init__(self, window_ref):
        self._window_ref = window_ref
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s):
        if not s:
            return
        with self._lock:
            self._buf += s
        # Flush complete lines
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._push_line(line)
        # Handle carriage returns (progress bars)
        while "\r" in self._buf:
            parts = self._buf.split("\r")
            if len(parts) > 1:
                # Take the last part after \r as the current line
                self._buf = parts[-1]
                self._push_line(parts[-2], cr=True)
                break

    def flush(self):
        with self._lock:
            if self._buf:
                self._push_line(self._buf)
                self._buf = ""

    def _push_line(self, line, cr=False):
        if not line:
            return
        w = self._window_ref()
        if w is None:
            return
        # Safely encode for JS using json.dumps (handles all edge cases)
        safe = json.dumps(line)
        try:
            if cr:
                w.evaluate_js(f"if(window.replaceLine)window.replaceLine({safe})")
            else:
                w.evaluate_js(f"if(window.appendLog)window.appendLog({safe})")
        except Exception:
            pass


# ─── Settings persistence ────────────────────────────────────────────────────

_settings_lock = threading.Lock()


def load_settings():
    with _settings_lock:
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def save_settings(data):
    with _settings_lock:
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass


# ─── Engine directory detection (Mac) ────────────────────────────────────────

_ENGINE_DEFAULTS = [
    Path(clone_instance.DEFAULT_ENGINE_DIR),
]

_INSTALL_DEFAULTS = [
    Path(clone_instance.DEFAULT_BLUESTACKS_DIR),
]


def detect_engine_dir_simple():
    """Check default paths for the BlueStacks Air engine directory."""
    for p in _ENGINE_DEFAULTS:
        if p.is_dir():
            return str(p)
    return ""


def detect_bs_install_dir():
    """Check default paths for the BlueStacks Air install directory."""
    for p in _INSTALL_DEFAULTS:
        if p.is_dir():
            return str(p)
    return ""


def _build_display_map(engine_dir):
    """Build name -> display_name map from bluestacks.conf."""
    conf_path = Path(engine_dir).parent / "bluestacks.conf"
    display_map = {}
    if conf_path.is_file():
        conf_lines = clone_instance.parse_conf(conf_path)
        for key, val in conf_lines:
            if val is not None and key.endswith(".display_name") and key.startswith("bst.instance."):
                name = key.split(".")[2]
                display_map[name] = val.strip('"')
    return display_map


def scan_instances(engine_dir):
    """Scan engine directory for BlueStacks Air instances.

    On Mac, instances are identified by having a data.qcow2 file
    (there are no .bstk config files like on Windows).
    """
    engine = Path(engine_dir)
    instances = []
    if not engine.is_dir():
        return instances
    display_map = _build_display_map(engine_dir)
    skip = {"Manager", "UserData"}
    for child in sorted(engine.iterdir()):
        if child.is_dir() and child.name not in skip:
            data_qcow2 = child / "data.qcow2"
            if data_qcow2.is_file():
                instances.append({
                    "name": child.name,
                    "display": display_map.get(child.name, child.name),
                })
    return instances


# ─── pywebview API class ─────────────────────────────────────────────────────

class Api:
    """Methods exposed to JavaScript via pywebview.api.*"""

    def __init__(self):
        self._window = None
        self._running = False
        self._running_lock = threading.Lock()
        self._vpn_manager = None  # Lazy-initialized VPNManager

    def set_window(self, window):
        self._window = window

    def _acquire_running(self):
        """Thread-safe check-and-set for _running flag. Returns True if acquired."""
        with self._running_lock:
            if self._running:
                return False
            self._running = True
            return True

    # ── Settings / paths ──

    def get_initial_state(self):
        """Called on page load. Returns engine dir + bluestacks dir + instances + source images."""
        settings = load_settings()
        saved = settings.get("engine_dir", "")

        if saved and Path(saved).is_dir():
            engine_dir = saved
        else:
            engine_dir = detect_engine_dir_simple()
            if engine_dir:
                settings["engine_dir"] = engine_dir
                save_settings(settings)

        # Images dir: default to DATA_DIR/images (writable location)
        images_dir = settings.get("images_dir", "")
        if not images_dir or not Path(images_dir).is_dir():
            images_dir = os.path.join(DATA_DIR, "images")
            os.makedirs(images_dir, exist_ok=True)
            settings["images_dir"] = images_dir
            save_settings(settings)

        instances = scan_instances(engine_dir) if engine_dir else []
        source_images = self._scan_source_images(images_dir)

        bs_dir = detect_bs_install_dir()

        return {
            "engine_dir": engine_dir,
            "bs_dir": bs_dir,
            "instances": instances,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "images_dir": images_dir,
            "source_images": source_images,
            # VPN state (persisted across restarts)
            "vpn_proxy_pool": settings.get("vpn_proxy_pool", []),
            "vpn_assignments": settings.get("vpn_assignments", {}),
            "suborbital_email": settings.get("suborbital_email", ""),
            "suborbital_has_password": bool(settings.get("suborbital_password", "")),
        }

    def verify_paths(self, engine_dir=None, bs_dir=None):
        """Verify setup-critical paths used by onboarding.

        Re-detects paths if not provided or empty (e.g. after BlueStacks
        was just installed and the page-load values are stale).
        """
        # Auto-detect if caller passed empty / None
        if not engine_dir:
            engine_dir = detect_engine_dir_simple()
        if not bs_dir:
            bs_dir = detect_bs_install_dir()

        engine_path = Path(engine_dir) if engine_dir else None
        bs_path = Path(bs_dir) if bs_dir else None
        conf_path = (engine_path.parent / "bluestacks.conf") if engine_path else None
        adb_path = (bs_path / "MacOS" / "hd-adb") if bs_path else None

        # Persist newly-detected engine_dir so future calls use it
        if engine_path and engine_path.is_dir():
            settings = load_settings()
            if settings.get("engine_dir") != str(engine_path):
                settings["engine_dir"] = str(engine_path)
                save_settings(settings)

        return {
            "engine_exists": bool(engine_path and engine_path.is_dir()),
            "bs_exists": bool(bs_path and bs_path.is_dir()),
            "conf_exists": bool(conf_path and conf_path.is_file()),
            "adb_exists": bool(adb_path and adb_path.is_file()),
            "engine_path": str(engine_path) if engine_path else "",
            "bs_path": str(bs_path) if bs_path else "",
            "conf_path": str(conf_path) if conf_path else "",
            "adb_path": str(adb_path) if adb_path else "",
        }

    def reset_settings(self):
        """Clear all saved settings to re-trigger setup wizard."""
        # Remove download marker if it exists
        settings = load_settings()
        engine_dir = settings.get("engine_dir", "")
        if engine_dir:
            marker = Path(engine_dir) / ".jspoof_image_downloaded"
            if marker.is_file():
                marker.unlink()
        save_settings({})
        return True

    def check_image_exists(self, engine_dir):
        """Check if any source images with the required disk file exist."""
        settings = load_settings()
        images_dir = settings.get("images_dir", os.path.join(DATA_DIR, "images"))
        source_images = self._scan_source_images(images_dir)
        complete = [img for img in source_images if img["complete"]]
        if complete:
            return {"exists": True, "images": complete}

        # Fallback: check for old marker file (migration from pre-v2.2)
        marker = Path(engine_dir) / ".jspoof_image_downloaded"
        if marker.is_file():
            return {"exists": True, "images": [], "legacy_marker": True}

        return {"exists": False, "images": []}

    def download_image(self, engine_dir, version='a13'):
        """Download and extract a source image archive."""
        if not self._acquire_running():
            return {"error": "An operation is already running"}

        settings = load_settings()
        images_dir = settings.get("images_dir", os.path.join(DATA_DIR, "images"))
        default_urls = {
            "a13": "https://ntii.io/base.qcow2",
        }
        urls = settings.get("download_urls", {})
        url = urls.get(version, default_urls.get(version, ""))

        if not url:
            # No URL configured — create the directory structure but skip download
            dest = Path(images_dir) / version
            dest.mkdir(parents=True, exist_ok=True)
            self._running = False
            if self._window:
                try:
                    self._window.evaluate_js(
                        "if(window.onOperationDone)window.onOperationDone('download')"
                    )
                except Exception:
                    pass
            return {"error": None, "version": version, "path": str(dest),
                    "skipped": True, "message": "No download URL configured. Place disk files manually."}

        def _run():
            try:
                self._download_and_extract(url, images_dir, version)
            except Exception as e:
                print(f"[Error] Download failed: {e}")
                self._emit_download_progress(-1, str(e))
            finally:
                self._running = False
                if self._window:
                    try:
                        self._window.evaluate_js(
                            "if(window.onOperationDone)window.onOperationDone('download')"
                        )
                    except Exception:
                        pass

        self._run_with_log(_run)
        return {"error": None, "version": version}

    def _download_and_extract(self, url, images_dir, version):
        """Download an archive (or raw qcow2) to images_dir/version/."""
        import urllib.request

        dest = Path(images_dir) / version
        dest.mkdir(parents=True, exist_ok=True)

        is_qcow2 = url.lower().endswith('.qcow2')
        is_7z = url.lower().endswith('.7z')

        if is_qcow2:
            # Raw qcow2 — download directly as data.qcow2
            dl_path = dest / "data.qcow2"
            partial = str(dl_path) + ".part"
        else:
            ext = '.7z' if is_7z else '.zip'
            dl_path = dest / f"{version}{ext}"
            partial = str(dl_path)

        archive_path = dl_path  # used by extraction code below

        # Download with progress
        print(f"Downloading {version} image from {url}...")
        req = urllib.request.Request(url, headers={"User-Agent": "LukesMirage/2.2"})

        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 256 * 1024  # 256KB chunks

            with open(partial, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = int(downloaded * 100 / total)
                        self._emit_download_progress(pct, "downloading")

        # For qcow2: rename .part → final and we're done (no extraction)
        if is_qcow2:
            os.replace(partial, str(dl_path))
            size_mb = dl_path.stat().st_size // (1024 * 1024)
            print(f"Download complete. Saved {size_mb} MB as {dl_path}")
            self._emit_download_progress(100, "complete")
            return

        print(f"Download complete. Extracting...")
        self._emit_download_progress(100, "extracting")

        if is_7z:
            # Try py7zr first, then BlueStacks' bundled 7zz, then 7z on PATH
            extracted = False
            try:
                import py7zr
                with py7zr.SevenZipFile(archive_path, 'r') as z:
                    z.extractall(path=str(dest))
                extracted = True
            except ImportError:
                pass

            if not extracted:
                import subprocess
                # Look for 7zz in BlueStacks Air app bundle
                exe_7z = None
                bs_dir = detect_bs_install_dir()
                if bs_dir:
                    candidate = Path(bs_dir) / "MacOS" / "7zz"
                    if candidate.is_file():
                        exe_7z = str(candidate)
                if not exe_7z:
                    # Fall back to 7z on PATH
                    exe_7z = "7z"

                print(f"Using {exe_7z} for extraction...")
                result = subprocess.run(
                    [exe_7z, "x", str(archive_path), f"-o{dest}", "-y"],
                    capture_output=True, text=True, timeout=600
                )
                if result.returncode != 0:
                    raise RuntimeError(f"7z extraction failed (exit {result.returncode}): {result.stderr}")
        else:
            import zipfile
            with zipfile.ZipFile(archive_path, 'r') as z:
                z.extractall(path=str(dest))

        # Clean up archive
        archive_path.unlink(missing_ok=True)

        # Flatten if archive extracted into a single nested subdirectory
        subdirs = [d for d in dest.iterdir() if d.is_dir()]
        top_level_files = {f.name for f in dest.iterdir() if f.is_file()}
        if len(subdirs) == 1 and not self.REQUIRED_DISK_FILES.issubset(top_level_files):
            nested = subdirs[0]
            if self.REQUIRED_DISK_FILES.issubset({f.name for f in nested.iterdir() if f.is_file()}):
                print(f"Flattening nested directory: {nested.name}/")
                import shutil
                for item in list(nested.iterdir()):
                    shutil.move(str(item), str(dest / item.name))
                nested.rmdir()

        print(f"Extraction complete: {dest}")
        self._emit_download_progress(100, "done")

    def _emit_download_progress(self, pct, status):
        """Push download progress to the frontend."""
        if self._window:
            try:
                self._window.evaluate_js(
                    f"if(window.onDownloadProgress)window.onDownloadProgress({pct},{json.dumps(status)})"
                )
            except Exception:
                pass

    def analyze_clones(self, engine_dir):
        """Analyze the Engine directory to find the source instance and existing clones.

        Detects the base instance (e.g. Tiramisu64) and all clones created via
        BlueStacks Multi-Instance Manager (e.g. Tiramisu64_1, Tiramisu64_2, ...).
        These already exist on disk — our program patches them with unique identities.
        """
        import re
        engine = Path(engine_dir)
        if not engine.is_dir():
            return {"error": "Engine directory not found", "source": "", "clones": [], "all_dirs": []}

        # Get all instance dirs (excluding Manager, UserData, etc.)
        all_dirs = []
        all_folders = []          # every subfolder, even without data.qcow2
        skip = {"Manager", "UserData"}
        for child in sorted(engine.iterdir()):
            if child.is_dir() and child.name not in skip:
                all_folders.append(child.name)
                # On Mac, check for data.qcow2 instead of .bstk
                data_qcow2 = child / "data.qcow2"
                if data_qcow2.is_file():
                    all_dirs.append(child.name)

        if not all_dirs and not all_folders:
            return {"error": "No instances found", "source": "", "clones": [], "all_dirs": []}

        # If no qcow2 instances found, still use all_folders as fallback
        if not all_dirs:
            all_dirs = all_folders

        # Find the base/source instance:
        # It's the one WITHOUT an underscore+number suffix
        # e.g. "Tiramisu64" is the source, "Tiramisu64_1", "Tiramisu64_2" are clones
        source = None
        for name in all_dirs:
            match = re.match(r'^(.+?)_(\d+)$', name)
            if not match:
                # This could be the source — check if any X_N instances exist for it
                derived = [n for n in all_dirs if re.match(rf'^{re.escape(name)}_\d+$', n)]
                if derived or source is None:
                    source = name

        if not source:
            source = all_dirs[0]

        # Find all existing clones (instances matching source_N pattern)
        clone_pattern = re.compile(rf'^{re.escape(source)}_(\d+)$')
        clones = []
        max_suffix = 0
        for name in all_dirs:
            m = clone_pattern.match(name)
            if m:
                clones.append(name)
                suffix_num = int(m.group(1))
                if suffix_num > max_suffix:
                    max_suffix = suffix_num

        # Also check bluestacks.conf for any instance entries with higher suffixes
        # (MIM may have created and deleted instances, leaving gaps)
        conf_path = engine.parent / "bluestacks.conf"
        if conf_path.is_file():
            try:
                conf_pattern = re.compile(
                    rf'bst\.instance\.{re.escape(source)}_(\d+)\.'
                )
                with open(conf_path, "r", encoding="utf-8") as f:
                    for line in f:
                        cm = conf_pattern.match(line)
                        if cm:
                            conf_suffix = int(cm.group(1))
                            if conf_suffix > max_suffix:
                                max_suffix = conf_suffix
            except Exception:
                pass

        # Also find any other instances that aren't the source and don't match
        # the source_N pattern (e.g. Pie64) — these are separate base instances
        other_bases = [n for n in all_dirs if n != source and not clone_pattern.match(n)]

        display_map = _build_display_map(engine_dir)

        return {
            "error": None,
            "source": source,
            "source_display": display_map.get(source, source),
            "clones": [{"name": n, "display": display_map.get(n, n)} for n in clones],
            "existing_count": len(clones),
            "max_clone_suffix": max_suffix,
            "other_instances": [{"name": n, "display": display_map.get(n, n)} for n in other_bases],
            "all_dirs": [{"name": n, "display": display_map.get(n, n)} for n in all_folders],
        }

    def set_engine_dir(self, path):
        """Save engine dir and return updated instance list."""
        settings = load_settings()
        settings["engine_dir"] = path
        save_settings(settings)
        instances = scan_instances(path) if path and Path(path).is_dir() else []
        return {"instances": instances}

    def browse_engine_dir(self):
        """Open native folder picker, return selected path."""
        if not self._window:
            return ""
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory="",
            allow_multiple=False
        )
        if result and len(result) > 0:
            path = result[0]
            self.set_engine_dir(path)
            return path
        return ""

    # ── Source images ──

    REQUIRED_DISK_FILES = {"data.qcow2"}

    def _scan_source_images(self, images_dir):
        """Scan images_dir for subdirectories containing data.qcow2."""
        images_path = Path(images_dir)
        results = []
        if not images_path.is_dir():
            return results
        for child in sorted(images_path.iterdir()):
            if child.is_dir():
                found = {f.name for f in child.iterdir() if f.is_file()}
                has_all = self.REQUIRED_DISK_FILES.issubset(found)
                total_size = sum(
                    (child / f).stat().st_size for f in self.REQUIRED_DISK_FILES
                    if (child / f).is_file()
                ) if has_all else 0
                results.append({
                    "name": child.name,
                    "path": str(child),
                    "complete": has_all,
                    "size_mb": total_size // (1024 * 1024),
                    "files": [f for f in self.REQUIRED_DISK_FILES if (child / f).is_file()],
                })
        return results

    def get_source_images(self):
        """Return current images dir and all source images found."""
        settings = load_settings()
        images_dir = settings.get("images_dir", os.path.join(DATA_DIR, "images"))
        return {
            "images_dir": images_dir,
            "images": self._scan_source_images(images_dir),
        }

    def scan_instances_for_dir(self, engine_dir):
        """Scan engine dir and return instance names."""
        return scan_instances(engine_dir) if engine_dir else []

    # ── BlueStacks Install ──

    _REQUIRED_BS_VERSION = "5.21.755.7538"
    _BS_PKG_URL = "https://ntii.io/BlueStacksInstaller_5.21.755.7538.pkg"
    _BS_APP = Path("/Applications/BlueStacks.app")
    _BS_ENGINE = Path("/Users/Shared/Library/Application Support/BlueStacks/Engine")

    def check_bluestacks(self):
        """Check BlueStacks installation and version.
        Returns dict with installed, version, version_ok, engine_dir_exists.
        Tries CFBundleVersion first, falls back to CFBundleShortVersionString.
        """
        import subprocess
        installed = self._BS_APP.is_dir()
        version = ""
        if installed:
            for plist_key in ("CFBundleVersion", "CFBundleShortVersionString"):
                try:
                    cp = subprocess.run(
                        ["defaults", "read",
                         str(self._BS_APP / "Contents" / "Info.plist"),
                         plist_key],
                        capture_output=True, text=True, timeout=10
                    )
                    v = cp.stdout.strip()
                    if v and cp.returncode == 0:
                        version = v
                        if version == self._REQUIRED_BS_VERSION:
                            break  # Found exact match, stop looking
                except Exception:
                    continue
            if not version:
                version = "unknown"
        return {
            "installed": installed,
            "version": version,
            "version_ok": version == self._REQUIRED_BS_VERSION,
            "required_version": self._REQUIRED_BS_VERSION,
            "engine_dir_exists": self._BS_ENGINE.is_dir(),
        }

    def _bs_install_status(self, phase, detail, pct=-1):
        """Push live install status to the frontend."""
        import json
        if self._window:
            try:
                payload = json.dumps({"phase": phase, "detail": detail, "pct": pct})
                self._window.evaluate_js(
                    f'if(window._onBsInstallProgress)window._onBsInstallProgress({payload})')
            except Exception:
                pass

    def install_bluestacks(self, force_reinstall=False):
        """Download and install BlueStacks 5.21.755.7538.
        If force_reinstall=True, uninstalls existing version first.
        Returns dict with success (bool), output (str), phase (str).
        """
        import subprocess, urllib.request, time
        pkg_path = Path("/tmp/BlueStacksInstaller_5.21.755.7538.pkg")

        try:
            # Phase 1: Kill BlueStacks
            self._bs_install_status("kill", "Stopping BlueStacks...")
            for proc in ("BlueStacks", "HD-MultiInstanceManager", "HD-LogCollector",
                         "hd-adb", "HD-DiskCompaction"):
                subprocess.run(["pkill", "-9", "-x", proc],
                               capture_output=True, timeout=5)
            time.sleep(2)

            # Phase 2: Uninstall if needed
            if force_reinstall:
                self._bs_install_status("uninstall", "Removing old version...")
                uninstall_cmds = (
                    "rm -rf /Applications/BlueStacks.app;"
                    "rm -rf /Applications/BlueStacksMIM.app;"
                    "rm -rf '/Users/Shared/Library/Application Support/BlueStacks';"
                    "pkgutil --forget com.now.gg.BlueStacks 2>/dev/null;"
                    "pkgutil --forget com.now.gg.BlueStacksMIM 2>/dev/null;"
                    "launchctl bootout system /Library/LaunchDaemons/com.now.gg.BlueStacks.cleanup.plist 2>/dev/null;"
                    "rm -f /Library/LaunchDaemons/com.now.gg.BlueStacks.cleanup.plist;"
                    "rm -rf '/Library/Application Support/BlueStacks'"
                )
                cp = subprocess.run(
                    ["osascript", "-e",
                     f'do shell script "{uninstall_cmds}" with administrator privileges'],
                    capture_output=True, text=True, timeout=30
                )
                if cp.returncode != 0:
                    print(f"[bs-install] Uninstall warning: {cp.stderr.strip()}")
                time.sleep(1)

            # Phase 3: Download .pkg
            if not pkg_path.exists() or pkg_path.stat().st_size < 500_000_000:
                self._bs_install_status("download", "Downloading installer...", 0)
                req = urllib.request.Request(self._BS_PKG_URL,
                    headers={"User-Agent": "jorkSpoofer-GUI/2.2-mac"})
                with urllib.request.urlopen(req, timeout=600) as resp:
                    total = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    last_pct = -1
                    with open(str(pkg_path), "wb") as f:
                        while True:
                            chunk = resp.read(262144)  # 256KB
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = int(downloaded / total * 100)
                                if pct != last_pct:
                                    self._bs_install_status("download",
                                        f"Downloading... {pct}%", pct)
                                    last_pct = pct
                self._bs_install_status("download", "Download complete", 100)
            else:
                self._bs_install_status("download", "Installer cached", 100)

            # Phase 4: Silent install
            self._bs_install_status("install", "Installing... (admin password required)")
            safe_pkg = shlex.quote(str(pkg_path))
            install_cmd = f'installer -pkg {safe_pkg} -target /'
            cp = subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{install_cmd}" with administrator privileges'],
                capture_output=True, text=True, timeout=300
            )
            if cp.returncode != 0:
                err = cp.stderr.strip() or cp.stdout.strip()
                return {"success": False, "output": f"Install failed: {err}",
                        "phase": "install"}

            # Phase 5: Verify
            self._bs_install_status("verify", "Verifying installation...")
            time.sleep(2)
            check = self.check_bluestacks()
            if not check["installed"]:
                self._bs_install_status("verify", "App not found after install")
                return {"success": False,
                        "output": "Verification failed: app not found after install",
                        "phase": "verify"}
            if not check["version_ok"]:
                self._bs_install_status("verify",
                    f"Version mismatch: got {check['version']}")
                return {"success": False,
                        "output": f"Version mismatch: got {check['version']}, "
                                  f"expected {self._REQUIRED_BS_VERSION}",
                        "phase": "verify"}
            self._bs_install_status("verify", f"Verified v{check['version']}")

            # Phase 6: Cleanup
            try:
                pkg_path.unlink(missing_ok=True)
            except Exception:
                pass

            self._bs_install_status("done", "Installation complete!", 100)
            return {"success": True,
                    "output": f"BlueStacks v{check['version']} installed",
                    "phase": "done"}

        except subprocess.TimeoutExpired:
            self._bs_install_status("error", "Operation timed out")
            return {"success": False, "output": "Operation timed out",
                    "phase": "timeout"}
        except Exception as e:
            self._bs_install_status("error", str(e))
            return {"success": False, "output": str(e),
                    "phase": "error"}

    def ensure_bluestacks_dirs(self):
        """Launch BlueStacks once and let it FULLY complete first-run initialization.

        Previous approach killed BS ~7 seconds after Engine dir appeared.
        That was far too early — BS was still writing bluestacks.conf, leaving
        it truncated/incomplete → "Failed to read configuration file" on launch.

        New approach: wait for bluestacks.conf to exist AND stabilize (size
        unchanged for 10+ seconds) before gracefully quitting BS.

        Returns dict with created (bool) and detail (str).
        """
        import subprocess, time
        if self._BS_ENGINE.is_dir():
            return {"created": False, "detail": "Engine directory already exists"}
        if not self._BS_APP.is_dir():
            return {"created": False, "detail": "BlueStacks not installed"}

        print("[bs-dirs] Launching BlueStacks for first-run initialization...")
        subprocess.run(["open", str(self._BS_APP)], timeout=10)

        conf_path = self._BS_ENGINE.parent / "bluestacks.conf"

        # ── Phase 1: Wait for Engine directory to appear (up to 60s) ──
        engine_found = False
        for i in range(30):
            time.sleep(2)
            if self._BS_ENGINE.is_dir():
                print(f"[bs-dirs] Engine directory created after {(i+1)*2}s")
                engine_found = True
                break

        if not engine_found:
            subprocess.run(["pkill", "-9", "-x", "BlueStacks"],
                           capture_output=True, timeout=5)
            return {"created": False,
                    "detail": "Timed out waiting for Engine directory"}

        # ── Phase 2: Wait for bluestacks.conf to stabilize (up to 90s) ──
        # BS writes config progressively during first-run. We need the file
        # to stop growing before we can safely quit BS. Poll every 3s;
        # consider stable when size hasn't changed for 4 consecutive checks
        # (12+ seconds) and the file has meaningful content (>1000 bytes).
        print("[bs-dirs] Waiting for BlueStacks to finish writing config...")
        last_size = -1
        stable_count = 0
        conf_stabilized = False
        for i in range(30):  # up to 90 more seconds
            time.sleep(3)
            try:
                if conf_path.is_file():
                    current_size = conf_path.stat().st_size
                    if current_size == last_size and current_size > 1000:
                        stable_count += 1
                        if stable_count >= 4:  # Stable for 12+ seconds
                            print(f"[bs-dirs] bluestacks.conf stabilized at "
                                  f"{current_size:,} bytes after ~{(i+1)*3}s")
                            conf_stabilized = True
                            break
                    else:
                        if last_size != current_size:
                            dbg(f"[bs-dirs] conf growing: {current_size:,} bytes")
                        stable_count = 0
                    last_size = current_size
            except OSError:
                pass

            if i > 0 and i % 5 == 0:
                print(f"[bs-dirs] Still waiting for first-run init... ({(i+1)*3}s)")

        if not conf_stabilized:
            print("[bs-dirs] WARNING: bluestacks.conf did not stabilize within "
                  "timeout — proceeding anyway")

        # ── Phase 3: Extra wait for any final writes ──
        # Even after conf stabilizes, give BS a few more seconds for any
        # background tasks (MIM metadata, instance defaults, etc.)
        print("[bs-dirs] Allowing extra time for background initialization...")
        time.sleep(8)

        # ── Phase 4: Gracefully quit BlueStacks ──
        print("[bs-dirs] Quitting BlueStacks...")
        # Try graceful quit via AppleScript first
        subprocess.run(
            ["osascript", "-e", 'tell application "BlueStacks" to quit'],
            capture_output=True, timeout=10
        )
        time.sleep(4)

        # Force kill if still alive
        still_running = subprocess.run(
            ["pgrep", "-x", "BlueStacks"],
            capture_output=True, timeout=5
        ).returncode == 0

        if still_running:
            subprocess.run(["pkill", "-x", "BlueStacks"],
                           capture_output=True, timeout=5)
            subprocess.run(["pkill", "-x", "HD-MultiInstanceManager"],
                           capture_output=True, timeout=5)
            time.sleep(3)
            # Last resort: SIGKILL
            if subprocess.run(["pgrep", "-x", "BlueStacks"],
                              capture_output=True, timeout=5).returncode == 0:
                subprocess.run(["pkill", "-9", "-x", "BlueStacks"],
                               capture_output=True, timeout=5)
                time.sleep(2)

        # Also kill helper processes
        for proc in ("HD-MultiInstanceManager", "hd-adb",
                     "HD-DiskCompaction", "HD-LogCollector"):
            subprocess.run(["pkill", "-x", proc],
                           capture_output=True, timeout=5)

        # ── Phase 5: Final verification ──
        conf_ok = conf_path.is_file() and conf_path.stat().st_size > 1000
        detail = (f"Directories created, config {'OK' if conf_ok else 'INCOMPLETE'} "
                  f"({conf_path.stat().st_size:,} bytes)" if conf_path.is_file()
                  else "Directories created, config file missing")
        print(f"[bs-dirs] {detail}")

        return {"created": True, "detail": detail}

    def disable_auto_updates(self, engine_dir):
        """Disable BlueStacks Air auto-updates via LaunchAgent/plist removal.

        IMPORTANT: We do NOT modify bluestacks.conf to add update-disabling keys.
        BS validates every key against its internal property directory on boot.
        Unknown/fabricated keys cause: "prop not found in prop dir" → FATAL err -1.

        Instead we:
        1. Remove BlueStacks updater LaunchAgent/LaunchDaemon plists
        2. (Future: could block cloud.bluestacks.com via /etc/hosts)

        Returns dict with results.
        """
        import subprocess

        result = {
            "conf_updated": False,  # Always False — we never touch conf
            "updater_disabled": False,
            "detail": "",
        }

        if not engine_dir:
            return {"error": "Engine directory not set"}

        # Step 1: Dynamically scan for ALL BlueStacks/now.gg updater plists
        updater_plists = []
        search_dirs = [
            "/Library/LaunchDaemons",
            os.path.expanduser("~/Library/LaunchAgents"),
            "/Library/LaunchAgents",
        ]
        for search_dir in search_dirs:
            if os.path.isdir(search_dir):
                try:
                    for fname in os.listdir(search_dir):
                        fname_lower = fname.lower()
                        if ("bluestacks" in fname_lower or "now.gg" in fname_lower
                                or "bluestacksupd" in fname_lower):
                            updater_plists.append(os.path.join(search_dir, fname))
                except PermissionError:
                    pass

        removed = []
        for plist in updater_plists:
            if os.path.exists(plist):
                try:
                    # Unload first
                    label = Path(plist).stem
                    subprocess.run(
                        ["launchctl", "bootout", f"gui/{os.getuid()}", plist],
                        capture_output=True, timeout=10
                    )
                    # Then remove
                    os.remove(plist)
                    removed.append(plist)
                except Exception:
                    # Some plists need admin to remove
                    try:
                        safe_plist = shlex.quote(plist)
                        subprocess.run(
                            ["osascript", "-e",
                             f'do shell script "launchctl bootout system {safe_plist} 2>/dev/null; rm -f {safe_plist}" with administrator privileges'],
                            capture_output=True, text=True, timeout=15
                        )
                        removed.append(plist)
                    except Exception:
                        pass

        result["updater_disabled"] = True
        result["removed_plists"] = removed
        result["detail"] = f"Removed {len(removed)} updater plist(s)"

        return result

    # ── Rooting ──

    def _root_script_env(self):
        """Return env dict with JORK_DATA_DIR for root_bluestacks.sh."""
        env = os.environ.copy()
        env["JORK_DATA_DIR"] = DATA_DIR
        return env

    def check_root_status(self):
        """Check if BlueStacks Air is rooted (Magisk files in initrd).
        Returns dict with 'rooted' (bool) and 'detail' (str).
        Uses --gui flag to prevent interactive prompts from hanging.
        """
        import subprocess
        script = Path(BUNDLE_DIR) / "root_bluestacks.sh"
        if not script.is_file():
            return {"rooted": False, "detail": "root_bluestacks.sh not found"}
        try:
            cp = subprocess.run(
                ["bash", str(script), "--gui", "--status"],
                capture_output=True, text=True, timeout=30,
                env=self._root_script_env()
            )
            output = (cp.stdout + cp.stderr).strip()
            rooted = "ROOTED" in output.upper() and "NOT ROOTED" not in output.upper()
            return {"rooted": rooted, "detail": output}
        except subprocess.TimeoutExpired:
            return {"rooted": False, "detail": "Status check timed out"}
        except Exception as e:
            return {"rooted": False, "detail": str(e)}

    def root_bluestacks(self):
        """Root BlueStacks Air by running root_bluestacks.sh --gui.
        The --gui flag makes the script auto-kill BlueStacks, skip interactive
        prompts, and proceed silently through version mismatches.
        Returns dict with 'success' (bool) and 'output' (str).
        """
        import subprocess
        script = Path(BUNDLE_DIR) / "root_bluestacks.sh"
        if not script.is_file():
            return {"success": False, "output": "root_bluestacks.sh not found"}

        try:
            cp = subprocess.run(
                ["bash", str(script), "--gui"],
                capture_output=True, text=True, timeout=180,
                env=self._root_script_env()
            )
            output = (cp.stdout + cp.stderr).strip()
            success = cp.returncode == 0 and "ROOT COMPLETE" in output.upper()
            if not success:
                # Extract the most useful error line for the user
                lines = output.split('\n')
                err_lines = [l for l in lines if any(w in l.lower() for w in ['fatal', '[✗]', 'error', 'failed', 'could not'])]
                hint = err_lines[-1] if err_lines else lines[-1] if lines else "Unknown error"
                return {"success": False, "output": hint, "full_output": output}
            return {"success": True, "output": output}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "Rooting timed out (>180s). Check internet connection."}
        except Exception as e:
            return {"success": False, "output": str(e)}

    # ── Permissions ──

    def fix_permissions(self, engine_dir):
        """Fix ALL macOS & device permissions required for Luke's Mirage.

        Host-side (macOS):
          1. Unlock bluestacks.conf (remove uchg immutable flag).
          2. Verify bluestacks.conf is writable, then re-lock it.
          3. Verify MimMetaData.json is writable.
          4. Ensure images directory exists and is writable.
          5. Mark all shell scripts as executable.

        Device-side (via ADB on any running instance):
          6. Ensure Magisk su_access=2 (auto-grant, no prompt).
          7. Ensure UID 0 and UID 2000 (ADB shell) have root policies.
          8. Grant Checker app (com.jorkspoofer.checker) permanent root.

        Returns dict with results for every sub-check.
        """
        import subprocess, re, os, stat

        if not engine_dir:
            return {"error": "Engine directory not set"}

        engine_path = Path(engine_dir)
        conf_path = engine_path.parent / "bluestacks.conf"
        if not conf_path.is_file():
            return {"error": f"bluestacks.conf not found at {conf_path}"}

        result = {
            # Host-side
            "conf_unlocked": False,
            "conf_locked": False,
            "mim_writable": False,
            "mim_detail": "",
            "images_dir_ok": False,
            "images_dir_detail": "",
            "scripts_executable": False,
            "scripts_detail": "",
            # Device-side
            "magisk_settings": False,
            "magisk_settings_detail": "",
            "shell_root": False,
            "shell_root_detail": "",
            "checker_root": False,
            "checker_root_detail": "",
        }

        # ── Host Steps 1-2: Unlock conf (if uchg) → verify writable → re-lock ──
        # ONE admin prompt to unlock, verify, then re-lock.
        #
        # IMPORTANT: We NEVER add, append, or insert keys into bluestacks.conf.
        # BS validates every key against its internal property directory on boot.
        # Unknown keys cause: "prop not found in prop dir" → FATAL err -1.
        # We can only safely modify VALUES of keys BS already wrote.
        try:
            ls_result = subprocess.run(
                ["ls", "-lO", str(conf_path)],
                capture_output=True, text=True, timeout=5
            )
            has_uchg = "uchg" in ls_result.stdout

            if has_uchg:
                # Step 1: Unlock (single admin prompt)
                safe_conf = shlex.quote(str(conf_path))
                unlock_cmd = (
                    f'do shell script "chflags nouchg {safe_conf}"'
                    f' with administrator privileges'
                )
                cp = subprocess.run(
                    ["osascript", "-e", unlock_cmd],
                    capture_output=True, text=True, timeout=30
                )
                if cp.returncode != 0:
                    dbg(f"Unlock failed: {cp.stderr}")
                    # conf_unlocked stays False — user cancelled password
                else:
                    result["conf_unlocked"] = True
            else:
                # Already unlocked
                result["conf_unlocked"] = True

            if result["conf_unlocked"]:
                # Step 2: Verify writable
                try:
                    with open(conf_path, "r+b"):
                        pass
                except OSError:
                    result["conf_unlocked"] = False

                # Step 3: Re-lock ONLY if the conf was originally locked.
                # On fresh installs BS has never set uchg — we must NOT add it
                # or BS can't write first-run setup keys.
                if has_uchg:
                    safe_conf = shlex.quote(str(conf_path))
                    relock_cmd = (
                        f'do shell script "chflags uchg {safe_conf}"'
                        f' with administrator privileges'
                    )
                    cp = subprocess.run(
                        ["osascript", "-e", relock_cmd],
                        capture_output=True, text=True, timeout=30
                    )
                    result["conf_locked"] = cp.returncode == 0
                    result["conf_lock_skipped"] = False
                    if not result["conf_locked"]:
                        dbg(f"Re-lock failed: {cp.stderr}")
                else:
                    # Fresh install — BS never set uchg, don't add it
                    result["conf_locked"] = False
                    result["conf_lock_skipped"] = True
                    dbg("Skipping uchg re-lock (fresh install, BS never set it)")

            # Step 5: Remove BlueStacks/now.gg updater plists
            updater_plists = []
            search_dirs = [
                "/Library/LaunchDaemons",
                os.path.expanduser("~/Library/LaunchAgents"),
                "/Library/LaunchAgents",
            ]
            for search_dir in search_dirs:
                if os.path.isdir(search_dir):
                    try:
                        for fname in os.listdir(search_dir):
                            fname_lower = fname.lower()
                            if ("bluestacks" in fname_lower or "now.gg" in fname_lower
                                    or "bluestacksupd" in fname_lower):
                                updater_plists.append(os.path.join(search_dir, fname))
                    except PermissionError:
                        pass

            for plist in updater_plists:
                if os.path.exists(plist):
                    try:
                        label = Path(plist).stem
                        subprocess.run(
                            ["launchctl", "bootout", f"gui/{os.getuid()}", plist],
                            capture_output=True, timeout=10
                        )
                        os.remove(plist)
                    except Exception:
                        try:
                            safe_plist = shlex.quote(plist)
                            subprocess.run(
                                ["osascript", "-e",
                                 f'do shell script "launchctl bootout system {safe_plist} 2>/dev/null; rm -f {safe_plist}" with administrator privileges'],
                                capture_output=True, text=True, timeout=15
                            )
                        except Exception:
                            pass

        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            dbg(f"Config permission error: {e}")

        # ── Host Step 4: Verify MimMetaData.json is writable ──
        mim_path = engine_path / "UserData" / "MimMetaData.json"
        try:
            if mim_path.is_file():
                with open(mim_path, "r+b"):
                    pass
                result["mim_writable"] = True
                result["mim_detail"] = "Writable"
            else:
                result["mim_writable"] = False
                result["mim_detail"] = "Not found"
        except OSError as e:
            result["mim_detail"] = str(e)

        # ── Host Step 5: Ensure images directory exists ──
        try:
            images_dir = Path(DATA_DIR) / "images"
            os.makedirs(images_dir, exist_ok=True)
            # Verify writable by touching a temp file
            test_file = images_dir / ".perm_test"
            test_file.write_text("ok")
            test_file.unlink()
            result["images_dir_ok"] = True
            result["images_dir_detail"] = str(images_dir)
        except Exception as e:
            result["images_dir_detail"] = str(e)

        # ── Host Step 6: Mark all shell scripts as executable ──
        try:
            script_dir = Path(BUNDLE_DIR)
            scripts_fixed = []
            for pattern in ("*.sh", "*.command"):
                for script in script_dir.glob(pattern):
                    st = script.stat()
                    if not (st.st_mode & stat.S_IXUSR):
                        script.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                        scripts_fixed.append(script.name)
            if scripts_fixed:
                result["scripts_executable"] = True
                result["scripts_detail"] = f"Fixed: {', '.join(scripts_fixed)}"
            else:
                result["scripts_executable"] = True
                result["scripts_detail"] = "All already executable"
        except Exception as e:
            result["scripts_detail"] = str(e)

        # ── Device-side: Find ADB and ALL running rooted instances ──
        #
        # CRITICAL: We apply device-side fixes to EVERY running instance,
        # not just one.  Each instance has its own /data partition with its
        # own Magisk database.  Granting root on instance A does nothing
        # for instance B — they are completely isolated.
        bs_dir = detect_bs_install_dir() or clone_instance.DEFAULT_BLUESTACKS_DIR
        adb_exe = randomize_instances.find_adb_exe(bs_dir)
        instances = randomize_instances.discover_instances_from_conf(conf_path)

        # Helper to run ADB shell with su
        def adb_su(serial, cmd, timeout=10):
            return subprocess.run(
                [adb_exe, "-s", serial, "shell", f'su -c "{cmd}"'],
                capture_output=True, text=True, timeout=timeout
            )

        # Collect ALL running + rooted instances
        connected = []  # list of (serial, inst) tuples
        for inst in instances:
            serial = f"127.0.0.1:{inst.adb_port}"
            try:
                if not randomize_instances.adb_connect(adb_exe, serial):
                    continue
                if not randomize_instances.probe_boot_completed(adb_exe, serial):
                    continue
                if not randomize_instances.probe_root(adb_exe, serial):
                    continue
                connected.append((serial, inst))
            except Exception:
                continue

        if not connected:
            result["magisk_settings_detail"] = "No running instance with root"
            result["shell_root_detail"] = "No running instance with root"
            result["checker_root_detail"] = "No running instance with root"
            return result

        # ── Apply device-side fixes to EVERY connected instance ──
        #
        # We track per-instance results and report a summary.
        # All instances must pass for the step to be marked successful.
        magisk_ok_names = []
        magisk_fail_names = []
        policy_ok_names = []
        policy_fail_names = []
        checker_ok_names = []
        checker_fail_names = []
        checker_missing_names = []

        settings_cmds = [
            "magisk --sqlite \\\"CREATE TABLE IF NOT EXISTS settings (key TEXT, value INT, PRIMARY KEY(key))\\\"",
            "magisk --sqlite \\\"INSERT OR REPLACE INTO settings VALUES('su_access', 2)\\\"",
            "magisk --sqlite \\\"INSERT OR REPLACE INTO settings VALUES('multiuser_mode', 0)\\\"",
            "magisk --sqlite \\\"INSERT OR REPLACE INTO settings VALUES('mnt_ns', 0)\\\"",
            # Use INSERT OR IGNORE for denylist/zygisk so we don't overwrite device-hardened values
            "magisk --sqlite \\\"INSERT OR IGNORE INTO settings VALUES('denylist', 0)\\\"",
            "magisk --sqlite \\\"INSERT OR IGNORE INTO settings VALUES('zygisk', 0)\\\"",
            "magisk --sqlite \\\"CREATE TABLE IF NOT EXISTS policies (uid INT, policy INT, until INT, logging INT, notification INT, PRIMARY KEY(uid))\\\"",
            "magisk --sqlite \\\"CREATE TABLE IF NOT EXISTS hidelist (package_name TEXT, process TEXT, PRIMARY KEY(package_name, process))\\\"",
            "magisk --sqlite \\\"CREATE TABLE IF NOT EXISTS strings (key TEXT, value TEXT, PRIMARY KEY(key))\\\"",
        ]

        policy_cmds = [
            "magisk --sqlite \\\"INSERT OR REPLACE INTO policies VALUES(0, 2, 0, 1, 0)\\\"",
            "magisk --sqlite \\\"INSERT OR REPLACE INTO policies VALUES(2000, 2, 0, 1, 0)\\\"",
        ]

        for serial, inst in connected:
            name = inst.name
            dbg(f"Device-side fix: {name} at {serial}")

            # ── Step 7: Magisk settings ──
            try:
                all_ok = True
                for cmd in settings_cmds:
                    cp = adb_su(serial, cmd)
                    if cp.returncode != 0:
                        all_ok = False
                        dbg(f"  {name}: Magisk setting failed: {cmd} -> {cp.stderr}")
                # Verify
                cp = adb_su(serial, "magisk --sqlite \\\"SELECT value FROM settings WHERE key='su_access'\\\"")
                if "2" in cp.stdout or all_ok:
                    magisk_ok_names.append(name)
                else:
                    magisk_fail_names.append(name)
            except Exception as e:
                dbg(f"  {name}: Magisk settings exception: {e}")
                magisk_fail_names.append(name)

            # ── Step 8: UID 0 + 2000 root policies ──
            try:
                all_ok = True
                for cmd in policy_cmds:
                    cp = adb_su(serial, cmd)
                    if cp.returncode != 0:
                        all_ok = False
                # Verify
                cp = adb_su(serial, "magisk --sqlite \\\"SELECT uid FROM policies WHERE uid=2000 AND policy=2\\\"")
                if "2000" in cp.stdout or all_ok:
                    policy_ok_names.append(name)
                else:
                    policy_fail_names.append(name)
            except Exception as e:
                dbg(f"  {name}: Policy exception: {e}")
                policy_fail_names.append(name)

            # ── Step 9: Grant Checker app root ──
            try:
                # Find Checker UID on THIS instance
                cp = adb_su(serial, "dumpsys package com.jorkspoofer.checker | grep userId=")
                uid = None
                if cp.returncode == 0:
                    m = re.search(r'userId=(\d+)', cp.stdout)
                    if m:
                        uid = int(m.group(1))

                if uid is None:
                    checker_missing_names.append(name)
                else:
                    # Check if already granted
                    cp = adb_su(serial, f"magisk --sqlite \\\"SELECT uid FROM policies WHERE uid={uid} AND policy=2\\\"")
                    if str(uid) in cp.stdout:
                        checker_ok_names.append(name)
                        dbg(f"  {name}: Checker UID {uid} already granted")
                    else:
                        # Grant root to Checker
                        cp = adb_su(serial,
                            f"magisk --sqlite \\\"INSERT OR REPLACE INTO policies VALUES({uid}, 2, 0, 1, 0)\\\"")
                        adb_su(serial, "sync")
                        if cp.returncode == 0:
                            checker_ok_names.append(name)
                            dbg(f"  {name}: Checker UID {uid} granted")
                        else:
                            checker_fail_names.append(name)
                            dbg(f"  {name}: Checker grant failed: {cp.stderr}")
            except Exception as e:
                dbg(f"  {name}: Checker exception: {e}")
                checker_fail_names.append(name)

            # ── Step 10: Auto-grant su to ALL installed apps ──
            # Scan packages.list and grant root to every app UID >= 10000.
            # Uses INSERT OR IGNORE so existing grants are untouched.
            try:
                cp = adb_su(serial,
                    "awk '{uid=$2; if(uid+0 >= 10000) print uid}' /data/system/packages.list"
                    " | sort -u"
                    " | while read uid; do"
                    " magisk --sqlite \\\"INSERT OR IGNORE INTO policies VALUES($uid, 2, 0, 1, 0)\\\";"
                    " done")
                dbg(f"  {name}: Auto-granted su to all installed apps")
            except Exception as e:
                dbg(f"  {name}: Auto-grant exception: {e}")

            # ── Step 11: Device-side auto-update hardening ──
            # Disable Play Store auto-updates + GMS system update services.
            # Non-fatal — failures here don't block the permissions step.
            try:
                # Disable package verifier (prevents Google scanning installed APKs)
                adb_su(serial, "settings put global package_verifier_enable 0")
                adb_su(serial, "settings put global verifier_verify_adb_installs 0")

                # Disable GMS system-update components (pm disable with root for reliability)
                gms_update_components = [
                    "com.google.android.gms/com.google.android.gms.update.SystemUpdateActivity",
                    "com.google.android.gms/com.google.android.gms.update.SystemUpdateService",
                    "com.google.android.gms/com.google.android.gms.update.SystemUpdateService\\$ActiveReceiver",
                    "com.google.android.gms/com.google.android.gms.update.SystemUpdateService\\$Receiver",
                    "com.google.android.gms/com.google.android.gms.update.SystemUpdateService\\$SecretCodeReceiver",
                ]
                for comp in gms_update_components:
                    adb_su(serial, f"pm disable '{comp}' 2>/dev/null || true")

                # Restrict Play Store and GMS background activity
                adb_su(serial, "cmd appops set com.android.vending RUN_IN_BACKGROUND deny 2>/dev/null || true")
                adb_su(serial, "cmd appops set com.google.android.gms RUN_IN_BACKGROUND deny 2>/dev/null || true")

                # Disable Play Protect (prevents scanning / removing modules)
                adb_su(serial, "settings put global package_verifier_user_consent -1")

                dbg(f"  {name}: Device auto-update hardening applied")
            except Exception as e:
                dbg(f"  {name}: Auto-update hardening warning (non-fatal): {e}")

        # ── Build summary results ──
        total = len(connected)

        # Magisk settings
        if len(magisk_ok_names) == total:
            result["magisk_settings"] = True
            result["magisk_settings_detail"] = f"su auto-grant on {total} instance{'s' if total != 1 else ''}"
        elif magisk_ok_names:
            result["magisk_settings"] = True
            result["magisk_settings_detail"] = f"{len(magisk_ok_names)}/{total} OK, failed: {', '.join(magisk_fail_names)}"
        else:
            result["magisk_settings_detail"] = f"Failed on all {total} instances"

        # Root policies (UID 0 + 2000)
        if len(policy_ok_names) == total:
            result["shell_root"] = True
            result["shell_root_detail"] = f"UID 0 + 2000 on {total} instance{'s' if total != 1 else ''}"
        elif policy_ok_names:
            result["shell_root"] = True
            result["shell_root_detail"] = f"{len(policy_ok_names)}/{total} OK, failed: {', '.join(policy_fail_names)}"
        else:
            result["shell_root_detail"] = f"Failed on all {total} instances"

        # Checker root
        checker_done = len(checker_ok_names)
        checker_total_applicable = checker_done + len(checker_fail_names)
        if checker_missing_names and not checker_done and not checker_fail_names:
            result["checker_root_detail"] = f"Checker not installed on {', '.join(checker_missing_names)}"
        elif checker_done == total:
            result["checker_root"] = True
            result["checker_root_detail"] = f"Granted on {total} instance{'s' if total != 1 else ''}"
        elif checker_done > 0:
            result["checker_root"] = True
            parts = [f"{checker_done} granted"]
            if checker_fail_names:
                parts.append(f"failed: {', '.join(checker_fail_names)}")
            if checker_missing_names:
                parts.append(f"not installed: {', '.join(checker_missing_names)}")
            result["checker_root_detail"] = "; ".join(parts)
        else:
            parts = []
            if checker_fail_names:
                parts.append(f"Failed: {', '.join(checker_fail_names)}")
            if checker_missing_names:
                parts.append(f"Not installed: {', '.join(checker_missing_names)}")
            result["checker_root_detail"] = "; ".join(parts) or "No instances processed"

        return result

    # ── Launch Diagnostics ──

    def diagnose_launch_readiness(self, engine_dir):
        """Run diagnostic checks to identify why BlueStacks fails to launch.

        Returns a dict with detailed status of every component BS needs:
        - bluestacks.conf: exists, size, line count, has installed_images,
          has at least one instance block, valid format
        - initrd: exists, is gzip, size
        - Engine directory: exists, has instance folders
        - Instance details: data.qcow2 presence, subdirs

        This helps us identify the ACTUAL cause of "Failed to read
        configuration file" errors without needing access to the test machine.
        """
        import subprocess, gzip as gz

        diag = {
            "conf": {},
            "initrd": {},
            "engine": {},
            "instances": [],
            "overall": "unknown",
        }

        # ── bluestacks.conf ──
        try:
            engine_path = Path(engine_dir) if engine_dir else self._BS_ENGINE
            conf_path = engine_path.parent / "bluestacks.conf"
            if conf_path.is_file():
                stat = conf_path.stat()
                diag["conf"]["exists"] = True
                diag["conf"]["size"] = stat.st_size
                diag["conf"]["size_human"] = f"{stat.st_size:,} bytes"

                with open(conf_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                lines = content.strip().split("\n")
                diag["conf"]["line_count"] = len(lines)

                # Check for key markers
                diag["conf"]["has_installed_images"] = any(
                    l.startswith("bst.installed_images") for l in lines
                )
                # Extract installed_images value
                for l in lines:
                    if l.startswith("bst.installed_images"):
                        diag["conf"]["installed_images"] = l.split("=", 1)[1].strip().strip('"')
                        break

                # Count instance blocks
                instance_names = set()
                for l in lines:
                    if l.startswith("bst.instance."):
                        parts = l.split(".")
                        if len(parts) >= 3:
                            instance_names.add(parts[2])
                diag["conf"]["instance_blocks"] = sorted(instance_names)
                diag["conf"]["instance_block_count"] = len(instance_names)

                # Check for truncation indicators
                diag["conf"]["ends_with_newline"] = content.endswith("\n")
                # Check last line for incomplete entries
                last_line = lines[-1] if lines else ""
                diag["conf"]["last_line_has_equals"] = "=" in last_line
                diag["conf"]["last_line_preview"] = last_line[:80] if last_line else ""

                # Check uchg flag
                ls = subprocess.run(["ls", "-lO", str(conf_path)],
                                    capture_output=True, text=True, timeout=5)
                diag["conf"]["has_uchg"] = "uchg" in ls.stdout

                # Check format validity
                bad_lines = 0
                for l in lines:
                    l = l.strip()
                    if l and "=" not in l and not l.startswith("#"):
                        bad_lines += 1
                diag["conf"]["format_bad_lines"] = bad_lines
                diag["conf"]["format_ok"] = bad_lines == 0
            else:
                diag["conf"]["exists"] = False
        except Exception as e:
            diag["conf"]["error"] = str(e)

        # ── initrd ──
        try:
            initrd_path = self._BS_APP / "Contents" / "img" / "initrd_hvf.img"
            if initrd_path.is_file():
                diag["initrd"]["exists"] = True
                diag["initrd"]["size"] = initrd_path.stat().st_size
                diag["initrd"]["size_human"] = f"{initrd_path.stat().st_size:,} bytes"
                # Check if it's a valid gzip
                try:
                    with open(initrd_path, "rb") as f:
                        magic = f.read(2)
                    diag["initrd"]["is_gzip"] = magic == b'\x1f\x8b'
                except Exception:
                    diag["initrd"]["is_gzip"] = False
            else:
                diag["initrd"]["exists"] = False
        except Exception as e:
            diag["initrd"]["error"] = str(e)

        # ── Engine directory & instances ──
        try:
            engine = Path(engine_dir) if engine_dir else self._BS_ENGINE
            diag["engine"]["exists"] = engine.is_dir()
            if engine.is_dir():
                # List instance folders
                for child in sorted(engine.iterdir()):
                    if child.is_dir() and child.name != "UserData":
                        inst = {
                            "name": child.name,
                            "has_data_qcow2": (child / "data.qcow2").is_file(),
                        }
                        if (child / "data.qcow2").is_file():
                            inst["data_qcow2_size"] = (child / "data.qcow2").stat().st_size
                            inst["data_qcow2_size_human"] = (
                                f"{(child / 'data.qcow2').stat().st_size / (1024*1024):.0f} MB"
                            )
                        # List subdirs
                        subdirs = [d.name for d in child.iterdir() if d.is_dir()]
                        inst["subdirs"] = subdirs
                        diag["instances"].append(inst)
        except Exception as e:
            diag["engine"]["error"] = str(e)

        # ── Overall assessment ──
        issues = []
        if not diag["conf"].get("exists"):
            issues.append("bluestacks.conf MISSING")
        elif diag["conf"].get("size", 0) < 1000:
            issues.append(f"bluestacks.conf too small ({diag['conf'].get('size', 0)} bytes)")
        elif not diag["conf"].get("format_ok"):
            issues.append(f"bluestacks.conf has {diag['conf'].get('format_bad_lines', 0)} malformed lines")
        elif not diag["conf"].get("has_installed_images"):
            issues.append("bluestacks.conf missing bst.installed_images key")
        elif diag["conf"].get("instance_block_count", 0) == 0:
            issues.append("bluestacks.conf has no instance blocks")

        if not diag["initrd"].get("exists"):
            issues.append("initrd_hvf.img MISSING")
        elif not diag["initrd"].get("is_gzip"):
            issues.append("initrd_hvf.img is NOT valid gzip")

        if not diag["engine"].get("exists"):
            issues.append("Engine directory MISSING")
        elif not diag["instances"]:
            issues.append("No instance directories found")

        if issues:
            diag["overall"] = "ISSUES FOUND"
            diag["issues"] = issues
        else:
            diag["overall"] = "OK"
            diag["issues"] = []

        return diag

    # ── Randomize tab ──

    def refresh_instances(self, engine_dir, bs_dir):
        """Discover instances, probe ADB status. Returns list of instance dicts."""
        if not engine_dir:
            return {"error": "Engine directory not set", "instances": []}

        conf_path = Path(engine_dir).parent / "bluestacks.conf"
        if not conf_path.is_file():
            return {"error": f"bluestacks.conf not found at {conf_path}", "instances": []}

        bs_dir = bs_dir or randomize_instances.DEFAULT_BLUESTACKS_DIR
        adb_exe = randomize_instances.find_adb_exe(bs_dir)

        instances = randomize_instances.discover_instances_from_conf(conf_path)
        if not instances:
            return {"error": None, "instances": []}

        results = []
        for inst in instances:
            serial = f"{randomize_instances.DEFAULT_HOST}:{inst.adb_port}"
            is_running = False
            has_root = False

            if randomize_instances.adb_connect(adb_exe, serial):
                is_running = randomize_instances.probe_boot_completed(adb_exe, serial)
                if is_running:
                    has_root = randomize_instances.probe_root(adb_exe, serial)

            print(f"[scan] {inst.name} :{inst.adb_port} running={is_running} root={has_root}")

            results.append({
                "name": inst.name,
                "port": inst.adb_port,
                "display": inst.display_name or "",
                "running": is_running,
                "root": has_root,
            })

        return {"error": None, "instances": results}

    def do_randomize(self, instance_names, engine_dir, bs_dir, do_profile, skip_reboot):
        """Run randomization on selected instances with per-instance progress."""
        if not self._acquire_running():
            return {"error": "An operation is already running"}

        def _emit_progress(inst_name, step_index, status, text):
            """Send progress update to frontend."""
            if self._window:
                import json as _j
                try:
                    self._window.evaluate_js(
                        f"if(window.onRandProgress)"
                        f"window.onRandProgress({_j.dumps(inst_name)},{step_index},{_j.dumps(status)},{_j.dumps(text)})"
                    )
                except Exception:
                    pass

        def _emit_instance_done(inst_name, success):
            """Signal that an instance is fully done."""
            if self._window:
                import json as _j
                try:
                    self._window.evaluate_js(
                        f"if(window.onRandInstanceDone)"
                        f"window.onRandInstanceDone({_j.dumps(inst_name)},{'true' if success else 'false'})"
                    )
                except Exception:
                    pass

        def _emit_results(results_data):
            """Send before/after results to frontend for the results modal."""
            if self._window:
                import json as _json
                try:
                    payload = _json.dumps(results_data)
                    self._window.evaluate_js(
                        f"if(window.onRandResults)window.onRandResults({payload})"
                    )
                except Exception:
                    pass

        def _run():
            try:
                conf_path = Path(engine_dir).parent / "bluestacks.conf"
                bs = bs_dir or randomize_instances.DEFAULT_BLUESTACKS_DIR
                adb_exe = randomize_instances.find_adb_exe(bs)
                all_instances = randomize_instances.discover_instances_from_conf(conf_path)
                name_set = set(instance_names)
                selected = [i for i in all_instances if i.name in name_set]

                # Collect before/after data for each instance
                all_results = []  # list of {name, display, before, after}

                # Step indices:
                # With profile:    0=connect, 1=profile, 2=identifiers, 3=cache, 4=reboot
                # Without profile: 0=connect, 1=identifiers, 2=cache, 3=reboot
                for inst in selected:
                    serial = f"{randomize_instances.DEFAULT_HOST}:{inst.adb_port}"
                    disp = inst.display_name or inst.name
                    step = 0

                    # Step 0: Connect & verify
                    _emit_progress(inst.name, step, 'active', 'Connecting...')
                    if not randomize_instances.adb_connect(adb_exe, serial):
                        _emit_progress(inst.name, step, 'fail', 'Unreachable')
                        print(f"[skip] {inst.name} ({disp}): not reachable")
                        _emit_instance_done(inst.name, False)
                        continue
                    if not randomize_instances.probe_boot_completed(adb_exe, serial):
                        _emit_progress(inst.name, step, 'fail', 'Not running')
                        print(f"[skip] {inst.name} ({disp}): not running")
                        _emit_instance_done(inst.name, False)
                        continue
                    if not randomize_instances.probe_root(adb_exe, serial):
                        _emit_progress(inst.name, step, 'fail', 'No root')
                        print(f"[skip] {inst.name} ({disp}): no root")
                        _emit_instance_done(inst.name, False)
                        continue
                    _emit_progress(inst.name, step, 'done', 'Connected')
                    print(f"[ok]   {inst.name} ({disp}): connected")

                    # ── Capture BEFORE state ──
                    before = {}
                    try:
                        before = randomize_instances.read_identifiers(adb_exe, serial)
                    except Exception:
                        pass

                    # Step: Profile (optional)
                    if do_profile:
                        step += 1
                        _emit_progress(inst.name, step, 'active', 'Profile...')
                        print(f"[do]   {inst.name} ({disp}): randomize profile")
                        try:
                            chosen = randomize_instances.randomize_profile(adb_exe, serial)
                            _emit_progress(inst.name, step, 'done', chosen)
                            print(f"[ok]   {inst.name}: profile -> {chosen}")
                        except Exception as e:
                            _emit_progress(inst.name, step, 'fail', 'Failed')
                            print(f"[fail] {inst.name}: profile: {e}")

                    # Step: Identifiers
                    step += 1
                    _emit_progress(inst.name, step, 'active', 'Identifiers...')
                    print(f"[do]   {inst.name} ({disp}): reset identifiers")
                    try:
                        randomize_instances.reset_identifiers(adb_exe, serial)
                        _emit_progress(inst.name, step, 'done', 'Reset')
                    except Exception as e:
                        _emit_progress(inst.name, step, 'fail', 'Failed')
                        print(f"[fail] {inst.name}: {e}")
                        _emit_instance_done(inst.name, False)
                        continue

                    # Sync Settings database (android_id, device_name, etc.)
                    try:
                        randomize_instances.sync_settings_db(adb_exe, serial)
                        print(f"[ok]   {inst.name}: settings db synced")
                    except Exception as e:
                        print(f"[warn] {inst.name}: settings db sync: {e}")

                    # ── Capture AFTER state ──
                    after = {}
                    try:
                        after = randomize_instances.read_identifiers(adb_exe, serial)
                    except Exception:
                        pass

                    all_results.append({
                        "name": inst.name,
                        "display": disp,
                        "before": before,
                        "after": after,
                    })

                    # Step: Dalvik cache
                    step += 1
                    _emit_progress(inst.name, step, 'active', 'Cache...')
                    print(f"[do]   {inst.name} ({disp}): clear dalvik cache")
                    try:
                        randomize_instances.clear_dalvik_cache(adb_exe, serial)
                        _emit_progress(inst.name, step, 'done', 'Cleared')
                        print(f"[ok]   {inst.name}: dalvik cache cleared")
                    except Exception as e:
                        _emit_progress(inst.name, step, 'fail', 'Failed')
                        print(f"[fail] {inst.name}: dalvik cache: {e}")

                    # Step: Reboot (or restart target apps if skipped)
                    step += 1
                    if skip_reboot:
                        # Force-stop target apps so they restart with fresh identity
                        try:
                            stopped = randomize_instances.restart_target_apps(adb_exe, serial)
                            if stopped:
                                _emit_progress(inst.name, step, 'done', f'Apps restarted ({len(stopped)})')
                                print(f"[ok]   {inst.name}: force-stopped {', '.join(stopped)}")
                            else:
                                _emit_progress(inst.name, step, 'done', 'Skipped (no apps)')
                                print(f"[ok]   {inst.name}: done (no target apps running)")
                        except Exception as e:
                            _emit_progress(inst.name, step, 'done', 'Skipped')
                            print(f"[warn] {inst.name}: restart_target_apps: {e}")
                    else:
                        _emit_progress(inst.name, step, 'active', 'Rebooting...')
                        print(f"[do]   {inst.name}: reboot")
                        randomize_instances.reboot_instance(adb_exe, serial)
                        _emit_progress(inst.name, step, 'done', 'Rebooted')
                        print(f"[ok]   {inst.name}: done (reboot issued)")

                    _emit_instance_done(inst.name, True)

                    # Refresh VPN status after identity change
                    if self._vpn_manager:
                        try:
                            self._vpn_manager.refresh_status(inst.name, serial)
                        except Exception:
                            pass

                # ── Emit results to frontend ──
                if all_results:
                    _emit_results(all_results)

                print("\nRandomization complete.")
            except Exception as e:
                print(f"[Error] {e}")
            finally:
                self._running = False
                if self._window:
                    try:
                        self._window.evaluate_js(
                            "if(window.onOperationDone)window.onOperationDone('randomize')"
                        )
                    except Exception:
                        pass

        self._run_with_log(_run)
        return {"error": None}

    # ── Base-image instance creation ──

    _BASE_IMAGE_URL = "https://ntii.io/base.qcow2"

    def find_base_image(self):
        """Locate the pre-built base image (data.qcow2 or base-image.qcow2).

        Searches in order:
          1. DATA_DIR/images/*/data.qcow2  (wizard download location)
          2. DATA_DIR/base-image.qcow2     (direct download location)
          3. BUNDLE_DIR/base-image.qcow2   (if bundled with dev checkout)
          4. dist/base-image.qcow2         (dev build directory)

        Returns dict with 'found' (bool), 'path' (str or None),
        and 'download_url' / 'download_to' when not found.
        """
        # Check wizard-downloaded images first (images/a13/data.qcow2 etc.)
        images_dir = os.path.join(DATA_DIR, "images")
        if os.path.isdir(images_dir):
            for subdir in sorted(Path(images_dir).iterdir()):
                candidate = subdir / "data.qcow2"
                if candidate.is_file():
                    return {"found": True, "path": str(candidate)}

        # Then check explicit base-image paths
        candidates = [
            os.path.join(DATA_DIR, "base-image.qcow2"),
            os.path.join(BUNDLE_DIR, "base-image.qcow2"),
            os.path.join(BUNDLE_DIR, "..", "dist", "base-image.qcow2"),
        ]
        for p in candidates:
            rp = os.path.realpath(p)
            if os.path.isfile(rp):
                return {"found": True, "path": rp}
        return {
            "found": False,
            "path": None,
            "download_url": self._BASE_IMAGE_URL,
            "download_to": os.path.join(DATA_DIR, "base-image.qcow2"),
        }

    def download_base_image(self):
        """Download the base image from ntii.io to DATA_DIR.

        Returns dict with 'error' (None on success) and 'path'.
        Progress is emitted via _emit_download_progress().
        """
        if not self._acquire_running():
            return {"error": "An operation is already running"}

        dest = os.path.join(DATA_DIR, "base-image.qcow2")

        def _run():
            import urllib.request

            try:
                self._emit(f"Downloading base image from {self._BASE_IMAGE_URL} ...")
                os.makedirs(DATA_DIR, exist_ok=True)

                partial = dest + ".part"
                req = urllib.request.Request(
                    self._BASE_IMAGE_URL,
                    headers={"User-Agent": "LukesMirage/2.2"},
                )
                with urllib.request.urlopen(req) as resp:
                    total = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    chunk_size = 256 * 1024  # 256 KB

                    with open(partial, "wb") as f:
                        while True:
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = int(downloaded * 100 / total)
                                self._emit_download_progress(pct, "downloading")

                # Rename .part → final only after complete download
                os.replace(partial, dest)
                size_mb = os.path.getsize(dest) / (1024 * 1024)
                self._emit(f"✓ Base image downloaded ({size_mb:.0f} MB)")
                self._emit(f"  Saved to: {dest}")
            except Exception as e:
                self._emit(f"✗ Download failed: {e}")
                # Clean up partial file
                try:
                    os.remove(dest + ".part")
                except OSError:
                    pass
            finally:
                if self._window:
                    try:
                        self._window.evaluate_js(
                            "if(window.onOperationDone)window.onOperationDone('download')"
                        )
                    except Exception:
                        pass

        self._run_with_log(_run)
        return {"error": None, "path": dest}

    def create_base_instance(self, base_image_path=None, instance_name=None, display_name=None,
                             engine_dir=None):
        """Create a new BlueStacks instance from a pre-built base image.

        Turnkey: creates the Engine directory, copies the qcow2, generates
        bluestacks.conf entries with unique identity, and registers in MIM.
        No existing instance or BlueStacks launch required.

        If base_image_path is None or empty, auto-discovers via find_base_image().
        """
        if not self._acquire_running():
            return {"error": "An operation is already running"}

        def _run():
            try:
                self._emit("Creating instance from base image...")

                # Auto-discover base image if not provided
                nonlocal base_image_path
                if not base_image_path:
                    loc = self.find_base_image()
                    if loc["found"]:
                        base_image_path = loc["path"]
                        self._emit(f"  Using base image: {base_image_path}")
                    else:
                        self._emit("✗ Base image not found (base-image.qcow2).")
                        self._emit(f"  Download from: {loc['download_url']}")
                        self._emit(f"  Or use the download_base_image() API to fetch it automatically.")
                        return

                # Determine engine dir + conf path for unlocking
                if engine_dir:
                    eng = Path(engine_dir)
                else:
                    eng = clone_instance.detect_engine_dir()
                if not eng:
                    self._emit("✗ Could not detect BlueStacks Engine directory")
                    return
                conf_path = eng.parent / "bluestacks.conf"

                # Unlock bluestacks.conf (uchg flag) for writing
                with conf_unlocked(conf_path):
                    result = clone_instance.create_instance_from_base(
                        base_image_path=base_image_path,
                        instance_name=instance_name,
                        display_name=display_name,
                        engine_dir=str(eng),
                        quiet=False,
                        on_progress=self._emit,
                    )

                if result.get("ok"):
                    self._emit(f"✓ Instance '{display_name}' created at port {result['adb_port']}")
                    self._emit(f"  Path: {result['path']}")
                    self._emit("Instance is ready — launch it from BlueStacks or the Manager tab.")
                else:
                    self._emit(f"✗ Failed: {result.get('error', 'unknown')}")
            except PermissionError as e:
                self._emit(f"✗ Permission denied: {e}")
            except Exception as e:
                self._emit(f"✗ Creation failed: {e}")

        self._run_with_log(_run)
        return {"error": None}

    # ── Clone tab ──

    def do_clone(self, source, clone_names, engine_dir, bs_dir, fix_mode, dry_run,
                 skip_bs_check=False, source_image_path=None, clone_display_names=None):
        """Run clone operation. source_image_path overrides engine_dir/source as the source directory."""
        if not self._acquire_running():
            return {"error": "An operation is already running"}

        def _run():
            import subprocess
            import time
            conf_was_locked = False
            conf_path = None
            mim_was_running = False
            try:
                ed = Path(engine_dir)
                bd = bs_dir or clone_instance.DEFAULT_BLUESTACKS_DIR
                if not skip_bs_check:
                    clone_instance.check_bluestacks_stopped(dry_run)

                src_dir = Path(source_image_path) if source_image_path else None

                if src_dir:
                    print("Cloning from source image — skipping master checks.")
                else:
                    print("Using engine instance as source.")

                # ── Stop MIM BEFORE cloning ──
                # MIM writes MimMetaData.json on exit, overwriting any
                # changes we make while it's running.  We must kill it
                # first so it flushes its in-memory state, THEN we clone
                # (which writes a fresh entry), THEN we relaunch MIM so
                # it picks up the new entry cleanly.
                if not dry_run:
                    try:
                        mim_check = subprocess.run(
                            ["pgrep", "-f", "HD-MultiInstanceManager"],
                            capture_output=True, text=True, timeout=5
                        )
                        if mim_check.returncode == 0 and mim_check.stdout.strip():
                            mim_was_running = True
                            print("Stopping MIM before clone (will relaunch after)...")
                            subprocess.run(
                                ["pkill", "-f", "HD-MultiInstanceManager"],
                                capture_output=True, timeout=5
                            )
                            time.sleep(3)  # Give MIM time to flush and exit
                    except Exception:
                        pass

                # ── Unlock bluestacks.conf (uchg) before cloning ──
                conf_path = ed.parent / "bluestacks.conf"
                conf_was_locked = False
                if conf_path.is_file():
                    ls_result = subprocess.run(
                        ["ls", "-lO", str(conf_path)],
                        capture_output=True, text=True, timeout=5
                    )
                    if "uchg" in ls_result.stdout:
                        conf_was_locked = True
                        print("Unlocking bluestacks.conf (admin password required)...")
                        safe_conf = shlex.quote(str(conf_path))
                        unlock_cmd = (
                            f'do shell script "chflags nouchg {safe_conf}"'
                            f' with administrator privileges'
                        )
                        cp = subprocess.run(
                            ["osascript", "-e", unlock_cmd],
                            capture_output=True, text=True, timeout=30
                        )
                        if cp.returncode != 0:
                            print("ERROR: Could not unlock bluestacks.conf — user cancelled or permission denied.")
                            return
                        print("bluestacks.conf unlocked.")

                # Ensure MimMetaData.json is writable (BS convention is 666;
                # fresh installs create it with 644 which blocks clone writes)
                mim_path = ed / "UserData" / "MimMetaData.json"
                if mim_path.is_file():
                    try:
                        os.chmod(str(mim_path), 0o666)
                    except Exception:
                        pass
                # Also fix the UserData directory permissions
                if mim_path.parent.is_dir():
                    try:
                        os.chmod(str(mim_path.parent), 0o777)
                    except Exception:
                        pass

                display_map = clone_display_names if isinstance(clone_display_names, dict) else {}

                total = len(clone_names)
                for idx, name in enumerate(clone_names, 1):
                    print(f"\n[{idx}/{total}] {source} → {name}")
                    display_override = display_map.get(name)
                    clone_instance.clone_instance(
                        source, name, ed, bd, fix_mode, dry_run,
                        quiet=True, source_dir_override=src_dir,
                        display_name_override=display_override
                    )
                    print(f"[{idx}/{total}] Complete")
                print(f"\nAll {total} clone(s) done!")

                # ── Auto-randomize cloned instances ──
                if not dry_run:
                    print("\n--- Auto-randomizing cloned instances ---")
                    # Re-read bluestacks.conf to get new ADB ports
                    conf_path_fresh = ed.parent / "bluestacks.conf"
                    all_inst = randomize_instances.discover_instances_from_conf(conf_path_fresh)
                    clone_set = set(clone_names)
                    cloned_inst = [i for i in all_inst if i.name in clone_set]

                    bs = bs_dir or randomize_instances.DEFAULT_BLUESTACKS_DIR
                    adb_exe = randomize_instances.find_adb_exe(bs)

                    for ci in cloned_inst:
                        cname = ci.name
                        cdisp = ci.display_name or cname
                        cserial = f"{randomize_instances.DEFAULT_HOST}:{ci.adb_port}"
                        print(f"\n[auto-rand] Starting {cname} ({cdisp})...")

                        # Start the instance
                        try:
                            subprocess.Popen(
                                ["open", "-na", "/Applications/BlueStacks.app",
                                 "--args", "--instance", cname],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                            )
                        except Exception as e:
                            print(f"[auto-rand] Failed to start {cname}: {e}")
                            continue

                        # Wait for boot (poll up to 90 seconds)
                        booted = False
                        for _wait in range(45):
                            time.sleep(2)
                            randomize_instances.adb_connect(adb_exe, cserial)
                            if randomize_instances.probe_boot_completed(adb_exe, cserial):
                                booted = True
                                break
                        if not booted:
                            print(f"[auto-rand] {cname}: boot timeout — skipping randomize")
                            continue

                        # Wait a bit more for Magisk to finish init
                        time.sleep(5)

                        # Check root
                        if not randomize_instances.probe_root(adb_exe, cserial):
                            print(f"[auto-rand] {cname}: no root — skipping randomize")
                            continue

                        # Randomize profile
                        try:
                            chosen = randomize_instances.randomize_profile(adb_exe, cserial)
                            print(f"[auto-rand] {cname}: profile -> {chosen}")
                        except Exception as e:
                            print(f"[auto-rand] {cname}: profile failed: {e}")

                        # Reset identifiers
                        try:
                            randomize_instances.reset_identifiers(adb_exe, cserial)
                            print(f"[auto-rand] {cname}: identifiers reset")
                        except Exception as e:
                            print(f"[auto-rand] {cname}: identifiers failed: {e}")

                        # Sync settings DB
                        try:
                            randomize_instances.sync_settings_db(adb_exe, cserial)
                            print(f"[auto-rand] {cname}: settings db synced")
                        except Exception as e:
                            print(f"[auto-rand] {cname}: settings sync failed: {e}")

                        # Ensure Magisk root policies are granted in the clone
                        try:
                            for policy_cmd in [
                                'magisk --sqlite "CREATE TABLE IF NOT EXISTS policies (uid INT, policy INT, until INT, logging INT, notification INT, PRIMARY KEY(uid))"',
                                'magisk --sqlite "INSERT OR REPLACE INTO policies VALUES(0, 2, 0, 1, 0)"',
                                'magisk --sqlite "INSERT OR REPLACE INTO policies VALUES(2000, 2, 0, 1, 0)"',
                                'magisk --sqlite "CREATE TABLE IF NOT EXISTS strings (key TEXT, value TEXT, PRIMARY KEY(key))"',
                            ]:
                                subprocess.run(
                                    [adb_exe, "-s", cserial, "shell", f'su -c \'{policy_cmd}\''],
                                    capture_output=True, text=True, timeout=10
                                )
                            print(f"[auto-rand] {cname}: Magisk root policies ensured")
                        except Exception as e:
                            print(f"[auto-rand] {cname}: Magisk policies: {e}")

                        # Clear dalvik cache
                        try:
                            randomize_instances.clear_dalvik_cache(adb_exe, cserial)
                        except Exception:
                            pass

                        # Reboot to apply all changes
                        try:
                            randomize_instances.reboot_instance(adb_exe, cserial)
                            print(f"[auto-rand] {cname}: rebooted with new identity")
                        except Exception:
                            print(f"[auto-rand] {cname}: reboot failed — may need manual restart")

                    print("\n--- Auto-randomization complete ---")

            except SystemExit:
                print("\n[Aborted] Operation stopped (see above).")
            except Exception as e:
                print(f"\n[Error] {e}")
            finally:
                # ── Re-lock bluestacks.conf if we unlocked it ──
                if conf_was_locked and conf_path.is_file():
                    try:
                        safe_conf = shlex.quote(str(conf_path))
                        relock_cmd = (
                            f'do shell script "chflags uchg {safe_conf}"'
                            f' with administrator privileges'
                        )
                        subprocess.run(
                            ["osascript", "-e", relock_cmd],
                            capture_output=True, text=True, timeout=30
                        )
                        print("bluestacks.conf re-locked.")
                    except Exception:
                        pass
                # ── Always launch MIM after cloning so new instances are ready ──
                if not dry_run:
                    try:
                        # Kill any lingering MIM that may have respawned
                        subprocess.run(
                            ["pkill", "-f", "HD-MultiInstanceManager"],
                            capture_output=True, timeout=5
                        )
                        time.sleep(0.5)
                        print("Launching Multi-Instance Manager...")
                        subprocess.Popen(
                            ["open", "/Applications/BlueStacksMIM.app"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )
                        print("MIM launched — new instances ready.")
                    except Exception:
                        pass
                self._running = False
                if self._window:
                    try:
                        self._window.evaluate_js("if(window.onOperationDone)window.onOperationDone('clone')")
                    except Exception:
                        pass

        self._run_with_log(_run)
        return {"error": None}

    def do_delete(self, instance_names, engine_dir):
        """Delete one or more instances. Handles uchg unlock/relock and MIM restart."""
        if not self._acquire_running():
            return {"error": "An operation is already running"}

        def _run():
            import subprocess
            import time
            conf_was_locked = False
            conf_path = None
            try:
                ed = Path(engine_dir)

                # ── Stop MIM BEFORE deleting ──
                try:
                    mim_check = subprocess.run(
                        ["pgrep", "-f", "HD-MultiInstanceManager"],
                        capture_output=True, text=True, timeout=5
                    )
                    if mim_check.returncode == 0 and mim_check.stdout.strip():
                        print("Stopping MIM before delete (will relaunch after)...")
                        subprocess.run(
                            ["pkill", "-f", "HD-MultiInstanceManager"],
                            capture_output=True, timeout=5
                        )
                        time.sleep(1)
                except Exception:
                    pass

                # ── Unlock bluestacks.conf (uchg) ──
                conf_path = ed.parent / "bluestacks.conf"
                if conf_path.is_file():
                    ls_result = subprocess.run(
                        ["ls", "-lO", str(conf_path)],
                        capture_output=True, text=True, timeout=5
                    )
                    if "uchg" in ls_result.stdout:
                        conf_was_locked = True
                        print("Unlocking bluestacks.conf (admin password required)...")
                        safe_conf = shlex.quote(str(conf_path))
                        unlock_cmd = (
                            f'do shell script "chflags nouchg {safe_conf}"'
                            f' with administrator privileges'
                        )
                        cp = subprocess.run(
                            ["osascript", "-e", unlock_cmd],
                            capture_output=True, text=True, timeout=30
                        )
                        if cp.returncode != 0:
                            print("ERROR: Could not unlock bluestacks.conf — user cancelled or permission denied.")
                            return
                        print("bluestacks.conf unlocked.")

                # ── Delete each instance ──
                total = len(instance_names)
                for idx, name in enumerate(instance_names, 1):
                    print(f"\n[{idx}/{total}] Deleting {name}...")
                    clone_instance.delete_instance(name, ed, dry_run=False, quiet=True)
                    print(f"[{idx}/{total}] Deleted")
                print(f"\nAll {total} instance(s) deleted!")

            except Exception as e:
                print(f"\n[Error] {e}")
            finally:
                # ── Re-lock bluestacks.conf if we unlocked it ──
                if conf_was_locked and conf_path and conf_path.is_file():
                    try:
                        safe_conf = shlex.quote(str(conf_path))
                        relock_cmd = (
                            f'do shell script "chflags uchg {safe_conf}"'
                            f' with administrator privileges'
                        )
                        subprocess.run(
                            ["osascript", "-e", relock_cmd],
                            capture_output=True, text=True, timeout=30
                        )
                        print("bluestacks.conf re-locked.")
                    except Exception:
                        pass
                # ── Relaunch MIM ──
                try:
                    subprocess.run(
                        ["pkill", "-f", "HD-MultiInstanceManager"],
                        capture_output=True, timeout=5
                    )
                    time.sleep(0.5)
                    print("Launching Multi-Instance Manager...")
                    subprocess.Popen(
                        ["open", "/Applications/BlueStacksMIM.app"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    print("MIM launched — instances updated.")
                except Exception:
                    pass
                self._running = False
                if self._window:
                    try:
                        self._window.evaluate_js("if(window.onOperationDone)window.onOperationDone('delete')")
                    except Exception:
                        pass

        self._run_with_log(_run)
        return {"error": None}

    # ── Instance Start / Stop ──

    def start_instance(self, instance_name):
        """Start a single BlueStacks Air instance by launching with --instance flag."""
        import subprocess as _sp
        try:
            bs_dir = self._detect_bs_dir()
            bs_exe = os.path.join(bs_dir, "MacOS", "BlueStacks") if bs_dir else None

            if not bs_exe or not os.path.isfile(bs_exe):
                return {"error": f"BlueStacks executable not found"}

            _sp.Popen(
                [bs_exe, "--instance", instance_name],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            dbg(f"start_instance: launched {instance_name}")
            return {"error": None, "name": instance_name}
        except Exception as e:
            return {"error": str(e)}

    def stop_instance(self, instance_name, engine_dir, bs_dir):
        """Stop a running BlueStacks instance gracefully via ADB power-off.

        Strategy:
        1. Find the instance's ADB port from bluestacks.conf
        2. Send 'su -c reboot -p' via ADB (graceful power off)
        3. Fallback: kill the QEMU process for this instance
        """
        import subprocess as _sp
        try:
            if not engine_dir:
                return {"error": "Engine directory not set"}

            conf_path = Path(engine_dir).parent / "bluestacks.conf"
            if not conf_path.is_file():
                return {"error": "bluestacks.conf not found"}

            bs = bs_dir or clone_instance.DEFAULT_BLUESTACKS_DIR
            adb_exe = randomize_instances.find_adb_exe(bs)

            # Find the instance's ADB port
            instances = randomize_instances.discover_instances_from_conf(conf_path)
            inst = None
            for i in instances:
                if i.name == instance_name:
                    inst = i
                    break

            if not inst:
                return {"error": f"Instance '{instance_name}' not found in config"}

            serial = f"127.0.0.1:{inst.adb_port}"

            # Try graceful shutdown via ADB
            try:
                if randomize_instances.adb_connect(adb_exe, serial):
                    cp = _sp.run(
                        [adb_exe, "-s", serial, "shell", "su", "-c", "reboot -p"],
                        capture_output=True, text=True, timeout=10
                    )
                    if cp.returncode == 0:
                        dbg(f"stop_instance: graceful shutdown sent to {instance_name}")
                        return {"error": None, "name": instance_name, "method": "adb"}
            except _sp.TimeoutExpired:
                pass
            except Exception as e:
                dbg(f"stop_instance: ADB shutdown failed for {instance_name}: {e}")

            # Fallback: kill the QEMU/BlueStacks process for this specific instance
            try:
                cp = _sp.run(
                    ["pkill", "-f", f"BlueStacks.*--instance.*{re.escape(instance_name)}"],
                    capture_output=True, text=True, timeout=5
                )
                if cp.returncode == 0:
                    dbg(f"stop_instance: killed process for {instance_name}")
                    return {"error": None, "name": instance_name, "method": "kill"}
            except Exception:
                pass

            return {"error": f"Could not stop {instance_name} — try stopping it manually"}
        except Exception as e:
            return {"error": str(e)}

    # ── CDP (removed — stubs for frontend compatibility) ──

    def cdp_toggle(self, instance_name, adb_port, enable):
        return {"error": None, "enabled": False}

    def cdp_enable_all(self, instances):
        return {"error": None}

    def cdp_disable_all(self):
        return {"error": None}

    def cdp_get_statuses(self):
        return {}

    def cdp_reload(self, instance_name):
        return {"error": None}

    # ── VPN / Proxy Management ──

    def _get_vpn_manager(self):
        """Lazily initialize VPNManager."""
        if self._vpn_manager is None:
            bs_dir = self._detect_bs_dir() or clone_instance.DEFAULT_BLUESTACKS_DIR
            adb_exe = randomize_instances.find_adb_exe(bs_dir)
            self._vpn_manager = vpn_manager.VPNManager(
                adb_exe=adb_exe,
                on_status_change=self._vpn_status_callback,
            )
            # Auto-restore Suborbital credentials if saved
            settings = load_settings()
            sub_user = settings.get("suborbital_email", "")
            sub_pass = settings.get("suborbital_password", "")
            if sub_user and sub_pass:
                self._vpn_manager.set_suborbital_credentials(sub_user, sub_pass)
        return self._vpn_manager

    def _vpn_status_callback(self, instance_name, status_dict):
        """Push VPN status changes to the frontend."""
        if self._window:
            try:
                self._window.evaluate_js(
                    f"if(window.onVpnStatus)"
                    f"window.onVpnStatus({json.dumps(instance_name)},{json.dumps(status_dict)})"
                )
            except Exception:
                pass

    def vpn_apply(self, instance_name, adb_port, server, port, username, password):
        """Apply proxy config and connect VPN on one instance."""
        try:
            mgr = self._get_vpn_manager()
            serial = f"127.0.0.1:{adb_port}"
            return mgr.apply_proxy(instance_name, serial, server, int(port), username, password)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def vpn_apply_bulk(self, assignments):
        """Apply proxy to multiple instances.

        assignments: [{instance_name, adb_port, server, port, username, password}, ...]
        """
        try:
            mgr = self._get_vpn_manager()
            # Convert adb_port to serial format
            mapped = []
            for a in assignments:
                mapped.append({
                    "instance_name": a["instance_name"],
                    "serial": f"127.0.0.1:{a['adb_port']}",
                    "server": a["server"],
                    "port": int(a["port"]),
                    "username": a.get("username", ""),
                    "password": a.get("password", ""),
                })
            return mgr.apply_proxy_bulk(mapped)
        except Exception as e:
            return {"error": str(e)}

    def vpn_disconnect(self, instance_name, adb_port):
        """Disconnect VPN for one instance."""
        try:
            mgr = self._get_vpn_manager()
            serial = f"127.0.0.1:{adb_port}"
            return mgr.disconnect(instance_name, serial)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def vpn_disconnect_all(self, instances):
        """Disconnect VPN for all specified instances.

        instances: [{name, port}, ...]
        """
        try:
            mgr = self._get_vpn_manager()
            mapped = [{"name": i["name"], "serial": f"127.0.0.1:{i['port']}"} for i in instances]
            return mgr.disconnect_all(mapped)
        except Exception as e:
            return {"error": str(e)}

    def vpn_start_polling(self, instances):
        """Start background VPN status polling.

        instances: [{name, port}, ...]
        """
        try:
            mgr = self._get_vpn_manager()
            mapped = [{"name": i["name"], "serial": f"127.0.0.1:{i['port']}"} for i in instances]
            mgr.start_polling(mapped)
            return {"error": None}
        except Exception as e:
            return {"error": str(e)}

    def vpn_stop_polling(self):
        """Stop background VPN status polling."""
        try:
            if self._vpn_manager:
                self._vpn_manager.stop_polling()
            return {"error": None}
        except Exception as e:
            return {"error": str(e)}

    def vpn_read_config(self, instance_name, adb_port):
        """Read current proxy config from a device."""
        try:
            bs_dir = self._detect_bs_dir() or clone_instance.DEFAULT_BLUESTACKS_DIR
            adb_exe = randomize_instances.find_adb_exe(bs_dir)
            serial = f"127.0.0.1:{adb_port}"
            return vpn_manager.read_current_config(adb_exe, serial)
        except Exception as e:
            return {"error": str(e)}

    def vpn_get_statuses(self):
        """Get cached VPN statuses for all instances."""
        if self._vpn_manager:
            return self._vpn_manager.get_all_statuses()
        return {}

    def vpn_set_always_on(self, instance_name, enabled, server='', port=1337, username='', password=''):
        """Enable/disable always-on VPN for an instance.

        When enabled, the VPN manager will automatically reconnect if the
        tunnel drops. The proxy config is stored so it can be re-applied.
        """
        try:
            mgr = self._get_vpn_manager()
            if enabled:
                mgr.set_always_on(instance_name, {
                    'server': server, 'port': int(port),
                    'username': username, 'password': password,
                })
            else:
                mgr.set_always_on(instance_name, None)
            return {"error": None}
        except Exception as e:
            return {"error": str(e)}

    def vpn_set_kill_switch(self, instance_name, adb_port, enabled):
        """Enable/disable kill switch for an instance.

        When enabled, iptables rules block all non-local/non-tunnel traffic,
        preventing real IP leaks if the VPN drops.
        """
        try:
            mgr = self._get_vpn_manager()
            serial = f"127.0.0.1:{adb_port}"
            mgr.set_kill_switch(instance_name, serial, enabled)
            return {"error": None}
        except Exception as e:
            return {"error": str(e)}

    def vpn_suborbital_login(self, username, password):
        """Log in to Suborbital with account credentials.

        Verifies credentials by calling GET /user, fetches IPs and bandwidth,
        then persists on success. Returns user info + proxy count + balance.
        """
        try:
            mgr = self._get_vpn_manager()
            mgr.set_suborbital_credentials(username, password)
            user_info = mgr.verify_suborbital()

            # Also fetch proxies and bandwidth for the dashboard
            proxies = []
            balance = None
            try:
                proxies = mgr.fetch_proxies()
            except Exception:
                pass
            try:
                bw = mgr.fetch_bandwidth()
                balance = bw.get("balance", bw.get("remaining", None))
            except Exception:
                pass

            # Inject SOCKS5 credentials: port is always 1337, password is account password
            for p in proxies:
                p.setdefault("port", 1337)
                p["password"] = password

            # Persist credentials
            settings = load_settings()
            settings["suborbital_email"] = username
            settings["suborbital_password"] = password
            save_settings(settings)
            return {
                "error": None,
                "user": user_info,
                "proxies": proxies,
                "balance": balance,
            }
        except vpn_manager.SuborbitalError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    def vpn_suborbital_logout(self):
        """Clear Suborbital credentials."""
        try:
            mgr = self._get_vpn_manager()
            mgr.set_suborbital_credentials("", "")
            settings = load_settings()
            settings.pop("suborbital_email", None)
            settings.pop("suborbital_password", None)
            save_settings(settings)
            return {"error": None}
        except Exception as e:
            return {"error": str(e)}

    def vpn_fetch_proxies(self):
        """Fetch proxy list from Suborbital API."""
        try:
            mgr = self._get_vpn_manager()
            proxies = mgr.fetch_proxies()
            # Inject SOCKS5 credentials: port is always 1337, password is account password
            settings = load_settings()
            acct_pass = settings.get("suborbital_password", "")
            for p in proxies:
                p.setdefault("port", 1337)
                p["password"] = acct_pass
            return {"error": None, "proxies": proxies}
        except vpn_manager.SuborbitalError as e:
            return {"error": str(e), "proxies": []}
        except Exception as e:
            return {"error": str(e), "proxies": []}

    def vpn_save_state(self, proxy_pool, assignments):
        """Persist VPN proxy pool and assignments to settings JSON.

        Uses the global settings lock to ensure atomic read-modify-write,
        preventing concurrent vpnSaveState() calls from overwriting each other.
        """
        try:
            with _settings_lock:
                try:
                    with open(SETTINGS_FILE, "r") as f:
                        settings = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    settings = {}
                settings["vpn_proxy_pool"] = proxy_pool
                settings["vpn_assignments"] = assignments
                try:
                    with open(SETTINGS_FILE, "w") as f:
                        json.dump(settings, f, indent=2)
                except OSError:
                    pass
            return {"error": None}
        except Exception as e:
            return {"error": str(e)}

    def vpn_get_state(self):
        """Load persisted VPN state from settings JSON."""
        settings = load_settings()
        return {
            "vpn_proxy_pool": settings.get("vpn_proxy_pool", []),
            "vpn_assignments": settings.get("vpn_assignments", {}),
            "suborbital_email": settings.get("suborbital_email", ""),
            "suborbital_has_password": bool(settings.get("suborbital_password", "")),
        }

    def vpn_restore_always_on(self, instances=None):
        """Restore always-on VPN state from persisted settings on startup.

        Reads proxy pool + assignments from disk, re-enables always-on for
        instances whose assigned proxy has alwaysOn=true, then starts polling
        so the auto-reconnect loop kicks in.

        Discovers instance ports from bluestacks.conf directly (the frontend's
        initial scan_instances() doesn't include ADB ports, so we can't rely
        on the frontend to pass them).

        instances: optional [{name, port}, ...] — ignored, kept for compat
        """
        try:
            mgr = self._get_vpn_manager()
            settings = load_settings()
            pool = settings.get("vpn_proxy_pool", [])
            assignments = settings.get("vpn_assignments", {})
            engine_dir = settings.get("engine_dir", "")

            # Build lookup: proxy id -> proxy object
            proxy_by_id = {}
            for p in pool:
                pid = p.get("id")
                if pid is not None:
                    proxy_by_id[pid] = p

            restored = 0
            for inst_name, proxy_id in assignments.items():
                proxy = proxy_by_id.get(proxy_id)
                if not proxy or not proxy.get("alwaysOn"):
                    continue
                mgr.set_always_on(inst_name, {
                    "server": proxy.get("server", ""),
                    "port": int(proxy.get("port", 1337)),
                    "username": proxy.get("username", ""),
                    "password": proxy.get("password", ""),
                })
                restored += 1

            # Discover instance ports from bluestacks.conf (not from frontend)
            if restored > 0 and engine_dir:
                conf_path = Path(engine_dir).parent / "bluestacks.conf"
                if conf_path.is_file():
                    all_inst = randomize_instances.discover_instances_from_conf(conf_path)
                    # Only poll instances that have always-on assignments
                    ao_names = {name for name, pid in assignments.items()
                                if proxy_by_id.get(pid, {}).get("alwaysOn")}
                    mapped = [{"name": i.name, "serial": f"127.0.0.1:{i.adb_port}"}
                              for i in all_inst if i.name in ao_names]
                    if mapped:
                        mgr.start_polling(mapped)
                        dbg(f"Always-on restore: polling {len(mapped)} instances")

            return {"error": None, "restored": restored}
        except Exception as e:
            return {"error": str(e)}

    def _detect_bs_dir(self):
        """Find the BlueStacks install directory."""
        default = clone_instance.DEFAULT_BLUESTACKS_DIR
        if os.path.isdir(default):
            return default
        return None

    # ── Helpers ──

    def _run_with_log(self, fn):
        """Run fn in a thread with stdout/stderr redirected to the webview log."""
        import weakref
        capture = LogCapture(weakref.ref(self._window) if self._window else lambda: None)

        def wrapper():
            with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
                fn()
            capture.flush()

        threading.Thread(target=wrapper, daemon=True).start()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    global DEBUG
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--debug", action="store_true")
    args, _ = parser.parse_known_args()
    DEBUG = args.debug

    if DEBUG:
        dbg("=== Luke's Mirage GUI starting (pywebview, Mac, debug mode) ===")

    # Deploy promo_guard + loading screen if missing (fallback for pkg postinstall)
    setup_promo_guard()

    # Suppress BlueStacks ads at the host level (bluestacks.conf)
    disable_host_ads()

    api = Api()

    html_path = os.path.join(BUNDLE_DIR, "index.html")
    if not os.path.isfile(html_path):
        print(f"ERROR: index.html not found at {html_path}", file=sys.stderr)
        sys.exit(1)

    window = webview.create_window(
        APP_TITLE,
        url=html_path,
        js_api=api,
        width=1220,
        height=730,
        min_size=(780, 600),
        background_color="#1a1d21",
        text_select=True,
    )
    api.set_window(window)

    webview.start(debug=DEBUG)


if __name__ == "__main__":
    main()
