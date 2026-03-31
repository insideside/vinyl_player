#!/bin/bash
set -e
echo "Building Vinyl Player for macOS..."

# Install dependencies
pip3 install pyinstaller httpx mutagen Pillow 2>/dev/null

# Build .app
python3 -m PyInstaller \
    --name "Vinyl Player" \
    --windowed \
    --icon build_assets/VinylPlayer.icns \
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
    --add-data "build_assets/VinylPlayer.icns:." \
    vinyl_player.py

echo ""
echo "Creating DMG..."

# Create DMG
APP_PATH="dist/Vinyl Player.app"
DMG_PATH="dist/VinylPlayer-macOS.dmg"

if [ -f "$DMG_PATH" ]; then rm "$DMG_PATH"; fi

# Create temporary DMG directory
mkdir -p dist/dmg
cp -r "$APP_PATH" dist/dmg/
ln -sf /Applications dist/dmg/Applications

hdiutil create -volname "Vinyl Player" -srcfolder dist/dmg -ov -format UDZO "$DMG_PATH"
rm -rf dist/dmg

echo ""
echo "Done! DMG: $DMG_PATH"
echo "App: $APP_PATH"
