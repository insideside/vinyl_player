@echo off
echo Building Vinyl Player for Windows...

pip install pyinstaller httpx mutagen Pillow 2>nul

pyinstaller ^
    --name "VinylPlayer" ^
    --windowed ^
    --icon build_assets\icon_256.png ^
    --onefile ^
    --noconfirm ^
    --clean ^
    --hidden-import httpx ^
    --hidden-import mutagen ^
    --hidden-import mutagen.mp3 ^
    --hidden-import mutagen.id3 ^
    --hidden-import mutagen.flac ^
    --hidden-import mutagen.mp4 ^
    --hidden-import mutagen.oggvorbis ^
    vinyl_player.py

echo.
echo Done! EXE: dist\VinylPlayer.exe
pause
