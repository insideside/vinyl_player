#!/bin/bash
set -e
echo "Building Vinyl Player for Linux..."

VERSION="1.0.0"
APPNAME="vinyl-player"

# Install Python dependencies
pip3 install pyinstaller httpx mutagen vkpymusic musicbrainzngs 2>/dev/null

# Download cloudflared if not present
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then CF_ARCH="linux-arm64"; else CF_ARCH="linux-amd64"; fi
CF_BIN="build_assets/cloudflared"
if [ ! -f "$CF_BIN" ]; then
    echo "Downloading cloudflared ($CF_ARCH)..."
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-${CF_ARCH}" -o "$CF_BIN"
    chmod +x "$CF_BIN"
fi

# Build binary
python3 -m PyInstaller \
    --name "vinyl-player" \
    --onefile \
    --noconfirm \
    --clean \
    --hidden-import httpx \
    --hidden-import httpx._transports \
    --hidden-import httpx._transports.default \
    --hidden-import httpcore \
    --hidden-import httpcore._async \
    --hidden-import httpcore._sync \
    --hidden-import h11 \
    --hidden-import anyio \
    --hidden-import anyio._backends \
    --hidden-import anyio._backends._asyncio \
    --hidden-import certifi \
    --hidden-import mutagen \
    --hidden-import mutagen.mp3 \
    --hidden-import mutagen.id3 \
    --hidden-import mutagen.id3._frames \
    --hidden-import mutagen.id3._specs \
    --hidden-import mutagen.flac \
    --hidden-import mutagen.mp4 \
    --hidden-import mutagen.oggvorbis \
    --hidden-import mutagen.ogg \
    --hidden-import vkpymusic \
    --hidden-import musicbrainzngs \
    --collect-all vkpymusic \
    --collect-all musicbrainzngs \
    --add-binary "${CF_BIN}:." \
    vinyl_player.py

echo ""
echo "Creating .deb package..."

DEB_DIR="dist/deb"
rm -rf "$DEB_DIR"
mkdir -p "$DEB_DIR/DEBIAN"
mkdir -p "$DEB_DIR/usr/local/bin"
mkdir -p "$DEB_DIR/usr/share/applications"
mkdir -p "$DEB_DIR/usr/share/icons/hicolor/256x256/apps"

cp dist/vinyl-player "$DEB_DIR/usr/local/bin/"
chmod 755 "$DEB_DIR/usr/local/bin/vinyl-player"

if [ -f "build_assets/icon_256.png" ]; then
    cp build_assets/icon_256.png "$DEB_DIR/usr/share/icons/hicolor/256x256/apps/vinyl-player.png"
fi

cat > "$DEB_DIR/DEBIAN/control" << CTRL
Package: vinyl-player
Version: $VERSION
Section: sound
Priority: optional
Architecture: $(dpkg --print-architecture 2>/dev/null || echo amd64)
Depends: libc6
Maintainer: insideside
Description: Vinyl Player - web music player with vinyl visualization
 Web-based music player with vinyl record animation, multi-user support,
 LAN/WAN access, metadata search, and VK Music integration.
 Includes cloudflared for WAN tunnel support.
CTRL

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

dpkg-deb --build "$DEB_DIR" "dist/${APPNAME}_${VERSION}_$(dpkg --print-architecture 2>/dev/null || echo amd64).deb"

echo ""
echo "Done! (includes cloudflared)"
echo "Binary: dist/vinyl-player"
echo "DEB: dist/${APPNAME}_${VERSION}_*.deb"
echo ""
echo "Install: sudo dpkg -i dist/${APPNAME}_${VERSION}_*.deb"
