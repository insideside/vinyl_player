@echo off
chcp 65001 >nul
:: Delayed expansion is needed for !VAR! inside for/if blocks below
:: (version detection and per-package install loop).
setlocal enabledelayedexpansion
title insideside music — Server Setup

echo.
echo ============================================
echo   insideside music — Windows Server Setup
echo ============================================
echo.

:: Check admin rights
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Запустите этот файл от имени администратора!
    echo     ПКМ → Запуск от имени администратора
    pause
    exit /b 1
)

:: Config
set APP_PORT=7656
set APP_DIR=C:\insideside-music
:: Pinned Python — known-good wheels for all our deps (cryptography, vkpymusic etc.)
set PINNED_PY_VERSION=3.12.7
set PINNED_PY_DIR=C:\Program Files\Python312
set PYTHON_URL=https://www.python.org/ftp/python/%PINNED_PY_VERSION%/python-%PINNED_PY_VERSION%-amd64.exe
set PYTHON_INSTALLER=%TEMP%\python_installer.exe
set SERVICE_NAME=InsideMusic
:: PY_EXE — explicit interpreter we'll use for pip and runtime (avoids picking up
:: a too-new system Python that has no wheels for cryptography/vkpymusic deps).
set PY_EXE=

echo [1/7] Проверка Python...

:: 1. Prefer existing pinned 3.12 install
if exist "%PINNED_PY_DIR%\python.exe" (
    set "PY_EXE=%PINNED_PY_DIR%\python.exe"
    echo       Найден совместимый Python 3.12 в %PINNED_PY_DIR%.
    goto :py_ready
)

:: 2. Check system python — only use if version is in supported range (3.10..3.13)
set SYS_PY_VER=
set SYS_PY_MINOR=
python --version >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set SYS_PY_VER=%%v
    for /f "tokens=1,2 delims=." %%a in ("!SYS_PY_VER!") do (
        set SYS_PY_MAJOR=%%a
        set SYS_PY_MINOR=%%b
    )
    if "!SYS_PY_MAJOR!"=="3" if !SYS_PY_MINOR! GEQ 10 if !SYS_PY_MINOR! LEQ 13 (
        set "PY_EXE=python"
        goto :py_ready
    )
    echo       Найден Python !SYS_PY_VER!, но он несовместим (нужен 3.10–3.13^).
    echo       Установлю Python %PINNED_PY_VERSION% рядом, ваш Python не будет затронут.
) else (
    echo       Python не найден. Скачиваю Python %PINNED_PY_VERSION%...
)

:: 3. Download and install pinned Python 3.12 alongside any existing version
curl -fsSL -o "%PYTHON_INSTALLER%" "%PYTHON_URL%"
if %errorlevel% neq 0 (
    echo [!] Не удалось скачать Python. Установите вручную: https://python.org
    pause
    exit /b 1
)
echo       Устанавливаю Python %PINNED_PY_VERSION%...
:: InstallAllUsers=1 → C:\Program Files\Python312. PrependPath=0 to NOT clobber
:: user's existing python on PATH — we use the explicit path instead.
"%PYTHON_INSTALLER%" /quiet InstallAllUsers=1 PrependPath=0 Include_pip=1 TargetDir="%PINNED_PY_DIR%"
del "%PYTHON_INSTALLER%"
if not exist "%PINNED_PY_DIR%\python.exe" (
    echo [!] Установка Python не удалась. Проверьте C:\Program Files\Python312.
    pause
    exit /b 1
)
set "PY_EXE=%PINNED_PY_DIR%\python.exe"
echo       Python %PINNED_PY_VERSION% установлен в %PINNED_PY_DIR%.

:py_ready
echo       Используется: %PY_EXE%

echo.
echo [2/7] Установка зависимостей...
"%PY_EXE%" -m pip install --upgrade pip >nul 2>&1
:: Install per-package with --only-binary so failed wheels are reported, not silently
:: source-built (which would fail without compiler/Rust). Each package is independent —
:: the app degrades gracefully if vkpymusic / cryptography / mutagen / musicbrainzngs
:: are missing (HAS_VK, HAS_MUTAGEN, HAS_MB flags + try/except in vinyl_player.py).
set INSTALL_FAILED=
for %%P in (httpx mutagen vkpymusic musicbrainzngs cryptography) do (
    "%PY_EXE%" -m pip install --only-binary=:all: %%P >nul 2>&1
    if errorlevel 1 (
        echo       [!] %%P не удалось установить — пропускаю.
        set INSTALL_FAILED=!INSTALL_FAILED! %%P
    ) else (
        echo       [+] %%P установлен.
    )
)
if defined INSTALL_FAILED (
    echo.
    echo       Не установлены:!INSTALL_FAILED!
    echo       Что отключится без них (приложение всё равно запустится, кроме httpx^):
    echo         vkpymusic       → импорт треков из VK
    echo         mutagen         → метаданные и обложки из тегов
    echo         musicbrainzngs  → автопоиск метаданных
    echo         cryptography    → автогенерация HTTPS-сертификата (можно вручную через openssl)
    echo         httpx           → КРИТИЧНО, без него приложение не запустится
)

