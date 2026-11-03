# -*- coding: utf-8 -*-
"""Amnezia Monitor — режим «приложения» для Windows (и для Linux тоже работает).

Двойной щелчок по AmneziaMonitor.exe:
  * данные и настройки — в %APPDATA%\\AmneziaMonitor\\ (servers.yaml, amnezia_monitor.db, журнал);
  * в фоне опрашиваются серверы (только чтение), веб-панель — на http://127.0.0.1:<порт>/;
  * открывается браузер; при первом запуске (серверов нет) — сразу страница «Настройки»;
  * значок в трее (Открыть панель / Папка с данными / Выход) или консольное окно «закройте, чтобы остановить»;
  * повторный запуск не создаёт второй экземпляр, а просто открывает браузер.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import amnezia_monitor as am   # noqa: E402
import app_settings            # noqa: E402
import dashboard               # noqa: E402

log = logging.getLogger("amnezia_monitor.app")
APP_NAME = "AmneziaMonitor"
DEFAULT_PORT = 8765


# --------------------------------------------------------------------------- пути и журнал
def default_data_dir() -> Path:
    if os.environ.get("AMNEZIA_APP_DIR"):
        return Path(os.environ["AMNEZIA_APP_DIR"])
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "amnezia-monitor"


def setup_logging(data_dir: Path, verbose: bool, console: bool):
    root = logging.getLogger()
    root.setLevel(logging.INFO if verbose else logging.WARNING)
    fh = logging.handlers.RotatingFileHandler(data_dir / "amnezia-monitor.log", maxBytes=1_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    if console and sys.stderr is not None:       # в .exe без консоли sys.stderr = None
        root.addHandler(logging.StreamHandler())
    logging.getLogger("paramiko").setLevel(logging.WARNING)


def say(text: str):
    if sys.stdout is not None:
        try:
            print(text, flush=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- один экземпляр
class InstanceLock:
    """Файловая блокировка в папке данных: второй запуск видит, что программа уже работает."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "app.lock"
        self.port_file = data_dir / "app.port"
        self.fh = None

    def acquire(self) -> bool:
        self.fh = open(self.path, "a+")
        try:
            if sys.platform.startswith("win"):
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def write_port(self, port: int):
        self.port_file.write_text(str(port), encoding="ascii")

    def running_url(self):
        try:
            port = int(self.port_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None
        url = f"http://127.0.0.1:{port}/"
        try:
            with urllib.request.urlopen(url + "api/health", timeout=3) as r:
                if json.loads(r.read()).get("app") == "amnezia-monitor":
                    return url
        except Exception:
            return None
        return None

    def release(self):
        if self.fh:
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None


# --------------------------------------------------------------------------- конфигурация
class ConfigHolder:
    """Держит актуальный Config; перечитывает servers.yaml после сохранения настроек."""

    def __init__(self, path: Path, db_path: Path):
        self.path, self.db_path = path, db_path
        self.lock = threading.Lock()
        self.cfg = am.Config(servers=[], db_path=str(db_path))
        self.error = None
        self.mtime = None
        self.reload(force=True)

    def reload(self, force=False):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self.mtime:
            return
        try:
            cfg = am.load_config(str(self.path), allow_empty=True)
            cfg.db_path = str(self.db_path)          # база всегда в папке данных
            with self.lock:
                self.cfg, self.error, self.mtime = cfg, None, mtime
        except am.ConfigError as e:
            self.error, self.mtime = str(e), mtime
            log.error("Ошибка конфигурации: %s", e)

    def get(self) -> am.Config:
        with self.lock:
            return self.cfg


def ensure_config(path: Path):
    if not path.exists():
        app_settings.write_raw(path, {"interval": 60, "servers": []})


# --------------------------------------------------------------------------- цикл опроса
def monitor_loop(holder: ConfigHolder, stop: threading.Event, wake: threading.Event):
    store = am.Store(str(holder.db_path))
    while not stop.is_set():
        holder.reload()
        cfg = holder.get()
        t0 = time.time()
        if cfg.servers:
            try:
                am.do_check(cfg, store, am.Notifier(cfg))
                am.maybe_daily(cfg, store, am.Notifier(cfg))
            except Exception as e:
                log.exception("Ошибка цикла опроса: %s", e)
        wake.wait(max(1.0, cfg.interval - (time.time() - t0)))
        wake.clear()
    store.db.close()


# --------------------------------------------------------------------------- трей
def make_icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in range(64):   # градиент индиго -> бирюзовый
        r, g, b = int(99 + (34 - 99) * i / 63), int(102 + (211 - 102) * i / 63), int(241 + (238 - 241) * i / 63)
        d.line([(i, 0), (0, i)], fill=(r, g, b, 255))
        d.line([(63, i), (i, 63)], fill=(r, g, b, 255))
    mask = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, 63, 63], 14, fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    d.polygon([(32, 12), (50, 52), (41, 52), (32, 30), (23, 52), (14, 52)], fill=(255, 255, 255, 255))
    d.rectangle([24, 40, 40, 45], fill=(255, 255, 255, 255))
    return img


