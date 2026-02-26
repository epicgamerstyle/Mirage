# Luke's Mirage

macOS manager for BlueStacks Android instances — device spoofing, rooting, cloning, and golden image deployment.

## Download

Grab the latest **mirage.pkg** installer and **golden image** from
[Releases](https://github.com/epicgamerstyle/Mirage/releases/latest).

The `.pkg` installer places the app in `/Applications` and sets up the loading-screen guard automatically. No Python needed.

## Golden Image

The golden image (`base.qcow2`) is a pre-configured BlueStacks Android 13 disk with:

- **Magisk** (Kitsune) with root, Zygisk, and 6 modules pre-installed
- **jorkSpoofer** + **jorkSpoofer-Native** — device identity spoofing
- **ReZygisk** + **Zygisk LSPosed** — framework-level hooks
- **Cromite WebView** — hardened browser engine replacement
- **Systemless Hosts** — OTA updates and ads blocked at DNS level
- **34 device profiles** ready to rotate
- Custom wallpaper, loading screen, and branded app icons
- Google Play Store & Play Services disabled (no OTA updates)

> The golden image is too large for git. It's uploaded as a split Release asset — the GUI handles downloading and extracting it automatically.

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

## Building from Source

### Prerequisites
- macOS 12+ (Apple Silicon)
- Python 3.10+
- BlueStacks 5.21+

### Build the .app + .pkg

```bash
cd gui
./build_mac_app.sh          # Full build: venv → .app → .pkg
./build_mac_app.sh --app-only  # Skip .pkg, just build .app
```

Output lands in `gui/dist/`:
- `Luke's Mirage.app` — standalone app bundle
- `mirage.pkg` — installer (includes promo_guard LaunchAgent)

### Create a Golden Image

```bash
cd bluestacks
./create_golden_image.sh --source Tiramisu64_2
```

Compresses the source instance's `data.qcow2` into `golden_image/base.qcow2`.

## What the PKG Installer Does

1. Places `Luke's Mirage.app` in `/Applications`
2. Strips Gatekeeper quarantine flags
3. Deploys `promo_guard.sh` + `load.jpg` to `~/.config/lukesmirage/`
4. Installs a LaunchAgent (`com.lukesmirage.promoguard`) that keeps the custom loading screen in place across all BlueStacks instances

## License

Private — do not redistribute without permission.
