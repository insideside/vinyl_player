#!/bin/bash
set -e
echo "Building Vinyl Player for Linux..."

VERSION="1.0.0"
APPNAME="vinyl-player"

# Install dependencies
pip3 install pyinstaller httpx mutagen 2>/dev/null

# Build binary
pyinstaller \
    --name "vinyl-player" \
    --onefile \
    --noconfirm \
    --clean \
    --hidden-import httpx \
    --hidden-import mutagen \
    --hidden-import mutagen.mp3 \
    --hidden-import mutagen.id3 \
    --hidden-import mutagen.flac \
    --hidden-import mutagen.mp4 \
    --hidden-import mutagen.oggvorbis \
    vinyl_player.py

echo ""
echo "Creating .deb package..."

# Build .deb
DEB_DIR="dist/deb"
rm -rf "$DEB_DIR"
mkdir -p "$DEB_DIR/DEBIAN"
mkdir -p "$DEB_DIR/usr/local/bin"
mkdir -p "$DEB_DIR/usr/share/applications"
mkdir -p "$DEB_DIR/usr/share/icons/hicolor/256x256/apps"

cp dist/vinyl-player "$DEB_DIR/usr/local/bin/"
chmod 755 "$DEB_DIR/usr/local/bin/vinyl-player"

# Copy icon if exists
if [ -f "build_assets/icon_256.png" ]; then
    cp build_assets/icon_256.png "$DEB_DIR/usr/share/icons/hicolor/256x256/apps/vinyl-player.png"
fi

# Control file
cat > "$DEB_DIR/DEBIAN/control" << CTRL
Package: vinyl-player
Version: $VERSION
Section: sound
Priority: optional
Architecture: amd64
Depends: libc6
Maintainer: insideside
Description: Vinyl Player - web music player with vinyl visualization
 Web-based music player with vinyl record animation, multi-user support,
 LAN/WAN access, metadata search, and VK Music integration.
CTRL

# Desktop entry
cat > "$DEB_DIR/usr/share/applications/vinyl-player.desktop" << DESKTOP
[Desktop Entry]
Name=Vinyl Player
Comment=Web music player with vinyl visualization
Exec=/usr/local/bin/vinyl-player
Icon=vinyl-player
Type=Application
Categories=Audio;Music;Player;
Terminal=false
DESKTOP

dpkg-deb --build "$DEB_DIR" "dist/${APPNAME}_${VERSION}_amd64.deb"

echo ""
echo "Done!"
echo "Binary: dist/vinyl-player"
echo "DEB: dist/${APPNAME}_${VERSION}_amd64.deb"
echo ""
echo "Install: sudo dpkg -i dist/${APPNAME}_${VERSION}_amd64.deb"
