#!/bin/bash
# Build a self-contained Zip2Markdown.app (no Python needed on the target Mac).
#
#   scripts/build_app.sh            -> dist/Zip2Markdown.app
#   scripts/build_app.sh --install  -> also copies it to /Applications
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Zip2Markdown"
DIST="$ROOT/dist"
WORK="${TMPDIR:-/tmp}/zip2md-build"   # keep build junk out of iCloud-synced folders
# Assemble & sign outside ~/Documents: iCloud adds extended attributes that
# codesign rejects ("resource fork, Finder information, or similar detritus").
APP="$WORK/$APP_NAME.app"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/engine/src/zip2md/__init__.py")"

echo "==> 1/5 Swift: app + ocr-helper (release)"
(cd "$ROOT/app" && swift build -c release)
BIN="$(cd "$ROOT/app" && swift build -c release --show-bin-path)"

echo "==> 2/5 Python engine: sync deps"
(cd "$ROOT/engine" && uv sync --quiet)

echo "==> 3/5 Python engine: freeze with PyInstaller"
rm -rf "$WORK" && mkdir -p "$WORK"
PYTHONPATH="$ROOT/engine/src" "$ROOT/engine/.venv/bin/pyinstaller" \
  --noconfirm --clean --log-level WARN \
  --onedir --console --name zip2md \
  --paths "$ROOT/engine/src" \
  --distpath "$WORK/dist" --workpath "$WORK/build" --specpath "$WORK" \
  --collect-all magika \
  --collect-all markitdown \
  --collect-all pymupdf4llm \
  --collect-all pymupdf \
  --collect-data charset_normalizer \
  --copy-metadata markitdown \
  --hidden-import zip2md.cli \
  "$ROOT/engine/pyinstaller_entry.py"

echo "==> 4/5 Assemble $APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN/Zip2MarkdownApp" "$APP/Contents/MacOS/$APP_NAME"
cp "$BIN/ocr-helper" "$APP/Contents/MacOS/ocr-helper"
cp -R "$WORK/dist/zip2md" "$APP/Contents/Resources/engine"

"$ROOT/engine/.venv/bin/python" "$ROOT/scripts/make_icon.py" "$WORK/AppIcon.iconset"
iconutil -c icns "$WORK/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>local.zip2markdown</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeName</key><string>Dokumen</string>
      <key>CFBundleTypeRole</key><string>Viewer</string>
      <key>LSHandlerRank</key><string>Alternate</string>
      <key>LSItemContentTypes</key>
      <array>
        <string>public.zip-archive</string>
        <string>public.folder</string>
        <string>com.adobe.pdf</string>
        <string>public.image</string>
        <string>public.content</string>
        <string>public.data</string>
      </array>
    </dict>
  </array>
</dict>
</plist>
PLIST

echo "==> 5/5 Ad-hoc code signing"
xattr -cr "$APP"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP" && echo "    signature OK"

mkdir -p "$DIST"
rm -rf "$DIST/$APP_NAME.app"
ditto --noextattr --noqtn "$APP" "$DIST/$APP_NAME.app"
du -sh "$DIST/$APP_NAME.app" | awk '{print "    size: "$1}'
echo "Built: $DIST/$APP_NAME.app"

if [[ "${1:-}" == "--install" ]]; then
  rm -rf "/Applications/$APP_NAME.app"
  ditto --noextattr --noqtn "$APP" "/Applications/$APP_NAME.app"
  # Finder info picked up on the way would make the signature fail strict verification
  xattr -cr "/Applications/$APP_NAME.app"
  codesign --verify --deep --strict "/Applications/$APP_NAME.app"
  echo "Installed: /Applications/$APP_NAME.app"
fi
