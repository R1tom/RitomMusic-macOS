#!/bin/bash
# Builds "Ritom Music.app" with swiftc (no Xcode needed) and bundles the Python tools.
#   ./build.sh            build for this Mac            → build/Ritom Music.app
#   ./build.sh release    universal (Intel + Apple Silicon) build + zip → build/RitomMusic-<version>-macOS.zip
#   ./build.sh install    build, then copy into /Applications
# Python tools: $RM_TOOLS_SRC if set, else ~/Music/FLAC when it holds them (the author's working copy,
# which is then mirrored into ../engine), else the copy in ../engine.
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:-}"
ENGINE="$(cd .. && pwd)/engine"
if [ -n "${RM_TOOLS_SRC:-}" ]; then SRC="$RM_TOOLS_SRC"
elif [ -f "$HOME/Music/FLAC/ytflac.py" ]; then SRC="$HOME/Music/FLAC"
else SRC="$ENGINE"; fi
if [ "$SRC" != "$ENGINE" ]; then                       # keep the repo's engine copy current
  mkdir -p "$ENGINE/ipod-tool"
  cp "$SRC/ytflac.py" "$SRC/verify_downloads.py" "$ENGINE/"
  cp "$SRC/ipod-tool/ipod_sync.py" "$SRC/ipod-tool/ipod_db.py" "$ENGINE/ipod-tool/"
fi
VERSION=$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' Info.plist)
APP="build/Ritom Music.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine/ipod-tool"
compile() { swiftc -O -parse-as-library -target "$1-apple-macos14.0" -o "$2" Sources/*.swift; }
if [ "$MODE" = "release" ]; then
  compile x86_64 build/RitomMusic-x86_64
  compile arm64 build/RitomMusic-arm64
  lipo -create build/RitomMusic-x86_64 build/RitomMusic-arm64 -output "$APP/Contents/MacOS/RitomMusic"
  rm -f build/RitomMusic-x86_64 build/RitomMusic-arm64
else
  compile "$(uname -m)" "$APP/Contents/MacOS/RitomMusic"
fi
cp Info.plist "$APP/Contents/"
if [ ! -f build/AppIcon.icns ]; then
  rm -rf build/AppIcon.iconset; mkdir -p build/AppIcon.iconset
  swift make-icon.swift build/AppIcon.iconset && iconutil -c icns build/AppIcon.iconset -o build/AppIcon.icns
fi
cp build/AppIcon.icns "$APP/Contents/Resources/"
cp "$SRC/ytflac.py" "$SRC/verify_downloads.py" "$APP/Contents/Resources/engine/"
cp "$SRC/ipod-tool/ipod_sync.py" "$SRC/ipod-tool/ipod_db.py" "$APP/Contents/Resources/engine/ipod-tool/"
codesign --force --deep -s - "$APP"
echo "built $APP ($VERSION, tools from $SRC)"
case "$MODE" in
  release)
    ZIP="build/RitomMusic-$VERSION-macOS.zip"
    rm -f "$ZIP"; ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
    shasum -a 256 "$ZIP"; echo "packaged $ZIP" ;;
  install)
    osascript -e 'quit app "Ritom Music"' 2>/dev/null || true; sleep 1
    rm -rf "/Applications/Ritom Music.app"; cp -R "$APP" /Applications/
    echo "installed /Applications/Ritom Music.app" ;;
esac
