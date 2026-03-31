#!/bin/bash
set -e
echo "Building Vinyl Player for macOS..."

# Install all dependencies (including optional)
pip3 install pyinstaller httpx mutagen vkpymusic musicbrainzngs Pillow 2>/dev/null

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
    --hidden-import vkpymusic.service \
    --hidden-import vkpymusic.models \
    --hidden-import vkpymusic.models.song \
    --hidden-import vkpymusic.models.playlist \
    --hidden-import vkpymusic.vk_api \
    --hidden-import vkpymusic.token_receiver \
    --hidden-import musicbrainzngs \
    --collect-all vkpymusic \
    --collect-all musicbrainzngs \
    --add-data "build_assets/VinylPlayer.icns:." \
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
echo "Done! DMG: $DMG_PATH"
echo "App: $APP_PATH"
