# -*- mode: python ; coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════════════════════
# Luke's Mirage — PyInstaller spec file for macOS .app bundle
#
# Build:  pyinstaller --clean --noconfirm LukesMirage.spec
# Or:     ./build_mac_app.sh   (handles venv + build + DMG)
# ═══════════════════════════════════════════════════════════════════════════════

from PyInstaller.utils.hooks import collect_submodules

# pywebview + pyobjc have many dynamic imports that PyInstaller can't detect
hiddenimports = (
    collect_submodules('objc') +
    collect_submodules('AppKit') +
    collect_submodules('Foundation') +
    collect_submodules('WebKit') +
    collect_submodules('Quartz') +
    collect_submodules('webview') +
    ['bottle', 'proxy_tools', 'websocket']
)

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[
        ('../bluestacks/bin/hev-socks5-tunnel', 'bin'),
    ],
    datas=[
        # ── Co-located Python modules (imported by gui.py) ──
        ('clone_instance.py', '.'),
        ('randomize_instances.py', '.'),
        ('vpn_manager.py', '.'),
        # ── Web UI (gui/) ──
        ('index.html', '.'),
        ('osmb-logo.gif', '.'),
        ('bs.png', '.'),
        ('bs-air.png', '.'),
        ('a13.png', '.'),
        ('a11.png', '.'),
        ('images/jorkspoofer-jspose.png', 'images'),
        ('images/checker.png', 'images'),
        # ── Device profiles (bluestacks/) ──
        ('../bluestacks/profiles', 'profiles'),
        # ── Shell scripts (bluestacks/) ──
        ('../bluestacks/root_bluestacks.sh', '.'),
        ('../bluestacks/magisk_bootstrap.sh', '.'),
        ('../bluestacks/create_golden_image.sh', '.'),
        # ── Magisk / modules (bluestacks/) ──
        ('../bluestacks/magisk.rc', '.'),
        ('../bluestacks/kitsune.apk', '.'),
        ('../bluestacks/lsposed_mgr.apk', '.'),
        ('../bluestacks/lsposed_mgr.apk.idsig', '.'),
        ('../bluestacks/hooks.apk', '.'),
        ('../bluestacks/ReZygisk-v1.0.0-rc.4-release.zip', '.'),
        # ── Promo guard assets (bluestacks/) ──
        ('../bluestacks/lib/promo_guard.sh', '.'),
        ('../bluestacks/load.jpg', '.'),
        # ── Checker APKs (bluestacks/) ──
        ('../bluestacks/checker/Checker.apk', 'checker'),
        ('../bluestacks/checker/Checker.apk.idsig', 'checker'),
        ('../bluestacks/community-checker/MirageStatus.apk', 'community-checker'),
        ('../bluestacks/community-checker/MirageStatus.apk.idsig', 'community-checker'),
    ],
    hiddenimports=hiddenimports,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='LukesMirage',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch='arm64',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='LukesMirage',
)

app = BUNDLE(
    coll,
    name="Luke's Mirage.app",
    icon=None,  # Add .icns file here when available
    bundle_identifier='com.lukesmirage.manager',
    info_plist={
        'CFBundleDisplayName': "Luke's Mirage",
        'CFBundleShortVersionString': '2.2.0',
        'CFBundleVersion': '2.2.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '12.0',
        'NSAppleEventsUsageDescription': 'Luke\'s Mirage needs to control system events for BlueStacks management.',
    },
)
