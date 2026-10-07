#!/bin/zsh
set -euo pipefail
ROOT=${0:A:h:h}
OUT=${1:-$ROOT/build/LampVM Harness.app}
mkdir -p "$OUT/Contents/MacOS"
xcrun swiftc -O -target arm64-apple-macos14 "$ROOT/tests/Harness.swift" -o "$OUT/Contents/MacOS/Harness"
cat > "$OUT/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>Harness</string>
<key>CFBundleIdentifier</key><string>dev.lampvm.harness</string>
<key>CFBundleName</key><string>LampVM Harness</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$OUT"
print -r -- "$OUT"
