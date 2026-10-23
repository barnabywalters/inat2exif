#!/usr/bin/env bash
# Package the PyInstaller .app bundle as a compressed disk image.
# Usage: packaging/macos/build-dmg.sh VERSION  (run from the repository root after PyInstaller)
set -euo pipefail

version="${1:?version required}"
arch="$(uname -m)"
root="$(pwd)"
staging="$root/build/dmg"
mkdir -p "$root/dist"
rm -rf "$staging"
mkdir -p "$staging"
cp -R "$root/build/dist/inat2exif.app" "$staging/"
ln -s /Applications "$staging/Applications"
hdiutil create -volname "inat2exif" -srcfolder "$staging" -ov -format UDZO \
  "$root/dist/inat2exif-$version-macos-$arch.dmg"
