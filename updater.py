# -*- coding: utf-8 -*-
"""Обновление Amnezia Monitor с GitHub Releases.

Принципы:
  * ничего не скачивается и не ставится без нажатия пользователя («Обновить»);
    проверка наличия новой версии только читает api.github.com;
  * скачивание только по HTTPS и только с хостов GitHub (в том числе после перенаправлений);
  * архив принимается, только если его SHA-256 совпал с файлом .sha256 из того же релиза;
  * подмену файла делает небольшой помощник после выхода программы: старый exe сохраняется как
    AmneziaMonitor.exe.bak, при сбое нового запуска возвращается автоматически;
  * папка данных (%APPDATA%\\AmneziaMonitor: настройки, база, known_hosts) не затрагивается.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from version import __version__

log = logging.getLogger("amnezia_monitor.update")

REPO = "freeon1989/amnezia-monitor"
API_BASE = "https://api.github.com"
GITHUB_HOSTS = frozenset({
    "api.github.com", "github.com",
    "objects.githubusercontent.com", "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
})
WIN_ASSET = "AmneziaMonitor-win-x64.zip"          # стабильные имена файлов релиза
WIN_SHA_ASSET = WIN_ASSET + ".sha256"
EXE_NAME = "AmneziaMonitor.exe"
TAG_RE = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
HASH_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
MAX_NOTES = 20_000
MAX_ZIP = 300 * 1024 * 1024
MAX_EXE = 400 * 1024 * 1024
MIN_EXE = 5 * 1024 * 1024                            # настоящий exe заметно больше
HTTP_TIMEOUT = 20
CHECK_INTERVAL = 6 * 3600


class UpdateError(Exception):
    """Понятная пользователю причина, почему обновление не удалось."""


# --------------------------------------------------------------------------- версии
def parse_version(text: str):
    m = TAG_RE.match(str(text or "").strip())
    if not m:
        raise UpdateError("Непонятный номер версии: %r" % (str(text)[:30],))
    return tuple(int(x) for x in m.groups())


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


# --------------------------------------------------------------------------- настройка источника
@dataclass
class Source:
    repo: str = REPO
    api_base: str = API_BASE
    allowed_hosts: frozenset = GITHUB_HOSTS
    allowed_schemes: tuple = ("https",)          # в тестах — ("http",) и локальный сервер
    asset_name: str = WIN_ASSET
    sha_name: str = WIN_SHA_ASSET
    min_exe: int = MIN_EXE
    timeout: float = HTTP_TIMEOUT


def _check_url(url: str, src: Source) -> str:
    p = urllib.parse.urlparse(url)
    if p.scheme not in src.allowed_schemes:
        raise UpdateError("Адрес обновления должен быть HTTPS")
    host = (p.hostname or "").lower()
    if host not in src.allowed_hosts:
        raise UpdateError("Адрес обновления не с GitHub: %s" % host[:60])
    return url


def release_page_ok(url: str, src: Source) -> bool:
    """Страницу релиза открываем в браузере, только если она на github.com в нашем репозитории."""
    p = urllib.parse.urlparse(url or "")
    return (p.scheme in src.allowed_schemes and (p.hostname or "").lower() in src.allowed_hosts
            and p.path.startswith("/%s/releases" % src.repo))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, src: Source):
        self.src = src

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl, self.src)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(src: Source):
    return urllib.request.build_opener(_SafeRedirect(src))


def _get(url: str, src: Source, limit: int, accept: str = "*/*"):
    _check_url(url, src)
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "AmneziaMonitor/" + __version__})
    try:
        return _opener(src).open(req, timeout=src.timeout)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise UpdateError("GitHub временно ограничил запросы (код %d). Попробуйте позже." % e.code)
        if e.code == 404:
            raise UpdateError("Релиз или файл не найден на GitHub (404).")
        raise UpdateError("GitHub ответил ошибкой %d." % e.code)
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise UpdateError("Нет связи с GitHub: %s" % type(reason).__name__)


def _read_limited(resp, limit: int) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("Ответ GitHub слишком большой")
    return data


# --------------------------------------------------------------------------- проверка версии
@dataclass
class Release:
    version: str
    tag: str
    notes: str
    page_url: str
    asset_url: str = ""
    sha_url: str = ""
    size: int = 0

    def public(self) -> dict:
        return {"version": self.version, "tag": self.tag, "notes": self.notes, "page_url": self.page_url,
                "size": self.size, "has_asset": bool(self.asset_url and self.sha_url)}


def fetch_latest(src: Source) -> Release:
    url = "%s/repos/%s/releases/latest" % (src.api_base.rstrip("/"), src.repo)
    with _get(url, src, 1_000_000, accept="application/vnd.github+json") as r:
        try:
            data = json.loads(_read_limited(r, 1_000_000).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise UpdateError("GitHub вернул непонятный ответ")
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise UpdateError("Подходящего релиза нет")
    tag = str(data.get("tag_name") or "")
    ver = ".".join(str(x) for x in parse_version(tag))
    page = str(data.get("html_url") or "")
    if not release_page_ok(page, src) and "https" in src.allowed_schemes:
        page = "https://github.com/%s/releases/tag/%s" % (src.repo, tag)
    rel = Release(version=ver, tag=tag, notes=str(data.get("body") or "")[:MAX_NOTES], page_url=page)
    by_name = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    a, s = by_name.get(src.asset_name), by_name.get(src.sha_name)
    if a and s:
        rel.asset_url = str(a.get("browser_download_url") or "")
        rel.sha_url = str(s.get("browser_download_url") or "")
        rel.size = int(a.get("size") or 0)
    return rel


# --------------------------------------------------------------------------- скачивание и проверка
def parse_sha256_file(text: str, asset_name: str) -> str:
    """Файл вида «<64 hex>  имя». Если строк несколько — берём строку нужного файла."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        m = HASH_RE.search(ln)
        if m and asset_name in ln:
            return m.group(1).lower()
    if len(lines) == 1:
        m = HASH_RE.fullmatch(lines[0].split()[0]) if lines[0].split() else None
        if m:
            return m.group(1).lower()
    raise UpdateError("В файле .sha256 нет контрольной суммы для %s" % asset_name)


