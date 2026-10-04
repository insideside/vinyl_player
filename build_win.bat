@echo off
echo Building Vinyl Player for Windows...

pip install pyinstaller httpx mutagen vkpymusic musicbrainzngs Pillow libtorrent 2>nul

rem Jackett installer for this platform (search over trackers, installed from the app on demand)
for /f "delims=" %%i in ('python scripts\fetch_jackett.py --current') do set JK_ARCH=%%i

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
    --collect-all libtorrent ^
    --add-data "%JK_ARCH%;vendor/jackett" ^
    --add-data "vendor\jackett\manifest.json;vendor/jackett" ^
    vinyl_player.py

echo.
echo Done! EXE: dist\VinylPlayer.exe (includes cloudflared)
pause
