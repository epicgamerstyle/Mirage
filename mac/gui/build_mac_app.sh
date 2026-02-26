#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Luke's Mirage — macOS App Builder
#
# Creates a standalone .app bundle + .pkg installer for distribution.
# The .pkg installer handles Gatekeeper automatically — users just double-click.
# No Python installation needed on the end-user's machine.
#
# Usage:
#   ./build_mac_app.sh              # Full build (venv + PyInstaller + PKG)
#   ./build_mac_app.sh --app-only   # Build .app only, skip installer
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="2.2.0"
APP_NAME="Luke's Mirage"
APP_IDENTIFIER="com.lukesmirage.manager"
PKG_NAME="mirage.pkg"
BUILD_VENV="$SCRIPT_DIR/.build_venv"
PKG_STAGING="$SCRIPT_DIR/build/pkg_staging"
SKIP_PKG=0

# Parse args
for arg in "$@"; do
    case "$arg" in
        --app-only) SKIP_PKG=1 ;;
    esac
done

echo "═══════════════════════════════════════════════════════════════"
echo "  Building $APP_NAME v$VERSION"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Step 1: Create build virtualenv ──────────────────────────────────────────
echo "→ Setting up build environment..."
if [ ! -d "$BUILD_VENV" ]; then
    python3 -m venv "$BUILD_VENV"
fi
# shellcheck disable=SC1091
source "$BUILD_VENV/bin/activate"

echo "→ Installing build dependencies..."
pip install -q --upgrade pip
pip install -q \
    pyinstaller \
    pywebview \
    pyobjc-framework-WebKit \
    pyobjc-framework-Quartz \
    pyobjc-framework-Security \
    pyobjc-framework-UniformTypeIdentifiers \
    bottle \
    proxy_tools \
    websocket-client \
    typing_extensions

echo "  ✓ Build environment ready"
echo ""

# ── Step 2: Run PyInstaller ──────────────────────────────────────────────────
echo "→ Building app bundle with PyInstaller..."
pyinstaller --clean --noconfirm LukesMirage.spec

if [ ! -d "dist/$APP_NAME.app" ]; then
    echo "✗ Build failed — .app not found in dist/"
    exit 1
fi
echo "  ✓ App bundle created"
echo ""

# ── Step 3: Fix permissions inside the bundle ────────────────────────────────
echo "→ Fixing file permissions..."
find "dist/$APP_NAME.app" -name "*.sh" -exec chmod +x {} \; 2>/dev/null || true
find "dist/$APP_NAME.app" -name "*.command" -exec chmod +x {} \; 2>/dev/null || true
chmod +x "dist/$APP_NAME.app/Contents/MacOS/LukesMirage" 2>/dev/null || true
find "dist/$APP_NAME.app" -name "hev-socks5-tunnel" -exec chmod +x {} \; 2>/dev/null || true
echo "  ✓ Permissions set"
echo ""

# ── Step 4: Strip quarantine from the .app ───────────────────────────────────
echo "→ Removing quarantine attributes..."
xattr -cr "dist/$APP_NAME.app" 2>/dev/null || true
echo "  ✓ Quarantine cleared"
echo ""

APP_SIZE=$(du -sh "dist/$APP_NAME.app" | cut -f1)
echo "  App size: $APP_SIZE"
echo ""

# ── Step 5: Build .pkg installer ─────────────────────────────────────────────
if [ "$SKIP_PKG" = "0" ]; then
    echo "→ Building .pkg installer..."

    # Clean staging area
    rm -rf "$PKG_STAGING"
    mkdir -p "$PKG_STAGING/payload/Applications"
    mkdir -p "$PKG_STAGING/scripts"

    # Copy app into payload
    cp -R "dist/$APP_NAME.app" "$PKG_STAGING/payload/Applications/"

    # Copy promo_guard assets into staging for the postinstall to deploy
    mkdir -p "$PKG_STAGING/scripts/assets"
    cp "$SCRIPT_DIR/../bluestacks/lib/promo_guard.sh" "$PKG_STAGING/scripts/assets/"
    cp "$SCRIPT_DIR/../bluestacks/load.jpg" "$PKG_STAGING/scripts/assets/"

    # Create postinstall script — strips quarantine + installs promo_guard
    cat > "$PKG_STAGING/scripts/postinstall" << 'POSTINSTALL'
