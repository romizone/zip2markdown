#!/bin/bash
# Pack dist/Zip2Markdown.app into a drag-to-install disk image.
#
#   scripts/build_app.sh && scripts/make_dmg.sh   -> dist/Zip2Markdown-<versi>.dmg
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Zip2Markdown"
DIST="$ROOT/dist"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/engine/src/zip2md/__init__.py")"
APP="$DIST/$APP_NAME.app"
DMG="$DIST/$APP_NAME-$VERSION.dmg"

[[ -d "$APP" ]] || { echo "Belum ada $APP — jalankan scripts/build_app.sh dulu." >&2; exit 1; }
BUILT="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"
[[ "$BUILT" == "$VERSION" ]] || { echo "App di dist/ versi $BUILT, sumber versi $VERSION — build ulang dulu." >&2; exit 1; }

# Stage outside ~/Documents: iCloud adds extended attributes that break the signature.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/zip2md-dmg.XXXXXX")"
trap 'rm -rf "${STAGE:?}"' EXIT

echo "==> 1/3 Menyiapkan isi disk image"
ditto --noextattr --noqtn "$APP" "$STAGE/$APP_NAME.app"
xattr -cr "$STAGE/$APP_NAME.app"
codesign --verify --deep --strict "$STAGE/$APP_NAME.app"
ln -s /Applications "$STAGE/Applications"       # drag the app onto this to install

echo "==> 2/3 Membuat $APP_NAME-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -fs HFS+ -format ULMO -ov -quiet "$DMG"

echo "==> 3/3 Memeriksa"
hdiutil verify -quiet "$DMG"
du -h "$DMG" | awk '{print "    ukuran: "$1}'
shasum -a 256 "$DMG" | awk '{print "    sha256: "$1}'
echo "Selesai: $DMG"
