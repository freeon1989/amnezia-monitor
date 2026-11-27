#!/bin/bash
# Публикация релиза Amnezia Monitor на GitHub с файлами ПОСТОЯННЫХ имён (их ищет обновление из программы):
#   AmneziaMonitor-win-x64.zip   + AmneziaMonitor-win-x64.zip.sha256
#   AmneziaMonitor-android.apk   + AmneziaMonitor-android.apk.sha256
# Использование: scripts/publish_release.sh <папка с файлами> <файл с описанием релиза> [--draft]
# Нужен gh с входом в аккаунт. Тег vX.Y.Z берётся из version.py и должен совпадать с versionName в android/app/build.gradle.kts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="${1:?папка с файлами релиза}"
NOTES="${2:?файл с описанием релиза}"
EXTRA="${3:-}"
VER="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); from version import __version__ as v; print(v)")"
TAG="v$VER"
grep -q "versionName = \"$VER\"" "$ROOT/android/app/build.gradle.kts" || { echo "versionName в Android не равен $VER" >&2; exit 1; }
ASSETS=()
for f in AmneziaMonitor-win-x64.zip AmneziaMonitor-android.apk; do
  [ -f "$DIST/$f" ] || { echo "нет $DIST/$f" >&2; exit 1; }
  [ -f "$DIST/$f.sha256" ] || { echo "нет $DIST/$f.sha256" >&2; exit 1; }
  want="$(awk '{print $1}' "$DIST/$f.sha256")"
  have="$(sha256sum "$DIST/$f" | awk '{print $1}')"
  [ "$want" = "$have" ] || { echo "SHA-256 $f не совпадает с .sha256" >&2; exit 1; }
  grep -q " $f\$" "$DIST/$f.sha256" || { echo "в $f.sha256 должно быть имя файла $f" >&2; exit 1; }
  ASSETS+=("$DIST/$f" "$DIST/$f.sha256")
done
gh release create "$TAG" "${ASSETS[@]}" --title "AmneziaMonitor $TAG" --notes-file "$NOTES" --target main $EXTRA
echo "готово: https://github.com/freeon1989/amnezia-monitor/releases/tag/$TAG"
