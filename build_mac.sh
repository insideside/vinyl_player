#!/bin/bash
set -e
echo "Building Vinyl Player for macOS..."

# Install Python dependencies
pip3 install pyinstaller httpx mutagen vkpymusic musicbrainzngs Pillow libtorrent 2>/dev/null

# Jackett installer for this platform (search over trackers, installed from the app on demand)
JK_ARCH=$(python3 scripts/fetch_jackett.py --current)

# Download cloudflared if not present
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then CF_ARCH="darwin-arm64"; else CF_ARCH="darwin-amd64"; fi
CF_BIN="build_assets/cloudflared"
if [ ! -f "$CF_BIN" ]; then
    echo "Downloading cloudflared ($CF_ARCH)..."
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-${CF_ARCH}.tgz" -o /tmp/cloudflared.tgz
    tar xzf /tmp/cloudflared.tgz -C build_assets/
    rm /tmp/cloudflared.tgz
    chmod +x "$CF_BIN"
fi

# Build .app
python3 -m PyInstaller \
    --name "Vinyl Player" \
    --windowed \
    --icon build_assets/VinylPlayer.icns \
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
    --add-data "build_assets/VinylPlayer.icns:." \
    --add-binary "${CF_BIN}:." \
    --collect-all libtorrent \
    --add-data "${JK_ARCH}:vendor/jackett" \
    --add-data "vendor/jackett/manifest.json:vendor/jackett" \
    vinyl_player.py

echo ""
echo "Creating DMG..."

APP_PATH="dist/Vinyl Player.app"
DMG_PATH="dist/VinylPlayer-macOS.dmg"

if [ -f "$DMG_PATH" ]; then rm "$DMG_PATH"; fi

mkdir -p dist/dmg
cp -r "$APP_PATH" dist/dmg/
ln -sf /Applications dist/dmg/Applications

hdiutil create -volname "Vinyl Player" -srcfolder dist/dmg -ov -format UDZO "$DMG_PATH"
rm -rf dist/dmg

echo ""
echo "Done! DMG: $DMG_PATH (includes cloudflared)"
