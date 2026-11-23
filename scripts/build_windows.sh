#!/bin/bash
# Кросс-сборка AmneziaMonitor.exe на Linux: Wine + официальный CPython amd64 + PyInstaller.
# Кросс-сборка через Wine на Linux.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -z "${WINE_BIN_DIR:-}" ]; then
  for d in "$HOME/wine/wine-10.17-amd64-wow64/bin" "$HOME/wine/wine-10.17-amd64/bin"; do
    if [ -x "$d/wine" ]; then WINE_BIN_DIR="$d"; break; fi
  done
fi
if [ -n "${WINE_BIN_DIR:-}" ] && [ -x "$WINE_BIN_DIR/wine" ]; then
  export PATH="$WINE_BIN_DIR:$PATH"
fi

# Собирать только из полного дерева репозитория: без app_window.py exe открывал панель в браузере.
for f in app_main.py app_window.py hostkeys.py dns_setup.py blockcheck.py theme.py theme_assets/heroines/heroine_default.webp theme_assets/flags/un.svg theme_assets/backgrounds/bg_default.webp dashboard.html AmneziaMonitor.spec; do
  [ -f "$ROOT/$f" ] || { echo "нет $f — это не полное дерево репозитория, сборка остановлена" >&2; exit 1; }
done
WINEPREFIX="${WINEPREFIX:-$HOME/.wine-amnezia}"
export WINEPREFIX WINEARCH=win64 WINEDEBUG="${WINEDEBUG:--all}"
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree,mshtml=}"
PY_WIN="C:\\Python312\\python.exe"
PY_VERSION="${PY_VERSION:-3.12.10}"
CACHE="${CACHE:-$HOME/.cache/amnezia-wine}"
INSTALLER="$CACHE/python-${PY_VERSION}-amd64.exe"
URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-amd64.exe"
OUT_DIR="${OUT_DIR:-$ROOT/dist}"
VERSION="${VERSION:-v1.4.0}"
PKG="AmneziaMonitor-win-x64-${VERSION}"
ZIP_NAME="${ZIP_NAME:-$PKG.zip}"
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

# В exe должны быть окно (pywebview + WebView2) и проверка ключей хостов.
if command -v pyi-archive_viewer >/dev/null 2>&1; then
  listing="$(pyi-archive_viewer -l -r "$ROOT/dist/AmneziaMonitor.exe" 2>/dev/null || true)"
  for m in app_window hostkeys dns_setup blockcheck theme heroine_default.webp webview.platforms.edgechromium WebView2Loader; do
    echo "$listing" | grep -q "$m" || { echo "в exe нет модуля $m" >&2; exit 1; }
  done
  echo "modules check ok"
else
  echo "pyi-archive_viewer нет на Linux — проверка модулей пропущена" >&2
fi

echo "== zip =="
rm -rf "$STAGE"
mkdir -p "$STAGE/$PKG" "$OUT_DIR"
cp "$ROOT/dist/AmneziaMonitor.exe" "$STAGE/$PKG/"
cat > "$STAGE/$PKG/START.txt" << 'TXT'
Amnezia Monitor — Windows x64 (v1.4.0)

Запуск: двойной щелчок по AmneziaMonitor.exe.
Панель открывается в своём окне программы (Microsoft Edge WebView2), а не в браузере.
Крестик прячет окно в трей, мониторинг продолжается; «Открыть панель» в трее
показывает окно снова, «Выход» закрывает программу. Повторный запуск не создаёт
вторую копию, а показывает уже открытое окно.
WebView2 Runtime уже есть в Windows 11 и в новых Windows 10. Если его нет, программа
объяснит причину и спросит, открыть ли панель в браузере (только запасной вариант).

Настройки и база — в папке %APPDATA%\AmneziaMonitor (servers.yaml,
amnezia_monitor.db, known_hosts, журнал).

Что нового в v1.4.0
- Новое оформление панели «Neon Latex»: тёмный неоново-фиолетовый фон со
  свечением, стеклянные карточки, глобус и героиня в цветах флага страны
  сервера справа, падающие лепестки сакуры.
- Флаг страны на вкладке каждого сервера (с точкой статуса), в заголовке
  сервера и в списке Настроек. Страна определяется так: поле «Страна» в
  Настройках → Изменить (вручную) > слова в названии (Frankfurt, Svecia,
  Вена, Kazahi, Питер, London…) > GeoIP по IP (выключен по умолчанию;
  Настройки → Оформление; IP уходит на ip-api.com, ответ хранится 30 дней).
- 14 стран со своей героиней (SE, US, AT, KZ, NL, RU, PL, DE, FR, GB, FI,
  IN, IL, HK) и ещё ~50 флагов с общей героиней Amnezia.