#!/bin/bash
# ── Gatekeeper / permissions ──
xattr -cr "/Applications/Luke's Mirage.app" 2>/dev/null || true
find "/Applications/Luke's Mirage.app" -name "*.sh" -exec chmod +x {} \; 2>/dev/null || true
find "/Applications/Luke's Mirage.app" -name "hev-socks5-tunnel" -exec chmod +x {} \; 2>/dev/null || true
chmod +x "/Applications/Luke's Mirage.app/Contents/MacOS/LukesMirage" 2>/dev/null || true

# ── Install promo_guard (loading screen protector) ──
# Determine the real user (postinstall runs as root)
REAL_USER=$(stat -f%Su /dev/console 2>/dev/null || echo "")
if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
    REAL_USER=$(defaults read /Library/Preferences/com.apple.loginwindow.plist lastUserName 2>/dev/null || echo "")
fi
if [ -z "$REAL_USER" ]; then
    exit 0  # Can't determine user, skip promo_guard setup
fi

REAL_HOME=$(eval echo "~$REAL_USER")
MIRAGE_DIR="$REAL_HOME/.config/lukesmirage"
LAUNCH_AGENTS="$REAL_HOME/Library/LaunchAgents"
SCRIPT_ASSETS="$(dirname "$0")/assets"

# Create config directory and deploy assets
mkdir -p "$MIRAGE_DIR"
cp "$SCRIPT_ASSETS/promo_guard.sh" "$MIRAGE_DIR/promo_guard.sh"
cp "$SCRIPT_ASSETS/load.jpg" "$MIRAGE_DIR/load.jpg"
chmod +x "$MIRAGE_DIR/promo_guard.sh"
chown -R "$REAL_USER" "$MIRAGE_DIR"

# Create LaunchAgent plist
mkdir -p "$LAUNCH_AGENTS"
PLIST="$LAUNCH_AGENTS/com.lukesmirage.promoguard.plist"
cat > "$PLIST" << 'PLISTEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lukesmirage.promoguard</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>MIRAGE_DIR_PLACEHOLDER/promo_guard.sh</string>
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
</plist>
PLISTEOF

# Replace placeholder with actual path
sed -i '' "s|MIRAGE_DIR_PLACEHOLDER|$MIRAGE_DIR|g" "$PLIST"
chown "$REAL_USER" "$PLIST"

# Load the LaunchAgent (as the real user, not root)
su "$REAL_USER" -c "launchctl unload '$PLIST' 2>/dev/null; launchctl load '$PLIST'" 2>/dev/null || true

exit 0
POSTINSTALL
    chmod +x "$PKG_STAGING/scripts/postinstall"

    # Build the component pkg
    pkgbuild \
        --root "$PKG_STAGING/payload" \
        --scripts "$PKG_STAGING/scripts" \
        --identifier "$APP_IDENTIFIER" \
        --version "$VERSION" \
        --install-location "/" \
        "dist/$PKG_NAME"

    PKG_SIZE=$(du -sh "dist/$PKG_NAME" | cut -f1)
    echo "  ✓ Installer created ($PKG_SIZE)"
    echo ""
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  BUILD COMPLETE"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  App:  dist/$APP_NAME.app  ($APP_SIZE)"
if [ "$SKIP_PKG" = "0" ]; then
    echo "  PKG:  dist/$PKG_NAME  ($PKG_SIZE)"
fi
echo ""
echo "  Distribute the .pkg file — users double-click to install."
echo "  The app will be placed in /Applications with no Gatekeeper issues."
echo ""
