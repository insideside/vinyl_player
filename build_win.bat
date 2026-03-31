@echo off
echo Building Vinyl Player for Windows...

pip install pyinstaller httpx mutagen vkpymusic musicbrainzngs Pillow 2>nul

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
    --hidden-import vkpymusic.service ^
    --hidden-import vkpymusic.models ^
    --hidden-import vkpymusic.models.song ^
    --hidden-import vkpymusic.models.playlist ^
    --hidden-import vkpymusic.vk_api ^
    --hidden-import vkpymusic.token_receiver ^
    --hidden-import musicbrainzngs ^
    --collect-all vkpymusic ^
    --collect-all musicbrainzngs ^
    vinyl_player.py

echo.
echo Done! EXE: dist\VinylPlayer.exe
pause