def open_folder(path: Path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        log.error("Не удалось открыть папку: %s", e)


def run_tray(url: str, data_dir: Path, quit_cb, stop: threading.Event) -> bool:
    """Значок в трее. Возвращает False, если pystray недоступен (тогда — консольный режим)."""
    try:
        import pystray
    except Exception as e:
        log.warning("Значок в трее недоступен (%s) — работаю в консольном режиме", type(e).__name__)
        return False
    icon = pystray.Icon(APP_NAME, make_icon_image(), "Amnezia Monitor — мониторинг VPN", menu=pystray.Menu(
        pystray.MenuItem("Открыть панель", lambda: webbrowser.open(url), default=True),
        pystray.MenuItem("Папка с данными", lambda: open_folder(data_dir)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", lambda: quit_cb()),
    ))
    threading.Thread(target=lambda: (stop.wait(), icon.stop()), daemon=True).start()
    try:
        icon.run()
    except Exception as e:
        log.warning("Трей не запустился (%s) — консольный режим", type(e).__name__)
        return False
    return True


# --------------------------------------------------------------------------- запуск
def bind_server(make, preferred: int):
    for port in (preferred, 0):
        try:
            return make(port)
        except OSError:
            if port == 0:
                raise
            log.warning("Порт %s занят — выбираю свободный", preferred)


class PanelServer:
    """HTTP-панель. По умолчанию только 127.0.0.1. Если включён доступ с телефона — ещё и LAN.

    0.0.0.0 уже принимает соединения на 127.0.0.1. Если выбран конкретный IP сети, рядом
    поднимается второй сокет на 127.0.0.1 с тем же портом, чтобы окно настроек не пропадало.
    """

    def __init__(self, make, preferred_port: int):
        self._make = make
        self.preferred_port = preferred_port
        self._lock = threading.Lock()
        self._httpd = []
        self.host = "127.0.0.1"
        self.token = None
        self.port = None

    def start(self, host: str, token):
        with self._lock:
            self._open(host, token, self.preferred_port)
            return self.port

    def update(self, host: str, token):
        with self._lock:
            if self._httpd and host == self.host and token == self.token:
                return self.port
            port = self.port or self.preferred_port
            prev_host, prev_token = self.host, self.token
            self._close()
            try:
                self._open(host, token, port)
            except OSError as e:
                log.error("Не удалось слушать %s (%s) — возвращаю %s", host, type(e).__name__, prev_host)
                self._open(prev_host, prev_token, port or 0)
            return self.port

    def stop(self):
        with self._lock:
            self._close()

    def _open(self, host, token, port):
        primary = self._bind(host, port, token)
        self._httpd = [primary]
        self.host, self.token = host, token
        self.port = primary.server_address[1]
        if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
            try:
                self._httpd.append(self._bind("127.0.0.1", self.port, token))
            except OSError as e:
                log.warning("127.0.0.1:%s недоступен (%s). Панель только на %s.", self.port, type(e).__name__, host)
        for httpd in self._httpd:
            threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
        if token and host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("Панель слушает %s:%s — с других устройств нужен токен из настроек (пароли SSH не отдаются)",
                        host, self.port)
        else:
            log.warning("Панель: http://127.0.0.1:%s/", self.port)

    def _bind(self, host, port, token):
        try:
            return self._make(host, port, token)
        except OSError:
            if not port:
                raise
            log.warning("Порт %s занят — выбираю свободный", port)
            return self._make(host, 0, token)

    def _close(self):
        old = list(self._httpd)
        self._httpd = []
        for httpd in old:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass


def add_args(p: argparse.ArgumentParser):
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="порт панели (если занят — любой свободный)")
    p.add_argument("--data-dir", help="папка данных (по умолчанию %%APPDATA%%\\AmneziaMonitor)")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    p.add_argument("--no-tray", action="store_true", help="без значка в трее (консольный режим)")
    p.add_argument("--exit-after", type=float, help=argparse.SUPPRESS)   # для тестов/проверки
    p.add_argument("--verbose", "-v", action="store_true")


def run(args) -> int:
    demo = bool(getattr(args, "demo", False))
    if demo:
        data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="amnezia-app-demo-"))
    else:
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir, args.verbose, console=args.no_tray)

    lock = InstanceLock(data_dir)
    if not lock.acquire():
        url = lock.running_url()
        if url:
            say(f"Amnezia Monitor уже запущен: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        say("Похоже, программа уже запущена, но не отвечает. Попробуйте ещё раз через минуту.")
        return 1

    config_path = data_dir / "servers.yaml"
    db_path = data_dir / "amnezia_monitor.db"
    if demo:
        import demo_data
        demo_data.demo_store(str(db_path))
        if not config_path.exists():
            app_settings.write_raw(config_path, {"interval": 60, "servers": [
                dict({"name": s.name, "host": s.host, "port": s.port, "user": s.user,
                      "notes": "демо-сервер"}, **app_settings._secret_fields(s.password, "password", "password_enc"))
                for s in demo_data.DEMO_SERVERS]})
    else:
        ensure_config(config_path)
        am.Store(str(db_path)).db.close()
    holder = ConfigHolder(config_path, db_path)

    stop, wake = threading.Event(), threading.Event()
    quitting = {"done": False}

    ctx = app_settings.SettingsContext(config_path, secrets.token_urlsafe(32), str(data_dir),
                                       on_quit=None)
    panel = PanelServer(lambda host, port, token: dashboard.make_server(
        lambda: am.Store(str(db_path)), holder.get, host, port, demo, ctx, read_token=token), args.port)

    def quit_app():
        if quitting["done"]:
            return
        quitting["done"] = True
        log.info("Остановка по запросу пользователя")
        stop.set()
        wake.set()
        # shutdown из потока запроса настроек заблокировал бы сам запрос — закрываем отдельно
        threading.Thread(target=panel.stop, daemon=True).start()

    def apply_listen():
        if stop.is_set():
            return
        try:
            host, token = app_settings.listen_params(app_settings.read_raw(config_path))
            panel.update(host, token)
            ctx.listen_port = panel.port
            lock.write_port(panel.port)
        except Exception as e:
            log.error("Не удалось применить адрес панели: %s", type(e).__name__)

    def on_saved():
        holder.reload(force=True)
        wake.set()                                   # опрос сразу с новыми настройками
        timer = threading.Timer(0.35, apply_listen)
        timer.daemon = True
        timer.start()

    ctx.on_saved = on_saved
    ctx.on_quit = quit_app
    host, token = app_settings.listen_params(app_settings.read_raw(config_path))
    port = panel.start(host, token)
    ctx.listen_port = port
    lock.write_port(port)
    url = f"http://127.0.0.1:{port}/"
    if not demo:
        threading.Thread(target=monitor_loop, args=(holder, stop, wake), name="monitor", daemon=True).start()
    log.warning("Amnezia Monitor запущен: %s (данные: %s)", url, data_dir)
    open_url = url + ("#settings" if not holder.get().servers else "")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(open_url)).start()
    if args.exit_after:
        threading.Timer(args.exit_after, quit_app).start()

    tray_ok = False
    if not args.no_tray:
        tray_ok = run_tray(url, data_dir, quit_app, stop)
    if not tray_ok:
        say("=" * 64)
        say(" Amnezia Monitor работает" + (" (ДЕМО-данные)" if demo else ""))
        say(f" Панель:  {url}")
        say(f" Данные:  {data_dir}")
        say(" Чтобы остановить — закройте это окно или нажмите Ctrl+C.")
        say("=" * 64)
        try:
            while not stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
    quit_app()
    panel.stop()
    lock.release()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="AmneziaMonitor", description="Amnezia Monitor: мониторинг + веб-панель")
    p.add_argument("--demo", action="store_true", help="демо-данные, без подключения к серверам")
    add_args(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