- Плавная смена цветов, героини и флага при переключении сервера.
- Переключатель «Анимации» в шапке и в Настройках (запоминается). Если в
  Windows выключены эффекты анимации, панель их тоже не показывает.
- Узкое окно (меньше ~1100 px): колонка с героиней скрывается.
- Исправлено: время на графике трафика и в «Событиях» показывалось неверно.

Что было в v1.3.0
- Проверка блокировок на карточке сервера: SSH и порты VPN (Xray/Reality,
  AmneziaWG) проверяются с этого ПК и с узлов check-host.net в России и в
  других странах. Вердикт: «Доступен везде», «Признаки блокировки в РФ»
  (в том числе ТСПУ/DPI: TCP подключается, затем сброс или тишина),
  «Недоступен отовсюду», «Частично доступен». Вкладки «Россия» и «Мир»,
  задержки, «проверено N мин назад», кнопка «Проверить сейчас» (результат
  хранится 30 минут). Фоновая проверка раз в 6 часов (Настройки), история —
  в базе, смена статуса — в «Событиях». На сервер программа для этого не
  входит и ничего на нём не меняет. UDP (AmneziaWG) снаружи не проверить —
  это честно помечено «не проверяется извне».
- Порты контейнеров в сети хоста можно добавить в карточке сервера
  (Настройки → Изменить → «Доп. порты», например 8443/tls).
- Если VPN на этом ПК пускает весь трафик через туннель, проверка «с этого
  ПК» пойдёт через VPN — для честного результата из РФ отключите его.
- Повторная проверка: если порт или сервер показал проблему (блокировка,
  «недоступен», «частично»), через ~2,5 минуты программа сама проверяет его
  ещё раз (с этого ПК; check-host — только если он не на паузе из-за лимита).
  Статус, событие «Статус изменился» и значок 🚫 меняются, только если
  проблема подтвердилась («подтверждено повторной проверкой»); иначе это
  записывается как кратковременный сбой. Пока ждём — «перепроверка…».
- Ручная проверка с этого ПК: кнопки «Проверить с этого ПК сейчас» (весь
  сервер) и «с ПК» (один порт) проверяют сразу, без check-host и без
  30-минутного ожидания. Кнопка полной проверки пишет, во сколько она снова
  будет доступна.
- Исправлено: на части серверов (с включённым DNS-журналом) не показывалась
  загрузка CPU («—», «CPU за 24 ч макс. 0%»), не строился график трафика и не
  появлялись события подключения клиентов. Теперь CPU считается двумя
  чтениями /proc/stat в одной команде и виден уже с первого опроса.

Что было в v1.2.0
- Смена ключа SSH сервера. «Настройки → Проверить подключение» показывает
  сохранённый и новый отпечатки SHA256 и кнопку «Доверять новому ключу».
  Ключ заменяется только после подтверждения и только если сервер всё ещё
  показывает тот же ключ. Пароль на сервер со сменившимся ключом не уходит.
- Ключи хостов программа хранит в своём файле known_hosts в папке данных.
  Файл ~/.ssh/known_hosts (OpenSSH) только читается и больше не перезаписывается.
- Тип ключа согласуется с сохранённым (RSA/ECDSA/ED25519), поэтому ложной
  «смены ключа» из-за другого типа больше нет.
- DNS-журнал: кнопки «Включить DNS-журнал», «Проверить», «Отключить» на
  карточке сервера (хранение около 24 часов, потолок 50 МБ). VPN и каскад
  не перезапускаются.

Учтите: DNS-журнал хранит домены, которые спрашивали устройства ваших
пользователей. Предупредите их, если нужно.

Файл не подписан. SmartScreen может показать «Windows защитила ваш компьютер» —
«Подробнее» — «Выполнить в любом случае». Антивирусы иногда ложно ругаются на
однофайловые сборки PyInstaller — тогда добавьте файл в исключения.
TXT
( cd "$STAGE" && sha256sum $PKG/AmneziaMonitor.exe > $PKG/SHA256SUMS.txt )
# в архиве не должно быть паролей, ключей, ip-списков
if find "$STAGE" -iname '*.db' -o -iname '*.yaml' -o -iname 'master.key' -o -iname 'id_rsa' | grep -q .; then
  echo "в каталог сборки попали лишние файлы" >&2
  exit 1
fi
ZIP_PATH="$OUT_DIR/$ZIP_NAME"
rm -f "$ZIP_PATH"
python3 "$ROOT/scripts/make_windows_zip.py" "$STAGE" "$ZIP_PATH"
sha256sum "$ZIP_PATH" | tee "$ZIP_PATH.sha256"
echo "готово: $ZIP_PATH"
