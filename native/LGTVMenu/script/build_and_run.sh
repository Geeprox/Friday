#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-run}"
APP_NAME="LGTVMenu"
BUNDLE_ID="local.friday.LGTVMenu"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$ROOT_DIR/../.." && pwd)"
OUTPUT_DIR="$REPO_DIR/.artifacts/lg-tv-menu"
APP_BUNDLE="$OUTPUT_DIR/LG TV.app"
APP_BINARY="$APP_BUNDLE/Contents/MacOS/$APP_NAME"
SIGNING_IDENTITY="${LGTV_SIGNING_IDENTITY:-}"
if [[ -z "$SIGNING_IDENTITY" && -f "$OUTPUT_DIR/signing-identity" ]]; then
    IFS= read -r SIGNING_IDENTITY < "$OUTPUT_DIR/signing-identity"
fi
SIGNING_IDENTITY="${SIGNING_IDENTITY:--}"

case "$MODE" in
    run|--verify|--logs|--telemetry|--debug|--build-only|--test) ;;
    *) echo "usage: $0 [run|--verify|--logs|--telemetry|--debug|--build-only|--test]" >&2; exit 2 ;;
esac

mkdir -p "$OUTPUT_DIR/module-cache" "$OUTPUT_DIR/cache" "$OUTPUT_DIR/config" "$OUTPUT_DIR/security"
export CLANG_MODULE_CACHE_PATH="$OUTPUT_DIR/module-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$OUTPUT_DIR/module-cache"
SWIFT_OPTIONS=(--disable-sandbox --package-path "$ROOT_DIR" --scratch-path "$OUTPUT_DIR/build"
    --cache-path "$OUTPUT_DIR/cache" --config-path "$OUTPUT_DIR/config" --security-path "$OUTPUT_DIR/security")

if [[ "$MODE" == "--test" ]]; then
    bash "$ROOT_DIR/script/build_audio_component.sh" --test
    swift test "${SWIFT_OPTIONS[@]}"
    exit 0
fi

if [[ "$MODE" != "--build-only" ]]; then
    pkill -x "$APP_NAME" >/dev/null 2>&1 || true
fi
swift build "${SWIFT_OPTIONS[@]}" -c release
bash "$ROOT_DIR/script/build_audio_component.sh" --package
BUILD_BINARY="$(swift build "${SWIFT_OPTIONS[@]}" -c release --show-bin-path)/$APP_NAME"

# Remove only this script's generated bundle in the ignored output directory.
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Helpers" "$APP_BUNDLE/Contents/Resources"
cp "$BUILD_BINARY" "$APP_BINARY"
cp "$OUTPUT_DIR/audio/LGTVAudioRecovery" "$APP_BUNDLE/Contents/Helpers/"
cp "$OUTPUT_DIR/audio/LGTVVolume.pkg" "$OUTPUT_DIR/audio/Uninstall-LGTVVolume.pkg" "$APP_BUNDLE/Contents/Resources/"
chmod +x "$APP_BINARY"
cat > "$APP_BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>$APP_NAME</string>
<key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
<key>CFBundleName</key><string>LG TV</string>
<key>CFBundleDisplayName</key><string>LG TV</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.2.0</string>
<key>CFBundleVersion</key><string>14</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>NSPrincipalClass</key><string>NSApplication</string>
<key>NSLocalNetworkUsageDescription</key><string>查找并控制你选择的 LG 电视，在 Mac 唤醒后发送局域网唤醒包。</string>
<key>NSMicrophoneUsageDescription</key><string>仅从 LG TV 虚拟音频设备读取系统播放的声音并转送到 HDMI；不打开物理麦克风，也不保存或上传音频。</string>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign --force --sign "$SIGNING_IDENTITY" "$APP_BUNDLE"
codesign --verify --strict "$APP_BUNDLE"

case "$MODE" in
    --build-only) echo "$APP_BUNDLE" ;;
    --debug) lldb -- "$APP_BINARY" ;;
    --logs|--telemetry)
        /usr/bin/open -n "$APP_BUNDLE"
        /usr/bin/log stream --info --style compact --predicate 'process == "LGTVMenu"'
        ;;
    --verify)
        /usr/bin/open -n "$APP_BUNDLE"
        sleep 1
        pgrep -x "$APP_NAME" >/dev/null
        echo "LG TV launched; device power state has not been tested."
        ;;
    run) /usr/bin/open -n "$APP_BUNDLE" ;;
esac
