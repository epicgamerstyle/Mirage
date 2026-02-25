#!/usr/bin/env bash
# build_injector.sh — Build the Mirage WebView Injector LSPosed module APK
#
# Requirements:
#   - JDK 17+  (brew install openjdk@17)
#   - Android SDK build-tools 34.0.0 + platform android-33
#     (sdkmanager "build-tools;34.0.0" "platforms;android-33")
#
# Usage:
#   bash build_injector.sh
#
# Output:
#   ./WebInject.apk (debug-signed LSPosed module, ready for adb install)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Tool paths ──
JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home}"
JAVAC="$JAVA_HOME/bin/javac"
JAR="$JAVA_HOME/bin/jar"
export JAVA_HOME

SDK_ROOT="${ANDROID_SDK_ROOT:-/opt/homebrew/share/android-commandlinetools}"
BUILD_TOOLS="$SDK_ROOT/build-tools/34.0.0"
AAPT2="$BUILD_TOOLS/aapt2"
D8="$BUILD_TOOLS/d8"
ZIPALIGN="$BUILD_TOOLS/zipalign"
APKSIGNER="$BUILD_TOOLS/apksigner"
ANDROID_JAR="$SDK_ROOT/platforms/android-33/android.jar"

# ── Verify tools ──
for tool in "$JAVAC" "$JAR" "$AAPT2" "$D8" "$ZIPALIGN" "$APKSIGNER" "$ANDROID_JAR"; do
    if [ ! -f "$tool" ]; then
        echo "ERROR: Missing tool: $tool" >&2
        exit 1
    fi
done
echo "[1/8] Tools verified"

# ── Clean build dir ──
BUILD_DIR="$SCRIPT_DIR/build"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/stub-classes" "$BUILD_DIR/compiled" "$BUILD_DIR/classes" "$BUILD_DIR/dex"

# ── Step 2: Compile Xposed API stubs ──
echo "[2/8] Compiling Xposed API stubs..."
find "$SCRIPT_DIR/xposed-stub" -name '*.java' > "$BUILD_DIR/stub-sources.txt"
"$JAVAC" \
    -source 11 -target 11 \
    -classpath "$ANDROID_JAR" \
    -d "$BUILD_DIR/stub-classes" \
    @"$BUILD_DIR/stub-sources.txt"

# Package stubs into a jar
"$JAR" cf "$BUILD_DIR/xposed-api-stub.jar" -C "$BUILD_DIR/stub-classes" .
echo "       Stub jar created"

# ── Step 3: Compile resources ──
echo "[3/8] Compiling and linking resources..."
"$AAPT2" compile --dir "$SCRIPT_DIR/res" -o "$BUILD_DIR/compiled/resources.zip"
"$AAPT2" link \
    --manifest "$SCRIPT_DIR/AndroidManifest.xml" \
    -I "$ANDROID_JAR" \
    -o "$BUILD_DIR/app.apk.tmp" \
    --auto-add-overlay \
    "$BUILD_DIR/compiled/resources.zip"

# ── Step 4: Compile Java source ──
echo "[4/8] Compiling Java source..."
"$JAVAC" \
    -source 11 -target 11 \
    -classpath "$ANDROID_JAR:$BUILD_DIR/xposed-api-stub.jar" \
    -d "$BUILD_DIR/classes" \
    "$SCRIPT_DIR/src/com/lukesmirage/webinject/WebViewInjector.java"

# ── Step 5: DEX ──
echo "[5/8] Dexing..."
"$D8" \
    --output "$BUILD_DIR/dex" \
    --lib "$ANDROID_JAR" \
    $(find "$BUILD_DIR/classes" -name '*.class')
# NOTE: We do NOT dex the stubs — LSPosed provides them at runtime.

# ── Step 6: Package APK ──
echo "[6/8] Packaging APK..."
cp "$BUILD_DIR/app.apk.tmp" "$BUILD_DIR/WebInject-unsigned.apk"

# Add classes.dex
cd "$BUILD_DIR/dex"
zip -j "$BUILD_DIR/WebInject-unsigned.apk" classes.dex
cd "$SCRIPT_DIR"

# Add xposed_init asset
mkdir -p "$BUILD_DIR/assets-zip/assets"
cp "$SCRIPT_DIR/assets/xposed_init" "$BUILD_DIR/assets-zip/assets/"
cd "$BUILD_DIR/assets-zip"
zip -r "$BUILD_DIR/WebInject-unsigned.apk" assets/
cd "$SCRIPT_DIR"

# Zipalign
"$ZIPALIGN" -f 4 "$BUILD_DIR/WebInject-unsigned.apk" "$BUILD_DIR/WebInject-aligned.apk"

# ── Step 7: Sign ──
echo "[7/8] Signing..."
KEYSTORE="$SCRIPT_DIR/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
    "$JAVA_HOME/bin/keytool" -genkeypair \
        -keystore "$KEYSTORE" \
        -storepass android \
        -keypass android \
        -alias androiddebugkey \
        -keyalg RSA \
        -keysize 2048 \
        -validity 10000 \
        -dname "CN=Debug,OU=Debug,O=Debug,L=Debug,ST=Debug,C=US" \
        2>/dev/null
fi

"$APKSIGNER" sign \
    --ks "$KEYSTORE" \
    --ks-pass pass:android \
    --ks-key-alias androiddebugkey \
    --key-pass pass:android \
    --out "$SCRIPT_DIR/WebInject.apk" \
    "$BUILD_DIR/WebInject-aligned.apk"

# ── Step 8: Verify ──
echo "[8/8] Verifying..."
"$APKSIGNER" verify "$SCRIPT_DIR/WebInject.apk" 2>/dev/null && echo "" || echo "WARNING: APK verification failed"

SIZE=$(ls -lh "$SCRIPT_DIR/WebInject.apk" | awk '{print $5}')
echo "Done! WebInject.apk ($SIZE)"
echo ""
echo "Install with:"
echo "  /Applications/BlueStacks.app/Contents/MacOS/hd-adb install -r WebInject.apk"
echo ""
echo "Then enable in LSPosed Manager:"
echo "  1. Open LSPosed → Modules → Mirage WebInject"
echo "  2. Enable the module"
echo "  3. Set scope: com.android.chrome, com.jagex.oldscape.android"
echo "  4. Reboot (or force-stop the target apps)"
