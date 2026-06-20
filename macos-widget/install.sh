#!/usr/bin/env bash
# Installs the Vinyl Player desktop widget into Übersicht.
# Symlinks the widget folder so future edits in the repo are picked up live.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WIDGET_SRC="$SCRIPT_DIR/vinyl-player.widget"
WIDGETS_DIR="$HOME/Library/Application Support/Übersicht/widgets"
LINK="$WIDGETS_DIR/vinyl-player.widget"

echo "==> Vinyl Player widget installer"

# 1. Ensure Übersicht is installed
if [ ! -d "/Applications/Übersicht.app" ]; then
  echo "[!] Übersicht не найден в /Applications."
  if command -v brew >/dev/null 2>&1; then
    echo "    Устанавливаю через Homebrew..."
    brew install --cask ubersicht
  else
    echo "    Установите Übersicht: https://tracesof.net/uebersicht/"
    echo "    или через Homebrew: brew install --cask ubersicht"
    exit 1
  fi
fi

# 2. Link the widget
mkdir -p "$WIDGETS_DIR"
if [ -L "$LINK" ] || [ -e "$LINK" ]; then
  echo "==> Удаляю прежнюю версию виджета"
  rm -rf "$LINK"
fi
ln -s "$WIDGET_SRC" "$LINK"
echo "==> Виджет связан: $LINK -> $WIDGET_SRC"

# 3. Launch Übersicht (it will pick up the widget)
open -a "Übersicht" || true

echo
echo "Готово. Если виджет не появился:"
echo "  • откройте Übersicht (иконка в строке меню) → Refresh All Widgets"
echo "  • разрешите Übersicht доступ в Системных настройках при первом запуске"
