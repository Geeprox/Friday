#!/bin/zsh
set -euo pipefail
umask 077
FRIDAY_KTV_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
FRIDAY_KTV_BUILD="$FRIDAY_KTV_ROOT/.artifacts/ktv-audio"
FRIDAY_KTV_APP="$FRIDAY_KTV_BUILD/Friday Audio Check.app"
mkdir -p "$FRIDAY_KTV_BUILD/swift-cache" "$FRIDAY_KTV_APP/Contents/MacOS"
# This independent probe never builds, stops or replaces the production Friday app.
if ! mkdir "$FRIDAY_KTV_BUILD/build.lock" 2>/dev/null; then
  print -u2 'ktv_probe_build_busy'
  exit 1
fi
trap 'rmdir "$FRIDAY_KTV_BUILD/build.lock"' EXIT
cat > "$FRIDAY_KTV_APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.friday.ktv.audio-check</string>
<key>CFBundleExecutable</key><string>FridayAudioCheck</string>
<key>CFBundleName</key><string>Friday Audio Check</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>NSMicrophoneUsageDescription</key><string>检查所选麦克风，或在你点击开始后试听最多 30 秒；不保存录音。</string>
<key>NSAudioCaptureUsageDescription</key><string>仅捕获网易云播放声音；试听时不保存，截取歌曲时保存网易云片段以生成伴奏，不保存麦克风录音。</string>
</dict></plist>
PLIST
/bin/cp "$FRIDAY_KTV_ROOT/native/KTVAudio/AudioProbe.swift" "$FRIDAY_KTV_BUILD/main.swift"
/usr/bin/xcrun swiftc -module-cache-path "$FRIDAY_KTV_BUILD/swift-cache" \
  "$FRIDAY_KTV_BUILD/main.swift" "$FRIDAY_KTV_ROOT/native/KTVAudio/NeteaseCapture.swift" \
  "$FRIDAY_KTV_ROOT/native/KTVAudio/NetworkAudio.swift" \
  "$FRIDAY_KTV_ROOT/native/KTVAudio/MicrophoneCapture.swift" \
  -o "$FRIDAY_KTV_APP/Contents/MacOS/FridayAudioCheck"
/usr/bin/codesign --force --sign - "$FRIDAY_KTV_APP"
if [[ "${1:-}" == '--build-only' ]]; then exit 0; fi
if [[ "${1:-}" == '--listen' ]]; then
  FRIDAY_KTV_REPORT="$FRIDAY_KTV_BUILD/listen-$(/usr/bin/uuidgen).json"
  print "$FRIDAY_KTV_REPORT"
  /usr/bin/open -n "$FRIDAY_KTV_APP" --args "$@" --report "$FRIDAY_KTV_REPORT"
elif [[ "${1:-}" == '--meter' ]]; then
  # New bounded runs write to a unique report; never reuse old success as this run's result.
  FRIDAY_KTV_REPORT="$FRIDAY_KTV_BUILD/meter-$(/usr/bin/uuidgen).json"
  print "$FRIDAY_KTV_REPORT"
  /usr/bin/open -n -W "$FRIDAY_KTV_APP" --args "$@" --report "$FRIDAY_KTV_REPORT"
  /bin/cat "$FRIDAY_KTV_REPORT"
else
  "$FRIDAY_KTV_APP/Contents/MacOS/FridayAudioCheck" "$@"
fi
