#!/bin/bash
# Кросс-сборка AmneziaMonitor.exe на Linux: Wine + официальный CPython amd64 + PyInstaller.
# Кросс-сборка через Wine на Linux.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

WINE_BIN_DIR="${WINE_BIN_DIR:-$HOME/wine/wine-10.17-amd64-wow64/bin}"
if [ -x "$WINE_BIN_DIR/wine" ]; then
  export PATH="$WINE_BIN_DIR:$PATH"
fi
WINEPREFIX="${WINEPREFIX:-$HOME/.wine-amnezia}"
export WINEPREFIX WINEARCH=win64 WINEDEBUG="${WINEDEBUG:--all}"
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree,mshtml=}"
PY_WIN="C:\\Python312\\python.exe"
PY_VERSION="${PY_VERSION:-3.12.10}"
CACHE="${CACHE:-$HOME/.cache/amnezia-wine}"
INSTALLER="$CACHE/python-${PY_VERSION}-amd64.exe"
URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-amd64.exe"
OUT_DIR="${OUT_DIR:-$ROOT/dist}"
ZIP_NAME="${ZIP_NAME:-AmneziaMonitor-win-x64-dns-journal.zip}"
STAGE="$ROOT/build/windows-zip"

need() { command -v "$1" >/dev/null 2>&1 || { echo "нет команды: $1" >&2; exit 1; }; }
need wine
need xvfb-run
need curl

mkdir -p "$CACHE"
if [ ! -f "$INSTALLER" ]; then
  echo "скачиваю $URL"
  curl -fL --retry 3 -o "$INSTALLER.partial" "$URL"
  mv "$INSTALLER.partial" "$INSTALLER"
fi

if [ ! -d "$WINEPREFIX/drive_c" ]; then
  mkdir -p "$WINEPREFIX"
  echo "== wineboot =="
  xvfb-run -a wineboot --init
fi

echo "== Windows Python =="
if ! xvfb-run -a wine "$PY_WIN" -c "import sys; print(sys.version)"; then
  echo "тихая установка Python в C:\\Python312"
  xvfb-run -a wine "$INSTALLER" /quiet InstallAllUsers=1 PrependPath=0 Include_test=0 Include_launcher=0 Include_pip=1 "TargetDir=C:\\Python312"
  ok=0
  for _ in $(seq 1 15); do
    if xvfb-run -a wine "$PY_WIN" -c "import sys; print(sys.version)"; then ok=1; break; fi
    sleep 3
  done
  if [ "$ok" != 1 ]; then
    echo "Python под Wine не запустился" >&2
    exit 1
  fi
fi

SITE_CUSTOM="$WINEPREFIX/drive_c/Python312/Lib/site-packages/sitecustomize.py"
if [ ! -f "$SITE_CUSTOM" ]; then
  mkdir -p "$(dirname "$SITE_CUSTOM")"
  cat > "$SITE_CUSTOM" << 'PY'
import shutil
_orig_copytree = shutil.copytree

def _copy2(src, dst, *args, **kwargs):
    return shutil.copy(src, dst)

def _copytree(src, dst, symlinks=False, ignore=None, copy_function=None,
              ignore_dangling_symlinks=False, dirs_exist_ok=False):
    return _orig_copytree(
        src, dst, symlinks=symlinks, ignore=ignore, copy_function=shutil.copy,
        ignore_dangling_symlinks=ignore_dangling_symlinks, dirs_exist_ok=dirs_exist_ok,
    )

shutil.copy2 = _copy2
shutil.copytree = _copytree
PY
fi

echo "== pip / PyInstaller =="
xvfb-run -a wine "$PY_WIN" -m pip install --disable-pip-version-check --upgrade pip
xvfb-run -a wine "$PY_WIN" -m pip install --disable-pip-version-check -r "$ROOT/requirements-app.txt" "pyinstaller==6.11.1"

echo "== тесты на Linux (отдельно), сборка exe =="
rm -rf "$ROOT/build/amnezia" "$ROOT/dist/AmneziaMonitor.exe" "$ROOT/dist/AmneziaMonitor"
xvfb-run -a wine "$PY_WIN" -m PyInstaller --noconfirm --clean "$ROOT/AmneziaMonitor.spec"
test -f "$ROOT/dist/AmneziaMonitor.exe"

# PE-заголовок
python3 - "$ROOT/dist/AmneziaMonitor.exe" << 'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
data = p.read_bytes()[:64]
assert data[:2] == b"MZ", data[:8]
print("PE check ok, size", p.stat().st_size)
PY

echo "== zip =="
rm -rf "$STAGE"
mkdir -p "$STAGE/AmneziaMonitor-win-x64-dns-journal" "$OUT_DIR"
cp "$ROOT/dist/AmneziaMonitor.exe" "$STAGE/AmneziaMonitor-win-x64-dns-journal/"
cat > "$STAGE/AmneziaMonitor-win-x64-dns-journal/START.txt" << 'TXT'
Amnezia Monitor — Windows x64

Запуск: двойной щелчок по AmneziaMonitor.exe.
Откроется панель в браузере (http://127.0.0.1:8765). Настройки и база —
в папке %APPDATA%\AmneziaMonitor. Повторный запуск не создаёт вторую
копию, а открывает уже работающую панель.

Что нового в этой сборке
- На карточке сервера кнопки «Включить DNS-журнал», «Проверить» и
  «Отключить». Они по SSH сами настраивают журнал в контейнере
  amnezia-dns: хранение около 24 часов, потолок 50 МБ, очистка каждые
  15 минут.
- Действие можно повторять: если журнал уже включён, конфиг не ломается.
- Перезапускается только контейнер DNS и только если конфиг изменился.
  VPN (AmneziaWG/WireGuard) и службы каскада (cascade-xray) не трогаются.
- Панель читает журнал Unbound из файла рядом с конфигом и из архивов
  за сутки (раньше брались только стандартные пути и лог контейнера).

Учтите: журнал хранит домены, которые спрашивали устройства ваших
пользователей. Предупредите их, если нужно.

Антивирусы иногда ложно ругаются на однофайловые сборки PyInstaller.
Если так — добавьте файл в исключения.
TXT
( cd "$STAGE" && sha256sum AmneziaMonitor-win-x64-dns-journal/AmneziaMonitor.exe > AmneziaMonitor-win-x64-dns-journal/SHA256SUMS.txt )
# в архиве не должно быть паролей, ключей, ip-списков
if find "$STAGE" -iname '*.db' -o -iname '*.yaml' -o -iname 'master.key' -o -iname 'id_rsa' | grep -q .; then
  echo "в каталог сборки попали лишние файлы" >&2
  exit 1
fi
ZIP_PATH="$OUT_DIR/$ZIP_NAME"
rm -f "$ZIP_PATH"
python3 /tmp/am_make_zip.py "$STAGE" "$ZIP_PATH"
sha256sum "$ZIP_PATH" | tee "$ZIP_PATH.sha256"
echo "готово: $ZIP_PATH"
