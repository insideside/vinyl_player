@echo off
echo Building Vinyl Player for Windows...

pip install pyinstaller httpx mutagen vkpymusic musicbrainzngs Pillow 2>nul

:: Download cloudflared if not present
if not exist "build_assets\cloudflared.exe" (
    echo Downloading cloudflared for Windows...
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -o "build_assets\cloudflared.exe"
)

python -m PyInstaller ^
    --name "VinylPlayer" ^
    --windowed ^
    --icon build_assets\icon_256.png ^
    --onefile ^
    --noconfirm ^
    --clean ^
    --hidden-import httpx ^
    --hidden-import httpx._transports ^
    --hidden-import httpx._transports.default ^
    --hidden-import httpcore ^
    --hidden-import httpcore._async ^
    --hidden-import httpcore._sync ^
    --hidden-import h11 ^
    --hidden-import anyio ^
    --hidden-import anyio._backends ^
    --hidden-import anyio._backends._asyncio ^
    --hidden-import certifi ^
    --hidden-import mutagen ^
    --hidden-import mutagen.mp3 ^
    --hidden-import mutagen.id3 ^
    --hidden-import mutagen.id3._frames ^
    --hidden-import mutagen.id3._specs ^
    --hidden-import mutagen.flac ^
    --hidden-import mutagen.mp4 ^
    --hidden-import mutagen.oggvorbis ^
    --hidden-import mutagen.ogg ^
    --hidden-import vkpymusic ^
    --hidden-import musicbrainzngs ^
    --collect-all vkpymusic ^
    --collect-all musicbrainzngs ^
    --add-binary "build_assets\cloudflared.exe;." ^
    vinyl_player.py

echo.
echo Done! EXE: dist\VinylPlayer.exe (includes cloudflared)
pause
