# -*- coding: utf-8 -*-
"""Окно приложения Windows: встроенный Edge WebView2, без внешнего браузера.

Панель по-прежнему открывается только как http://127.0.0.1 (сервер поднимает app_main).
Если среды WebView2 нет, показывается понятное сообщение и браузер сам не открывается.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger("amnezia_monitor.window")

WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
MISSING_RUNTIME_TEXT = (
    "Не найден Microsoft Edge WebView2 Runtime, поэтому окно Amnezia Monitor не открыто.\n\n"
    "Установите бесплатный компонент «WebView2 Runtime» с сайта Microsoft и запустите программу снова:\n"
    "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
    "Внешний браузер не открывается. Если значок программы есть у часов, мониторинг продолжается; "
    "остановить его можно пунктом «Выход»."
)


def wants_desktop_window(no_browser: bool, platform: str | None = None) -> bool:
    """На Windows окно — обычный запуск. --no-browser оставляет прежний фоновый режим."""
    plat = sys.platform if platform is None else platform
    return plat.startswith("win") and not no_browser


def webview2_runtime_installed() -> bool:
    """Есть ли Evergreen WebView2 (не старый IE). На других ОС всегда False."""
    if not sys.platform.startswith("win"):
        return False
    import winreg

    roots = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
    )
    for hive, base in roots:
        try:
            with winreg.OpenKey(hive, base + "\\" + WEBVIEW2_CLIENT) as key:
                build, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        parts = str(build).split(".")
        try:
            nums = [int(x) for x in parts[:4]]
        except ValueError:
            continue
        while len(nums) < 4:
            nums.append(0)
        if tuple(nums) >= (86, 0, 622, 0) and tuple(nums) != (0, 0, 0, 0):
            return True
    return False


def default_message_box(text: str) -> None:
    log.error("%s", text.replace("\n", " "))
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Amnezia Monitor", 0x00000010)
    except Exception:
        log.exception("Не удалось показать сообщение об ошибке")


class DesktopWindow:
    """Одно окно панели. Закрытие крестиком прячет окно, если трей жив; «Выход» закрывает всё."""

    def __init__(self, data_dir: Path, presenter=None):
        self.data_dir = Path(data_dir)
        self.presenter = presenter or default_message_box
        self.quitting = False
        self.tray_running = False
        self.window = None
        self.failure = None

    def explain_missing_runtime(self) -> None:
        self.failure = "webview2-missing"
        self.presenter(MISSING_RUNTIME_TEXT)

    def on_closing(self):
        """False отменяет закрытие. Вызывается из потока окна, поэтому hide — следом, не сразу."""
        if self.quitting or not self.tray_running:
            return None

        def later_hide():
            if not self.quitting:
                self.show_or_hide(False)

        threading.Timer(0.05, later_hide).start()
        return False

    def show_or_hide(self, show: bool) -> None:
        window = self.window
        if window is None:
            return
        try:
            if show:
                window.show()
                window.restore()
            else:
                window.hide()
        except Exception:
            log.exception("Не удалось %s окно", "показать" if show else "скрыть")

    def show(self) -> None:
        if self.failure:
            self.explain_missing_runtime()
            return
        self.show_or_hide(True)

    def destroy(self) -> None:
        self.quitting = True
        window = self.window
        if window is None:
            return
        try:
            window.destroy()
        except Exception:
            log.exception("Не удалось закрыть окно")

    def watch_show_requests(self, lock, stop: threading.Event) -> None:
        def loop():
            while not stop.is_set():
                try:
                    if lock.consume_show_request():
                        self.show()
                except Exception:
                    log.exception("Запрос показать окно")
                stop.wait(0.4)

        threading.Thread(target=loop, name="show-request", daemon=True).start()

    def run(self, url: str, on_ready=None) -> bool:
        """Блокирует поток, пока окно не закрыто по-настоящему. False — окно не создано."""
        log.warning("Проверка WebView2 перед окном: %s", "есть" if webview2_runtime_installed() else "нет")
        if not webview2_runtime_installed():
            self.explain_missing_runtime()
            return False
        try:
            import webview
        except Exception as e:
            self.failure = "webview-import"
            self.presenter(
                "Не удалось загрузить окно программы (%s).\n"
                "Внешний браузер не открывается." % type(e).__name__
            )
            log.exception("import webview")
            return False

        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
        webview.settings["SHOW_DEFAULT_MENUS"] = False
        self.window = webview.create_window(
            "Amnezia Monitor",
            url,
            width=1200,
            height=800,
            min_size=(960, 640),
            background_color="#0b1020",
            text_select=True,
            confirm_close=False,
        )
        self.window.events.closing += self.on_closing
        if on_ready is not None:
            def ready():
                try:
                    on_ready()
                except Exception:
                    log.exception("Не удалось запустить трей после окна")

            self.window.events.shown += ready

        def guard():
            renderer = getattr(webview, "renderer", None)
            log.warning("Окно: renderer=%s", renderer)
            if renderer and renderer != "edgechromium":
                self.failure = "renderer-" + str(renderer)
                log.error("Ожидался Edge WebView2, получен %s. Окно закрывается.", renderer)
                self.quitting = True
                self.destroy()

        try:
            webview.start(
                guard,
                gui="edgechromium",
                private_mode=True,
                storage_path=str(self.data_dir / "webview"),
                http_server=False,
            )
        except Exception as e:
            self.failure = "webview-start"
            self.presenter(
                "Окно Amnezia Monitor не запустилось (%s).\nВнешний браузер не открывается."
                % type(e).__name__
            )
            log.exception("webview.start")
            self.window = None
            return False
        if self.failure and str(self.failure).startswith("renderer-"):
            self.presenter(
                "Окно открылось не на Edge WebView2 (%s), поэтому оно закрыто.\n"
                "Установите WebView2 Runtime. Внешний браузер не открывается." % self.failure
            )
            return False
        return True
