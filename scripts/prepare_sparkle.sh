#!/bin/bash
# Xcode removes framework headers on embedding, invalidating upstream signatures.
# Thin and sign nested code from the inside out, preserving helper entitlements.
set -euo pipefail
FRAMEWORK="${1:?Supply embedded Sparkle.framework}"
IDENTITY="${2:--}"
VERSION_DIR="$FRAMEWORK/Versions/Current"
[ -d "$VERSION_DIR" ] || { echo 'Missing embedded Sparkle framework' >&2; exit 1; }

# Resolve Current; find must traverse the actual directory, not a symlink.
VERSION_DIR="$(cd "$VERSION_DIR" && pwd -P)"
while IFS= read -r -d '' binary; do
    case "$(/usr/bin/file -b "$binary")" in
        *Mach-O*)
            slices="$(/usr/bin/lipo -archs "$binary")"
            case " $slices " in
                *" arm64 "*) ;;
                *) echo "Sparkle component lacks arm64: $binary" >&2; exit 1 ;;
            esac
            if [ "$slices" != arm64 ]; then
                /usr/bin/lipo "$binary" -thin arm64 -output "$binary.arm64"
                chmod "$(stat -f '%Lp' "$binary")" "$binary.arm64"
                mv "$binary.arm64" "$binary"
            fi
            ;;
    esac
done < <(find "$VERSION_DIR" -type f -print0)

SIGN_OPTIONS=(--force --sign "$IDENTITY" --preserve-metadata=identifier,entitlements)
if [ "$IDENTITY" = '-' ]; then
    SIGN_OPTIONS+=(--timestamp=none)
else
    SIGN_OPTIONS+=(--options runtime --timestamp)
fi
for component in \
    "$VERSION_DIR/Autoupdate" \
    "$VERSION_DIR/XPCServices/Downloader.xpc" \
    "$VERSION_DIR/XPCServices/Installer.xpc" \
    "$VERSION_DIR/Updater.app" \
    "$FRAMEWORK"; do
    /usr/bin/codesign "${SIGN_OPTIONS[@]}" "$component"
done
/usr/bin/codesign --verify --deep --strict "$FRAMEWORK"
