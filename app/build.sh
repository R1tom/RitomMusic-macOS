#!/bin/bash
# Builds "Ritom Music.app" with swiftc (no Xcode needed) and bundles the Python tools
# from ~/Music/FLAC (the single source of truth for ytflac / verify / ipod-tool).
set -euo pipefail
cd "$(dirname "$0")"
SRC="${RM_TOOLS_SRC:-$HOME/Music/FLAC}"
APP="build/Ritom Music.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine/ipod-tool"
swiftc -O -parse-as-library -target "$(uname -m)-apple-macos14.0" \
  -o "$APP/Contents/MacOS/RitomMusic" Sources/*.swift
cp Info.plist "$APP/Contents/"
if [ ! -f build/AppIcon.icns ]; then
  rm -rf build/AppIcon.iconset; mkdir -p build/AppIcon.iconset
  swift make-icon.swift build/AppIcon.iconset && iconutil -c icns build/AppIcon.iconset -o build/AppIcon.icns
fi
cp build/AppIcon.icns "$APP/Contents/Resources/"
cp "$SRC/ytflac.py" "$SRC/verify_downloads.py" "$APP/Contents/Resources/engine/"
cp "$SRC/ipod-tool/ipod_sync.py" "$SRC/ipod-tool/ipod_db.py" "$APP/Contents/Resources/engine/ipod-tool/"
codesign --force --deep -s - "$APP"
echo "built $APP"
