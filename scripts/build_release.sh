#!/bin/bash
# CI and local release entry point. Never includes .env or credentials in the app.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
source scripts/arm64_native.sh
arm64_require_native_shell
: "${RELEASE_VERSION:?Set RELEASE_VERSION}"
: "${RELEASE_BUILD:?Set RELEASE_BUILD}"
PYTHON="${RELEASE_PYTHON:-python3}"
"$PYTHON" -c 'import platform; assert platform.machine() == "arm64", "Release Python must run as arm64"'
export PYI_ONEDIR=1 PYI_TARGET_ARCH=arm64
"$PYTHON" -m PyInstaller backend.spec --clean --noconfirm --distpath dist-onedir
arm64_assert_bundle dist-onedir/backend_server
"$PYTHON" scripts/verify_backend_binary.py --binary dist-onedir/backend_server/backend_server
export CI_ADHOC_SIGN=1
bash AIChatApp/scripts/build_dmg.sh
mkdir -p release-output
APP="AIChatApp/build/dmg-staging/AIChatApp.app"
SDK_FRAMEWORK="$(find AIChatApp/build/dmg-derived/SourcePackages/artifacts -type d -name Sparkle.framework | head -1)"
[ -n "$SDK_FRAMEWORK" ] || { echo 'Missing Sparkle SDK headers' >&2; exit 1; }
# Embedded frameworks have headers removed; compile against the resolved SDK,
# then run against the thinned/re-signed framework inside the distribution App.
swiftc scripts/verify_updater.swift -F "$(dirname "$SDK_FRAMEWORK")" -framework Sparkle \
    -Xlinker -rpath -Xlinker "$REPO_DIR/$APP/Contents/Frameworks" \
    -o release-output/verify-updater
release-output/verify-updater "$REPO_DIR/$APP"
cp "AIChatApp/AIChatApp-$RELEASE_VERSION.dmg" release-output/
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "release-output/AIChatApp-$RELEASE_VERSION.zip"

# PR builds stop here; only trusted main-branch releases receive the private key.
if [ "${PUBLISH_UPDATE:-0}" = "1" ]; then
    : "${SPARKLE_PRIVATE_KEY_FILE:?Set SPARKLE_PRIVATE_KEY_FILE}"
    : "${SPARKLE_TOOLS_DIR:?Set SPARKLE_TOOLS_DIR}"
    : "${RELEASE_DOWNLOAD_URL:?Set RELEASE_DOWNLOAD_URL}"
    # generate_appcast inspects the actual bundle version, OS/CPU requirements and
    # SUPublicEDKey, then signs BOTH the archive and feed (SURequireSignedFeed).
    mkdir -p release-output/feed
    cp "release-output/AIChatApp-$RELEASE_VERSION.zip" release-output/feed/
    "$SPARKLE_TOOLS_DIR/generate_appcast" \
        --ed-key-file "$SPARKLE_PRIVATE_KEY_FILE" \
        --download-url-prefix "$RELEASE_DOWNLOAD_URL/" \
        --maximum-deltas 0 --maximum-versions 1 \
        -o release-output/feed/appcast.xml release-output/feed
    "$SPARKLE_TOOLS_DIR/sign_update" --verify --ed-key-file "$SPARKLE_PRIVATE_KEY_FILE" \
        release-output/feed/appcast.xml
    cp release-output/feed/appcast.xml release-output/appcast.xml
    "$PYTHON" scripts/verify_update_feed.py "$APP" release-output/appcast.xml \
        "release-output/AIChatApp-$RELEASE_VERSION.zip"
fi
