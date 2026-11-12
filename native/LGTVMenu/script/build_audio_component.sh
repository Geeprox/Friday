#!/usr/bin/env bash
set -euo pipefail
MODE="${1:---package}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$ROOT_DIR/../.." && pwd)"
OUTPUT_DIR="$REPO_DIR/.artifacts/lg-tv-menu/audio"
mkdir -p "$OUTPUT_DIR"
INCLUDES=(-I "$ROOT_DIR/Sources/LGTVDSP/include")
FRAMEWORKS=(-framework CoreAudio -framework CoreFoundation)
if [[ "$MODE" == "--test" ]]; then
    clang -std=c11 -fblocks -mmacosx-version-min=14.0 "${INCLUDES[@]}" "${FRAMEWORKS[@]}" \
        "$ROOT_DIR/AudioDriver/DriverTests.c" "$ROOT_DIR/Sources/LGTVDSP/LGTVDSP.c" -o "$OUTPUT_DIR/DriverTests"
    "$OUTPUT_DIR/DriverTests"
    exit 0
fi
[[ "$MODE" == "--package" ]] || { echo "usage: $0 [--package|--test]" >&2; exit 2; }
SIGNING_IDENTITY="${LGTV_SIGNING_IDENTITY:-}"
if [[ -z "$SIGNING_IDENTITY" && -f "$REPO_DIR/.artifacts/lg-tv-menu/signing-identity" ]]; then
    IFS= read -r SIGNING_IDENTITY < "$REPO_DIR/.artifacts/lg-tv-menu/signing-identity"
fi
SIGNING_IDENTITY="${SIGNING_IDENTITY:--}"
DRIVER="$OUTPUT_DIR/payload/LGTVAudio.driver"
rm -rf "$OUTPUT_DIR/payload" "$OUTPUT_DIR/install-scripts" "$OUTPUT_DIR/uninstall-scripts"
mkdir -p "$DRIVER/Contents/MacOS" "$OUTPUT_DIR/install-scripts" "$OUTPUT_DIR/uninstall-scripts"
cp "$ROOT_DIR/AudioDriver/Info.plist" "$DRIVER/Contents/Info.plist"
clang -std=c11 -fblocks -O2 -arch arm64 -arch x86_64 -fvisibility=hidden -mmacosx-version-min=14.0 \
    "${INCLUDES[@]}" -bundle "${FRAMEWORKS[@]}" "$ROOT_DIR/AudioDriver/LGTVAudio.c" \
    "$ROOT_DIR/Sources/LGTVDSP/LGTVDSP.c" -o "$DRIVER/Contents/MacOS/LGTVAudio"
clang -std=c11 -O2 -arch arm64 -arch x86_64 -mmacosx-version-min=14.0 "${FRAMEWORKS[@]}" \
    "$ROOT_DIR/AudioDriver/LGTVAudioRecovery.c" -o "$OUTPUT_DIR/LGTVAudioRecovery"
codesign --force --sign "$SIGNING_IDENTITY" "$DRIVER"
codesign --force --sign "$SIGNING_IDENTITY" "$OUTPUT_DIR/LGTVAudioRecovery"
codesign --verify --strict "$DRIVER"
nm -gU "$DRIVER/Contents/MacOS/LGTVAudio" | /usr/bin/grep -q '_LGTVAudio_Create'
cp "$OUTPUT_DIR/LGTVAudioRecovery" "$OUTPUT_DIR/install-scripts/Recovery"
cp "$OUTPUT_DIR/LGTVAudioRecovery" "$OUTPUT_DIR/uninstall-scripts/Recovery"
cp "$ROOT_DIR/AudioDriver/installer/preinstall" "$OUTPUT_DIR/install-scripts/preinstall"
cp "$ROOT_DIR/AudioDriver/installer/postinstall" "$OUTPUT_DIR/install-scripts/postinstall"
cp "$ROOT_DIR/AudioDriver/installer/uninstall" "$OUTPUT_DIR/uninstall-scripts/postinstall"
chmod 755 "$OUTPUT_DIR"/{install-scripts,uninstall-scripts}/*
pkgbuild --analyze --root "$OUTPUT_DIR/payload" "$OUTPUT_DIR/components.plist"
/usr/libexec/PlistBuddy -c 'Add :0:BundleIsRelocatable bool false' "$OUTPUT_DIR/components.plist"
/usr/libexec/PlistBuddy -c 'Set :0:BundleIsVersionChecked false' "$OUTPUT_DIR/components.plist"
pkgbuild --root "$OUTPUT_DIR/payload" --install-location /Library/Audio/Plug-Ins/HAL \
    --component-plist "$OUTPUT_DIR/components.plist" \
    --identifier local.friday.LGTVAudio --version 0.1.1 --scripts "$OUTPUT_DIR/install-scripts" \
    "$OUTPUT_DIR/LGTVVolume.pkg"
pkgbuild --nopayload --identifier local.friday.LGTVAudio.uninstall --version 0.1.0 \
    --scripts "$OUTPUT_DIR/uninstall-scripts" "$OUTPUT_DIR/Uninstall-LGTVVolume.pkg"
echo "$OUTPUT_DIR/LGTVVolume.pkg"
