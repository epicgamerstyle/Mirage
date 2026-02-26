# Luke's Mirage

macOS manager for BlueStacks Android instances — device spoofing, rooting, cloning, and golden image deployment.

## Download

Grab the latest **mirage.pkg** installer and **golden image** from
[Releases](https://github.com/epicgamerstyle/Mirage/releases/latest).

The `.pkg` installer places the app in `/Applications` and sets up the loading-screen guard automatically. No Python needed.

## Project Structure

```
.
├── gui/                            # macOS GUI application
│   ├── gui.py                      # Main app (pywebview + bottle backend)
│   ├── index.html                  # Web UI frontend
│   ├── clone_instance.py           # Instance cloning logic
│   ├── randomize_instances.py      # Profile randomization
│   ├── vpn_manager.py              # SOCKS5 tunnel manager
│   ├── LukesMirage.spec            # PyInstaller build spec
│   ├── build_mac_app.sh            # Build script (.app + .pkg)
│   ├── requirements.txt            # Python dependencies
│   ├── images/                     # UI assets
│   │   ├── jorkspoofer-jspose.png
│   │   └── checker.png
│   ├── osmb-logo.gif               # Branding
│   ├── bs.png                      # BlueStacks icon
│   ├── bs-air.png                  # BlueStacks Air icon
│   ├── a13.png                     # Android 13 badge
│   └── a11.png                     # Android 11 badge
│
├── bluestacks/                     # BlueStacks modules & scripts
│   ├── root_bluestacks.sh          # One-shot root + Magisk installer
│   ├── magisk_bootstrap.sh         # Magisk module bootstrap
│   ├── create_golden_image.sh      # Golden image creation script
│   ├── magisk.rc                   # Magisk init.rc entries
│   ├── jorkSpoofer.sh              # Device spoofing CLI
│   ├── jorkSpoofer.command         # Double-click launcher
│   ├── jorkspoofer-switch.sh       # Quick profile switch
│   │
│   ├── kitsune.apk                 # Magisk (Kitsune fork)
│   ├── hooks.apk                   # LSPosed Chrome hooks module
│   ├── lsposed_mgr.apk            # LSPosed Manager
│   ├── ReZygisk-v1.0.0-rc.4-release.zip
│   ├── LukesMirage-Native-v2.0.0.zip  # jorkSpoofer native module
│   │
│   ├── profiles/                   # 34 device identity profiles
│   │   └── profiles_export/        # .conf files (Pixel, Samsung, etc.)
│   │
│   ├── checker/                    # Checker app (build + APK)
│   │   ├── Checker.apk
│   │   ├── src/
│   │   ├── res/
│   │   └── build_checker.sh
│   │
│   ├── community-checker/          # MirageStatus app (build + APK)
│   │   ├── MirageStatus.apk
│   │   ├── src/
│   │   ├── res/
│   │   └── build_status.sh
│   │
│   ├── golden_image/               # Golden image output (gitignored)
│   │   └── golden_info.json        # Image metadata
│   │
│   ├── lib/                        # Shared helpers
│   │   ├── promo_guard.sh          # Loading screen protector daemon
│   │   ├── su_grant.sh             # Auto-grant superuser
│   │   └── tz_detect.sh            # Timezone detection
│   │
│   ├── bin/                        # Native binaries
│   │   └── hev-socks5-tunnel       # SOCKS5 tunnel for VPN
│   │
│   ├── wallpaper.jpg               # Custom wallpaper
│   └── load.jpg                    # Custom loading screen
│
└── .gitignore
```

