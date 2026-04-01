@echo off
chcp 65001 >nul
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
set PYTHON_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
set PYTHON_INSTALLER=%TEMP%\python_installer.exe
set SERVICE_NAME=InsideMusic

echo [1/7] Проверка Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo       Python не найден. Скачиваю...
    curl -fsSL -o "%PYTHON_INSTALLER%" "%PYTHON_URL%"
    if %errorlevel% neq 0 (
        echo [!] Не удалось скачать Python. Установите вручную: https://python.org
        pause
        exit /b 1
    )
    echo       Устанавливаю Python 3.11...
    "%PYTHON_INSTALLER%" /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1
    del "%PYTHON_INSTALLER%"
    :: Refresh PATH
    set "PATH=C:\Program Files\Python311;C:\Program Files\Python311\Scripts;%PATH%"
    echo       Python установлен.
) else (
    echo       Python найден.
)

echo.
echo [2/7] Установка зависимостей...
python -m pip install --upgrade pip >nul 2>&1
pip install httpx mutagen vkpymusic musicbrainzngs 2>nul
echo       Зависимости установлены.

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

:: Run script
(
echo @echo off
echo chcp 65001 ^>nul
echo title insideside music Server
echo cd /d "%APP_DIR%"
echo echo Starting insideside music on port %APP_PORT%...
echo echo.
echo python vinyl_player.py --public
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
