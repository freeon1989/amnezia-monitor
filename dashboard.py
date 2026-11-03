# -*- coding: utf-8 -*-
"""Веб-панель: http.server из стандартной библиотеки, одна HTML-страница без внешних зависимостей.
Данные — только чтение SQLite-истории. По умолчанию слушает 127.0.0.1 (доступ только с этого компьютера).

Страница «Настройки» (режим приложения) включается, если передан SettingsContext:
  * запросы принимаются только с loopback-адреса;
  * заголовок Host должен быть 127.0.0.1/localhost (защита от DNS-rebinding);
  * Origin (если есть) — только эта же страница;
  * каждый запрос несёт X-Amnezia-Token — случайный токен сессии, встроенный в страницу (CSRF);
  * пароли и токен Telegram в ответах не возвращаются никогда.

Доступ с телефона (read_token): панель может слушать сеть, но /api/servers, /api/server и /api/health
с не-loopback адреса требуют заголовок X-Amnezia-Token (или Authorization: Bearer) — отдельный токен
чтения, не токен сессии настроек. Страница настроек с другого компьютера не открывается.
Для нативного приложения CORS не нужен; заголовки CORS добавляются только вместе с read_token,
чтобы браузер с другого origin мог прислать этот заголовок.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import dashboard_api as api

log = logging.getLogger("amnezia_monitor.dashboard")
# В собранном .exe (PyInstaller) файлы данных лежат в sys._MEIPASS
_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
HTML_PATH = _BASE / "dashboard.html"
MAX_BODY = 64 * 1024
LOCAL_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")
LOOPBACK_BINDS = ("127.0.0.1", "localhost", "::1")
READ_API_PATHS = ("/api/health", "/api/servers", "/api/server")


def client_may_read(client_ip: str, presented: str, read_token: str = None) -> bool:
    """Чтение JSON с другого компьютера — только с токеном. Loopback остаётся открытым,
    чтобы локальная панель и проверка «программа уже запущена» работали как раньше."""
    if not read_token:
        return True
    if _is_loopback(client_ip):
        return True
    presented = presented or ""
    try:
        return hmac.compare_digest(presented.encode("utf-8"), str(read_token).encode("utf-8"))
    except Exception:
        return False


def presented_token(headers) -> str:
    tok = (headers.get("X-Amnezia-Token") or "").strip()
    if tok:
        return tok
    auth = headers.get("Authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def render_html(snapshot: dict = None, demo: bool = False, settings_token: str = None,
                first_run: bool = False) -> str:
    html = HTML_PATH.read_text(encoding="utf-8")
    meta = {"demo": demo}
    if settings_token:
        meta.update({"settings": True, "token": settings_token, "first_run": first_run})
    inject = f"<script>window.__META__ = {json.dumps(meta)};</script>"
    if snapshot is not None:   # статическая версия: данные встраиваются в страницу
        data = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
        inject += f"<script>window.__DATA__ = {data};</script>"
    return html.replace("<!--STATIC_DATA-->", inject)


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%")[0]).is_loopback
    except ValueError:
        return False


def make_handler(store_factory, cfg, demo=False, settings=None, read_token=None):
    """cfg — Config или функция, возвращающая актуальный Config (перечитывается после сохранения)."""
    get_cfg = cfg if callable(cfg) else (lambda: cfg)

    class Handler(BaseHTTPRequestHandler):
        server_version = "AmneziaMonitor"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code, body: bytes, ctype: str, cors: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if cors:
                # Только вместе с токеном чтения. Нативному клиенту заголовок не нужен.
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "X-Amnezia-Token, Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                                 "form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200, cors: bool = False):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", cors=cors)

        def _read_allowed(self) -> bool:
            if client_may_read(self.client_address[0], presented_token(self.headers), read_token):
                return True
            self._json({"error": "bad token"}, 403, cors=bool(read_token))
            return False

        # ---- защита API настроек -------------------------------------------------------------
        def _local_request_ok(self) -> bool:
            if not _is_loopback(self.client_address[0]):
                return False
            host = (self.headers.get("Host") or "").strip().lower()
            hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            if hostname not in LOCAL_HOSTNAMES:
                return False
            origin = self.headers.get("Origin")
            if origin and origin.lower() not in (f"http://{host}",):
                return False
            return True

        def _settings_guard(self) -> bool:
            if settings is None:
                self._json({"error": "settings disabled"}, 404)
                return False
            if not self._local_request_ok():
                self._json({"error": "forbidden"}, 403)
                return False
            tok = self.headers.get("X-Amnezia-Token") or ""
            if not hmac.compare_digest(tok.encode(), settings.token.encode()):
                self._json({"error": "bad token"}, 403)
                return False
            return True

        def _read_json(self):
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                raise ValueError("нужен Content-Type: application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                raise ValueError("пустой или слишком большой запрос")
            obj = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("ожидался JSON-объект")
            return obj

        # ---- маршруты ----------------------------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    first = bool(settings) and not get_cfg().servers
                    page = render_html(demo=demo, settings_token=settings.token if settings and
                                       self._local_request_ok() else None, first_run=first)
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                if u.path == "/api/settings":
                    if self._settings_guard():
                        self._json(settings.get())
                    return
                if u.path in READ_API_PATHS:
                    if not self._read_allowed():
                        return
                    cors = bool(read_token)
                    if u.path == "/api/health":
                        return self._json({"ok": True, "time": time.time(), "app": "amnezia-monitor"}, cors=cors)
                    cfg_now = get_cfg()
                    store = store_factory()
                    try:
                        if u.path == "/api/servers":
                            return self._json(api.api_servers(store, cfg_now), cors=cors)
                        name = (q.get("name") or [""])[0]
                        if name not in api.server_names(store, cfg_now):
                            return self._json({"error": "unknown server"}, 404, cors=cors)
                        hours = min(168.0, max(1.0, float((q.get("hours") or ["24"])[0])))
                        return self._json(api.api_server(store, cfg_now, name, hours=hours), cors=cors)
                    finally:
                        store.db.close()
                self._json({"error": "not found"}, 404)
            except Exception as e:  # pragma: no cover
                log.exception("API error")
                self._json({"error": type(e).__name__}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path not in ("/api/settings", "/api/settings/test", "/api/app/quit"):
                return self._json({"error": "not found"}, 404)
            if not self._settings_guard():
                return
            try:
                payload = self._read_json()
            except Exception as e:
                return self._json({"error": str(e)}, 400)
            try:
                if u.path == "/api/settings":
                    return self._json(settings.save(payload))
                if u.path == "/api/settings/test":
                    return self._json(settings.test(payload))
                if u.path == "/api/app/quit":
                    if not settings.on_quit:
                        return self._json({"error": "not available"}, 404)
                    self._json({"ok": True})
                    threading.Timer(0.3, settings.on_quit).start()
                    return
            except ValueError as e:          # SettingsError — понятный текст для пользователя
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                log.error("Ошибка настроек: %s", type(e).__name__)
                return self._json({"error": "внутренняя ошибка: " + type(e).__name__}, 500)

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def do_OPTIONS(self):
            u = urlparse(self.path)
            if read_token and u.path in READ_API_PATHS:
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "X-Amnezia-Token, Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
    return Handler


def make_server(store_factory, cfg, host="127.0.0.1", port=8765, demo=False, settings=None, read_token=None):
    if read_token is not None:
        read_token = str(read_token).strip() or None
    # Настройки остаются только для loopback (проверка внутри обработчика). Слушать сеть можно
    # лишь с токеном чтения, чтобы имена клиентов и DNS не были открыты всей LAN без ключа.
    if settings is not None and host not in LOOPBACK_BINDS and not read_token:
        raise ValueError("Доступ к панели из сети вместе со страницей настроек требует токен чтения "
                         "(Настройки → Телефон или --api-token)")

    class _Server(ThreadingHTTPServer):
        allow_reuse_address = True  # чтобы смена адреса (127.0.0.1 ↔ сеть) не ждала TIME_WAIT

    return _Server((host, port), make_handler(store_factory, cfg, demo, settings, read_token))


def serve(store_factory, cfg, host="127.0.0.1", port=8765, background_task=None, demo=False, api_token=None):
    token = (str(api_token).strip() if api_token else "") or None
    if host not in LOOPBACK_BINDS:
        if token:
            print("Доступ из сети: с других компьютеров /api/servers, /api/server и /api/health "
                  "требуют заголовок X-Amnezia-Token. Пароли SSH в API не передаются.")
        else:
            print("ВНИМАНИЕ: панель доступна из сети и не защищена паролем. Открывайте её только в доверенной сети.")
            print("Чтобы закрыть чтение токеном, задайте --api-token или переменную AMNEZIA_API_TOKEN.")
    httpd = make_server(store_factory, cfg, host, port, demo, read_token=token)
    if background_task:
        threading.Thread(target=background_task, daemon=True).start()
    print(f"Панель: http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/  (Ctrl+C — выход)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