def download_and_stage(rel: Release, stage_dir: Path, src: Source,
                       progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Скачивает архив, сверяет SHA-256 с .sha256 из релиза, достаёт AmneziaMonitor.exe.
    Возвращает путь к подготовленному exe. При любой ошибке ничего не остаётся в stage_dir."""
    if not (rel.asset_url and rel.sha_url):
        raise UpdateError("В релизе нет файлов для автоматической установки")
    stage_dir = Path(stage_dir)
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True, exist_ok=True)
    try:
        with _get(rel.sha_url, src, 4096) as r:
            expected = parse_sha256_file(_read_limited(r, 4096).decode("utf-8", "replace"), src.asset_name)
        part = stage_dir / "download.part"
        h = hashlib.sha256()
        done = 0
        with _get(rel.asset_url, src, MAX_ZIP) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or rel.size or 0)
            if total > MAX_ZIP:
                raise UpdateError("Файл обновления слишком большой")
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_ZIP:
                    raise UpdateError("Файл обновления слишком большой")
                h.update(chunk)
                f.write(chunk)
                if progress:
                    progress(done, total)
        if h.hexdigest() != expected:
            raise UpdateError("Контрольная сумма SHA-256 не совпала: файл повреждён или подменён. "
                              "Обновление отменено, ничего не изменено.")
        new_exe = _extract_exe(part, stage_dir, src.min_exe)
        part.unlink(missing_ok=True)
        (stage_dir / "staged.json").write_text(json.dumps(
            {"version": rel.version, "sha256": expected, "exe": new_exe.name}), encoding="utf-8")
        return new_exe
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _extract_exe(zip_path: Path, stage_dir: Path, min_size: int) -> Path:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise UpdateError("Архив обновления повреждён")
    with zf:
        cands = []
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            parts = [x for x in name.split("/") if x]
            if ".." in parts or name.startswith("/") or (parts and ":" in parts[0]):
                raise UpdateError("Небезопасный путь в архиве обновления")
            if len(parts) in (1, 2) and parts[-1] == EXE_NAME and not info.is_dir():
                cands.append(info)
        if len(cands) != 1:
            raise UpdateError("В архиве должен быть ровно один %s" % EXE_NAME)
        info = cands[0]
        if info.file_size > MAX_EXE or info.file_size < min_size:
            raise UpdateError("Размер %s в архиве неправдоподобен" % EXE_NAME)
        out = stage_dir / (EXE_NAME + ".new")
        n = 0
        with zf.open(info) as r, open(out, "wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                n += len(chunk)
                if n > MAX_EXE:
                    raise UpdateError("Файл в архиве слишком большой")
                f.write(chunk)
    with open(out, "rb") as f:
        if f.read(2) != b"MZ":
            raise UpdateError("Файл в архиве не похож на программу Windows")
    return out


# --------------------------------------------------------------------------- помощник подмены (после выхода)
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)         # SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x102   # WAIT_TIMEOUT: ещё работает
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                                    # зомби считаем завершённым
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def _file_free(path: Path) -> bool:
    """Windows не даёт открыть на запись exe, пока он запущен; на других системах всегда True."""
    try:
        with open(path, "r+b"):
            return True
    except OSError:
        return False


def clean_env() -> dict:
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"           # новый запуск onefile не должен брать папку родителя
    return env


def _detached_flags() -> int:
    if sys.platform.startswith("win"):
        return 0x00000008 | 0x00000200 | 0x08000000     # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    return 0


def _spawn(exe: Path, args: list) -> subprocess.Popen:
    kw = {"creationflags": _detached_flags()} if sys.platform.startswith("win") else {"start_new_session": True}
    return subprocess.Popen([str(exe)] + args, env=clean_env(), close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def write_result(data_dir: Path, ok: bool, version: str, message: str) -> None:
    try:
        p = Path(data_dir) / "update_result.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ok": ok, "version": version, "message": message, "time": time.time()},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def apply_update(target: Path, new_exe: Path, pids: list, data_dir: Path, version: str, *,
                 wait_timeout: float = 90, health_seconds: float = 20, selftest: bool = True,
                 restart_args: Optional[list] = None, spawn=None) -> bool:
    """Выполняется помощником уже после выхода программы. True — обновлено, False — оставлена старая версия."""
    target, new_exe, data_dir = Path(target), Path(new_exe), Path(data_dir)
    bak = target.with_name(target.name + ".bak")
    side = target.with_name(target.name + ".new")
    restart_args = restart_args or []
    spawn = spawn or _spawn

    def fail(msg: str, relaunch: bool = True) -> bool:
        log.error("Обновление не удалось: %s", msg)
        write_result(data_dir, False, version, msg)
        if relaunch and target.exists():
            try:
                spawn(target, restart_args)
            except OSError:
                log.exception("Не удалось запустить прежнюю версию")
        return False

    try:
        if not new_exe.is_file():
            return fail("Подготовленный файл обновления исчез")
        deadline = time.time() + wait_timeout
        while any(_pid_alive(p) for p in pids) or not _file_free(target):
            if time.time() > deadline:
                return fail("Программа не закрылась вовремя. Прежняя версия не изменена.", relaunch=False)
            time.sleep(0.3)
        if selftest:   # новый exe должен хотя бы запуститься здесь — до замены ничего не меняем
            probe = data_dir / "update_selftest.json"
            probe.unlink(missing_ok=True)
            try:
                rc = subprocess.run([str(new_exe), "--update-selftest", str(probe)], env=clean_env(), timeout=120,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=_detached_flags() if sys.platform.startswith("win") else 0).returncode
            except (OSError, subprocess.TimeoutExpired) as e:
                return fail("Новая версия не запустилась на этом ПК (%s). Прежняя версия не изменена." % type(e).__name__)
            try:
                ok = rc == 0 and json.loads(probe.read_text(encoding="utf-8")).get("version") == version
            except (OSError, ValueError):
                ok = False
            probe.unlink(missing_ok=True)
            if not ok:
                return fail("Проверка новой версии не прошла (код %s). Прежняя версия не изменена." % rc)
        try:
            shutil.copyfile(new_exe, side)
            if bak.exists():
                bak.unlink()
            os.replace(target, bak)
        except OSError as e:
            side.unlink(missing_ok=True)
            return fail("Нет права записи в папку программы (%s). Установите вручную или перенесите программу "
                        "в папку пользователя." % type(e).__name__)
        try:
            os.replace(side, target)
        except OSError as e:
            os.replace(bak, target)
            side.unlink(missing_ok=True)
            return fail("Не удалось поставить новый файл (%s). Возвращена прежняя версия." % type(e).__name__)
        try:
            proc = spawn(target, restart_args)
        except OSError as e:
            return _rollback(target, bak, data_dir, version, restart_args, spawn,
                             "Новая версия не запустилась (%s)" % type(e).__name__)
        try:
            rc = proc.wait(timeout=health_seconds)
        except subprocess.TimeoutExpired:
            rc = None                                       # работает — считаем успехом
        if rc not in (None, 0):
            return _rollback(target, bak, data_dir, version, restart_args, spawn,
                             "Новая версия завершилась с ошибкой (код %s)" % rc)
        write_result(data_dir, True, version, "Обновлено до версии %s. Прежний файл сохранён как %s." % (version, bak.name))
        shutil.rmtree(data_dir / "update_stage", ignore_errors=True)
        return True
    except Exception as e:  # pragma: no cover
        log.exception("apply_update")
        return fail("Внутренняя ошибка обновления: %s" % type(e).__name__)


def _rollback(target, bak, data_dir, version, restart_args, spawn, why) -> bool:
    log.error("%s — возвращаю прежнюю версию", why)
    try:
        if target.exists():
            target.unlink()
        os.replace(bak, target)
        spawn(target, restart_args)
        write_result(data_dir, False, version, why + ". Возвращена прежняя версия.")
    except OSError as e:
        write_result(data_dir, False, version, why + ". Не удалось вернуть прежнюю версию автоматически (%s): "
                     "верните файл %s вручную." % (type(e).__name__, bak.name))
    return False


def launch_helper(target: Path, new_exe: Path, data_dir: Path, version: str, restart_args=None) -> None:
    """Запускает копию exe как помощника подмены. Вызывающий после этого должен завершить программу."""
    helper_dir = Path(tempfile.mkdtemp(prefix="AmneziaMonitor-update-"))
    helper = helper_dir / "AmneziaMonitor-updater.exe"
    shutil.copyfile(target, helper)
    args = ["--apply-update", "--target", str(target), "--new", str(new_exe), "--data-dir", str(data_dir),
            "--version", version, "--pid", str(os.getpid())]
    for a in restart_args or []:
        args += ["--restart-arg", a]
    _spawn(helper, args)


def clean_old_helpers(max_age: float = 86400) -> None:
    base = Path(tempfile.gettempdir())
    try:
        for d in base.glob("AmneziaMonitor-update-*"):
            if d.is_dir() and time.time() - d.stat().st_mtime > max_age:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def helper_main(argv: list) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="AmneziaMonitor --apply-update")
    p.add_argument("--apply-update", action="store_true")
    p.add_argument("--target", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--pid", type=int, action="append", default=[])
    p.add_argument("--restart-arg", action="append", default=[])
    a = p.parse_args(argv)
    ddir = Path(a.data_dir)
    try:
        ddir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(ddir / "update.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
        logging.getLogger().setLevel(logging.INFO)
    except OSError:
        pass
    ok = apply_update(Path(a.target), Path(a.new), a.pid, ddir, a.version, restart_args=a.restart_arg)
    return 0 if ok else 1


# --------------------------------------------------------------------------- менеджер для панели
def can_install() -> bool:
    """Автоустановка — только в собранном Windows-exe; из исходников/Linux/macOS — уведомление и страница релиза."""
    return bool(getattr(sys, "frozen", False)) and sys.platform.startswith("win")


class UpdateManager:
    def __init__(self, data_dir, *, src: Optional[Source] = None, current: str = __version__,
                 installable: Optional[bool] = None, target: Optional[Path] = None,
                 launcher: Optional[Callable[[Path, Path, str], None]] = None,
                 on_exit: Optional[Callable[[], None]] = None):
        self.data_dir = Path(data_dir)
        self.src = src or Source()
        self.current = current
        self.installable = can_install() if installable is None else installable
        self.target = Path(target) if target else Path(sys.executable)
        self.launcher = launcher or (lambda tgt, new, ver: launch_helper(tgt, new, self.data_dir, ver))
        self.on_exit = on_exit
        self.lock = threading.Lock()
        self.state_path = self.data_dir / "update.json"
        self.stage = self.data_dir / "update_stage"
        self.release: Optional[Release] = None
        self.error = ""
        self.checked_at = 0.0
        self.phase = "idle"              # idle | checking | downloading | ready | error
        self.progress = (0, 0)
        self.staged: Optional[Path] = None
        self._cfg = self._load()

    # --- сохранённые настройки
    def _load(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cfg, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            log.warning("Не удалось сохранить настройки обновления")

    @property
    def auto_check(self) -> bool:
        return bool(self._cfg.get("auto_check", True))

    def set_auto_check(self, on: bool) -> None:
        with self.lock:
            self._cfg["auto_check"] = bool(on)
            self._save()

    def skip(self, version: str) -> None:
        parse_version(version)
        with self.lock:
            self._cfg["skipped"] = version
            self._save()

    def unskip(self) -> None:
        with self.lock:
            self._cfg.pop("skipped", None)
            self._save()

    # --- проверка
    def check(self, manual: bool = False) -> dict:
        with self.lock:
            if self.phase in ("checking", "downloading"):
                return self._status()
            self.phase, self.error = "checking", ""
        try:
            rel = fetch_latest(self.src)
            with self.lock:
                self.release, self.checked_at = rel, time.time()
                self.phase = "idle"
                self._cfg["last_check"] = self.checked_at
                self._save()
        except UpdateError as e:
            with self.lock:
                self.phase, self.error = "error" if manual else "idle", str(e)
                if not manual:
                    log.info("Проверка обновлений не удалась: %s", e)
        return self.status()

    def auto_check_due(self) -> bool:
        return self.auto_check and time.time() - float(self._cfg.get("last_check") or 0) > 60

    def _status(self) -> dict:
        rel = self.release
        newer = bool(rel and is_newer(rel.version, self.current))
        skipped = bool(rel and self._cfg.get("skipped") == rel.version)
        res = None
        rp = self.data_dir / "update_result.json"
        try:
            res = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return {
            "current": self.current, "auto_check": self.auto_check, "can_install": self.installable,
            "available": newer, "skipped": skipped and newer, "release": rel.public() if rel else None,
            "checked_at": self.checked_at, "phase": self.phase, "error": self.error,
            "progress": {"done": self.progress[0], "total": self.progress[1]}, "last_result": res,
        }

    def status(self) -> dict:
        with self.lock:
            return self._status()

    def ack_result(self) -> None:
        (self.data_dir / "update_result.json").unlink(missing_ok=True)

    # --- установка (только по нажатию)
    def start_download(self) -> dict:
        with self.lock:
            rel = self.release
            if not rel or not is_newer(rel.version, self.current):
                raise UpdateError("Новой версии нет")
            if not self.installable:
                raise UpdateError("Автоустановка работает только в Windows-версии (exe). Откройте страницу релиза.")
            if self.phase in ("downloading", "checking"):
                return self._status()
            self.phase, self.error, self.progress = "downloading", "", (0, 0)
        threading.Thread(target=self._download, args=(rel,), name="update-dl", daemon=True).start()
        return self.status()

    def _download(self, rel: Release) -> None:
        def prog(done, total):
            self.progress = (done, total)
        try:
            staged = download_and_stage(rel, self.stage, self.src, prog)
            with self.lock:
                self.staged, self.phase = staged, "ready"
        except UpdateError as e:
            with self.lock:
                self.phase, self.error, self.staged = "error", str(e), None
        except Exception as e:  # pragma: no cover
            log.exception("update download")
            with self.lock:
                self.phase, self.error = "error", "Не удалось скачать обновление: " + type(e).__name__

    def restart_now(self) -> dict:
        with self.lock:
            if self.phase != "ready" or not self.staged or not self.release:
                raise UpdateError("Обновление ещё не готово")
            staged, ver = self.staged, self.release.version
        self.launcher(self.target, staged, ver)
        if self.on_exit:
            threading.Timer(0.5, self.on_exit).start()
        return {"ok": True}


def selftest_main(out_path: str) -> int:
    """--update-selftest <файл>: новый exe записывает свою версию и выходит (проверка перед подменой)."""
    try:
        import amnezia_monitor, app_settings, dashboard, hostkeys, blockcheck  # noqa: F401  (всё ли упаковано)
        Path(out_path).write_text(json.dumps({"version": __version__, "ok": True}), encoding="utf-8")
        return 0
    except Exception:
        return 2
