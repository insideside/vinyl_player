#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Vinyl Player desktop widget — one-shot installer for a fresh Mac.
# Double-click in Finder, or run:  bash macos-widget/setup.command
#
# Installs everything the widget needs and wires it to THIS machine's paths:
#   1. Homebrew            (if missing)
#   2. Übersicht           (the desktop-widget host)
#   3. Python venv + deps  (so vinyl_player.py runs)
#   4. The widget itself   (copied into Übersicht with correct paths baked in)
#
# Re-runnable: safe to run again to update deps or repair the install.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP="$REPO_DIR/vinyl_player.py"
LOG="$REPO_DIR/logs/widget_player.log"
VENV="$REPO_DIR/.venv"
REQ="$REPO_DIR/requirements.txt"
WIDGETS_DIR="$HOME/Library/Application Support/Übersicht/widgets"
DEST="$WIDGETS_DIR/vinyl-player.widget"

echo "════════════════════════════════════════════"
echo " Vinyl Player widget — установка"
echo " Проект: $REPO_DIR"
echo "════════════════════════════════════════════"

if [ ! -f "$APP" ]; then
  echo "[✗] Не найден $APP — запускайте этот файл из папки проекта (macos-widget/)."
  exit 1
fi

# ── 1. Homebrew ──────────────────────────────────────────────────────────────
if ! command -v brew >/dev/null 2>&1; then
  echo "==> Homebrew не найден. Устанавливаю (может запросить пароль)..."
  NONINTERACTIVE=1 /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
# Make brew available in this session regardless of shell config
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
[ -x /usr/local/bin/brew ]   && eval "$(/usr/local/bin/brew shellenv)"

# ── 2. Übersicht ─────────────────────────────────────────────────────────────
if [ ! -d "/Applications/Übersicht.app" ]; then
  echo "==> Устанавливаю Übersicht..."
  brew install --cask ubersicht
else
  echo "==> Übersicht уже установлен."
fi

# ── 3. Python venv + dependencies ────────────────────────────────────────────
PY3="$(command -v python3 || true)"
if [ -z "$PY3" ]; then
  echo "==> Python3 не найден. Устанавливаю..."
  brew install python
  PY3="$(command -v python3)"
fi
echo "==> Создаю venv и ставлю зависимости ($PY3)..."
"$PY3" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$REQ"
PY="$VENV/bin/python"
# Sanity check — httpx is mandatory
if ! "$PY" -c "import httpx" 2>/dev/null; then
  echo "[✗] httpx не установился — проверьте вывод pip выше."
  exit 1
fi
echo "==> Зависимости установлены."

# ── 4. Install the widget with this machine's paths baked in ─────────────────
mkdir -p "$WIDGETS_DIR" "$REPO_DIR/logs"
rm -rf "$DEST"
cp -R "$SCRIPT_DIR/vinyl-player.widget" "$DEST"

# Escape values for safe use in a sed replacement
esc() { printf '%s' "$1" | sed -e 's/[&\\|]/\\&/g'; }
sed -i '' "s|^const PY .*|const PY   = \"$(esc "$PY")\";|"  "$DEST/index.jsx"
sed -i '' "s|^const APP .*|const APP  = \"$(esc "$APP")\";|" "$DEST/index.jsx"
sed -i '' "s|^const LOG .*|const LOG  = \"$(esc "$LOG")\";|" "$DEST/index.jsx"

echo "==> Виджет установлен: $DEST"
echo "    PY  = $PY"
echo "    APP = $APP"

# ── 5. Launch ────────────────────────────────────────────────────────────────
open -a "Übersicht" || true

cat <<EOF

════════════════════════════════════════════
 Готово ✅
 • Виджет появится на рабочем столе (Übersicht).
 • Тумблер запускает плеер; откройте http://127.0.0.1:7656
 • Если виджета нет: иконка Übersicht в меню-баре → Refresh All Widgets,
   и разрешите Übersicht доступ при первом запуске.
════════════════════════════════════════════
EOF