echo.
echo [3/7] Создание директории приложения...
if not exist "%APP_DIR%" mkdir "%APP_DIR%"
if not exist "%APP_DIR%\music" mkdir "%APP_DIR%\music"

:: Download latest from GitHub
echo.
echo [4/7] Скачивание приложения из GitHub...
curl -fsSL -o "%APP_DIR%\vinyl_player.py" "https://raw.githubusercontent.com/insideside/vinyl_player/main/vinyl_player.py"
if %errorlevel% neq 0 (
    echo [!] Не удалось скачать. Проверьте подключение к интернету.
    pause
    exit /b 1
)
echo       Приложение скачано: %APP_DIR%\vinyl_player.py

:: Download cloudflared
echo.
echo [5/7] Скачивание cloudflared...
if not exist "%APP_DIR%\cloudflared.exe" (
    curl -fsSL -o "%APP_DIR%\cloudflared.exe" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    echo       cloudflared скачан.
) else (
    echo       cloudflared уже есть.
)

:: Firewall rule
echo.
echo [6/7] Настройка брандмауэра Windows...
netsh advfirewall firewall show rule name="InsideMusic" >nul 2>&1
if %errorlevel% neq 0 (
    netsh advfirewall firewall add rule name="InsideMusic" dir=in action=allow protocol=TCP localport=%APP_PORT% >nul
    netsh advfirewall firewall add rule name="InsideMusic" dir=out action=allow protocol=TCP localport=%APP_PORT% >nul
    echo       Правило брандмауэра создано: порт %APP_PORT% открыт.
) else (
    echo       Правило брандмауэра уже существует.
)

:: Create startup script
echo.
echo [7/7] Создание скриптов запуска...

:: Run script — uses the explicit Python we picked above (PY_EXE), not whatever
:: happens to be on PATH at runtime. This survives the user installing a newer
:: Python later that breaks our deps.
(
echo @echo off
echo chcp 65001 ^>nul
echo title insideside music Server
echo cd /d "%APP_DIR%"
echo echo Starting insideside music on port %APP_PORT%...
echo echo.
echo "%PY_EXE%" vinyl_player.py --public
echo pause
) > "%APP_DIR%\start_server.bat"

:: Auto-start via Task Scheduler
echo.
echo Создание автозапуска при загрузке Windows...
schtasks /query /tn "%SERVICE_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
    schtasks /delete /tn "%SERVICE_NAME%" /f >nul 2>&1
)
schtasks /create /tn "%SERVICE_NAME%" /tr "\"%APP_DIR%\start_server.bat\"" /sc onlogon /rl highest /f >nul 2>&1
if %errorlevel% equ 0 (
    echo       Автозапуск настроен (при входе в систему^).
) else (
    echo       [!] Не удалось настроить автозапуск. Запускайте вручную.
)

:: Get IP
echo.
echo ============================================
echo   Установка завершена!
echo ============================================
echo.
echo   Директория: %APP_DIR%
echo   Порт: %APP_PORT%
echo.

:: Show IP addresses
echo   Адреса для подключения:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=1" %%b in ("%%a") do (
        echo     http://%%b:%APP_PORT%
    )
)
echo.
echo   Запуск сервера:  %APP_DIR%\start_server.bat
echo   Или автоматически при входе в Windows.
echo.
echo   Первый запуск: откройте браузер, создайте аккаунт админа.
echo   Корневая папка музыки: %APP_DIR%\music
echo.

:: Ask to start now
set /p STARTNOW="Запустить сервер сейчас? (y/n): "
if /i "%STARTNOW%"=="y" (
    start "" "%APP_DIR%\start_server.bat"
    echo.
    echo   Сервер запускается...
    timeout /t 3 >nul
    start http://127.0.0.1:%APP_PORT%
)

echo.
pause
