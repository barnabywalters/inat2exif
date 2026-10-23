#!/usr/bin/env bash
# Package the PyInstaller one-folder build as a tarball and an AppImage.
# Usage: packaging/linux/build-appimage.sh VERSION  (run from the repository root after PyInstaller)
set -euo pipefail

version="${1:?version required}"
arch="$(uname -m)"
root="$(pwd)"
bundle="$root/build/dist/inat2exif"
out="$root/dist"
appdir="$root/build/AppDir"
mkdir -p "$out"

tar -C "$root/build/dist" -czf "$out/inat2exif-$version-linux-$arch.tar.gz" inat2exif

rm -rf "$appdir"
mkdir -p "$appdir/usr/lib" "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/512x512/apps"
cp -a "$bundle" "$appdir/usr/lib/inat2exif"
cp packaging/linux/inat2exif.desktop "$appdir/inat2exif.desktop"
cp packaging/linux/inat2exif.desktop "$appdir/usr/share/applications/inat2exif.desktop"
cp packaging/icons/inat2exif.png "$appdir/inat2exif.png"
cp packaging/icons/inat2exif.png "$appdir/usr/share/icons/hicolor/512x512/apps/inat2exif.png"
cat > "$appdir/AppRun" <<'EOF'
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
exec "$here/usr/lib/inat2exif/inat2exif" "$@"
EOF
chmod +x "$appdir/AppRun"

tool="${APPIMAGETOOL:-$root/build/appimagetool-$arch.AppImage}"
if [ ! -x "$tool" ]; then
  curl -fsSL -o "$tool" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$arch.AppImage"
  chmod +x "$tool"
fi
ARCH="$arch" APPIMAGE_EXTRACT_AND_RUN=1 "$tool" --no-appstream "$appdir" "$out/inat2exif-$version-linux-$arch.AppImage"
