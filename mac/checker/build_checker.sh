#!/usr/bin/env bash
# build_checker.sh — Build the Checker APK without Android Studio
#
# Requirements:
#   - JDK 17+  (brew install openjdk@17)
#   - Android SDK build-tools 34.0.0 + platform android-33
#     (sdkmanager "build-tools;34.0.0" "platforms;android-33")
#
# Usage:
#   bash build_checker.sh
#
# Output:
#   ./Checker.apk (debug-signed, ready for adb install)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Tool paths ──
JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home}"
JAVAC="$JAVA_HOME/bin/javac"
export JAVA_HOME

SDK_ROOT="${ANDROID_SDK_ROOT:-/opt/homebrew/share/android-commandlinetools}"
BUILD_TOOLS="$SDK_ROOT/build-tools/34.0.0"
AAPT2="$BUILD_TOOLS/aapt2"
D8="$BUILD_TOOLS/d8"
ZIPALIGN="$BUILD_TOOLS/zipalign"
APKSIGNER="$BUILD_TOOLS/apksigner"
ANDROID_JAR="$SDK_ROOT/platforms/android-33/android.jar"

# ── Verify tools ──
for tool in "$JAVAC" "$AAPT2" "$D8" "$ZIPALIGN" "$APKSIGNER" "$ANDROID_JAR"; do
    if [ ! -f "$tool" ]; then
        echo "ERROR: Missing tool: $tool" >&2
        exit 1
    fi
done
echo "[1/6] Tools verified"

# ── Clean build dir ──
BUILD_DIR="$SCRIPT_DIR/build"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/compiled" "$BUILD_DIR/classes" "$BUILD_DIR/dex"

# ── Compile resources ──
echo "[2/6] Compiling and linking resources..."
if [ -d "$SCRIPT_DIR/res" ]; then
    "$AAPT2" compile --dir "$SCRIPT_DIR/res" -o "$BUILD_DIR/compiled/resources.zip"
    "$AAPT2" link \
        --manifest "$SCRIPT_DIR/AndroidManifest.xml" \
        -I "$ANDROID_JAR" \
        -o "$BUILD_DIR/app.apk.tmp" \
        --auto-add-overlay \
        "$BUILD_DIR/compiled/resources.zip"
else
    "$AAPT2" link \
        --manifest "$SCRIPT_DIR/AndroidManifest.xml" \
        -I "$ANDROID_JAR" \
        -o "$BUILD_DIR/app.apk.tmp" \
        --auto-add-overlay \
        -v 2>/dev/null || "$AAPT2" link \
        --manifest "$SCRIPT_DIR/AndroidManifest.xml" \
        -I "$ANDROID_JAR" \
        -o "$BUILD_DIR/app.apk.tmp"
fi

# ── Compile Java ──
echo "[3/6] Compiling Java..."
"$JAVAC" \
    -source 11 -target 11 \
    -classpath "$ANDROID_JAR" \
    -d "$BUILD_DIR/classes" \
    "$SCRIPT_DIR/src/com/jorkspoofer/checker/CheckerActivity.java"

# ── Dex ──
echo "[4/6] Dexing..."
# Include all .class files (inner classes like ProfileBridge compile to separate files)
"$D8" \
    --output "$BUILD_DIR/dex" \
    --lib "$ANDROID_JAR" \
    $(find "$BUILD_DIR/classes" -name '*.class')

# ── Merge DEX into APK ──
echo "[5/6] Packaging APK..."
# Copy the linked APK, then add the dex file
cp "$BUILD_DIR/app.apk.tmp" "$BUILD_DIR/Checker-unsigned.apk"

# Use zip to add classes.dex into the APK (APK is just a ZIP)
cd "$BUILD_DIR/dex"
zip -j "$BUILD_DIR/Checker-unsigned.apk" classes.dex
cd "$SCRIPT_DIR"

# Zipalign
"$ZIPALIGN" -f 4 "$BUILD_DIR/Checker-unsigned.apk" "$BUILD_DIR/Checker-aligned.apk"

# ── Sign ──
echo "[6/6] Signing..."
# Generate a debug keystore if it doesn't exist
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
    --out "$SCRIPT_DIR/Checker.apk" \
    "$BUILD_DIR/Checker-aligned.apk"

# ── Verify ──
"$APKSIGNER" verify "$SCRIPT_DIR/Checker.apk" 2>/dev/null && echo "" || echo "WARNING: APK verification failed"

SIZE=$(ls -lh "$SCRIPT_DIR/Checker.apk" | awk '{print $5}')
echo "Done! Checker.apk ($SIZE)"
echo ""
echo "Install with:"
echo "  /Applications/BlueStacks.app/Contents/MacOS/hd-adb install -r Checker.apk"
echo ""
echo "Launch with:"
echo "  /Applications/BlueStacks.app/Contents/MacOS/hd-adb shell am start -n com.jorkspoofer.checker/.CheckerActivity"
