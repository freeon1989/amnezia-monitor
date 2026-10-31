# -*- coding: utf-8 -*-
"""Настройки для страницы «Настройки»: чтение/запись servers.yaml, проверка подключения.

Пароли и токен никогда не возвращаются в браузер: наружу уходит только признак «сохранён».
Пустое поле пароля при редактировании = оставить прежний пароль.
"""
from __future__ import annotations

import os
import re
import socket
import tempfile
import time
from pathlib import Path

import yaml

import secrets_store

NAME_RE = re.compile(r"^[\w.\- ]{1,40}$", re.UNICODE)
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
USER_RE = re.compile(r"^[A-Za-z0-9._\-]{1,32}$")
MIN_INTERVAL, MAX_INTERVAL = 15, 3600

HEADER = ("# Файл создан программой Amnezia Monitor (страница «Настройки»).\n"
          "# Пароли и токен: {how}.\n"
          "# Не пересылайте этот файл и не выкладывайте его в общий доступ.\n")


class SettingsError(ValueError):
    pass


def read_raw(path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {"servers": []}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"servers": []}


def write_raw(path, raw: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    how = ("зашифрованы DPAPI (расшифровать может только этот пользователь Windows на этом ПК)"
           if secrets_store.IS_WINDOWS else "хранятся открытым текстом, файл доступен только владельцу (chmod 600)")
    text = HEADER.format(how=how) + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".servers-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        secrets_store.restrict_file(tmp)
        os.replace(tmp, p)                      # атомарная замена
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    secrets_store.restrict_file(p)


def _has_password(s: dict) -> bool:
    return bool(s.get("password") or s.get("password_enc") or s.get("password_env"))


def public_view(raw: dict) -> dict:
    """То, что можно отдать в браузер: без паролей и токенов."""
    tg = raw.get("telegram") or {}
    servers = []
    for s in raw.get("servers") or []:
        servers.append({
            "name": s.get("name", ""), "host": s.get("host", ""), "port": int(s.get("port", 22)),
            "user": s.get("user", "root"), "key_path": s.get("key_path") or "", "notes": s.get("notes") or "",
            "has_password": _has_password(s), "password_env": s.get("password_env") or "",
        })
    return {
        "servers": servers,
        "interval": int(raw.get("interval", 60)),
        "telegram": {"chat_id": str(tg.get("chat_id") or ""),
                     "has_token": bool(tg.get("token") or tg.get("token_enc"))},
    }


def _secret_fields(secret: str, plain_key: str, enc_key: str) -> dict:
    if secrets_store.IS_WINDOWS:
        return {enc_key: secrets_store.protect(secret)}
    return {plain_key: secret}                   # вне Windows: открытым текстом + chmod 600


def validate_server(s: dict) -> dict:
    name = str(s.get("name") or "").strip()
    host = str(s.get("host") or "").strip()
    user = str(s.get("user") or "root").strip()
    if not NAME_RE.match(name) or name.lower() == "settings":
        raise SettingsError("Имя сервера: 1–40 символов (буквы, цифры, пробел, . _ -)")
    if not HOST_RE.match(host):
        raise SettingsError(f"{name}: укажите IP-адрес или домен сервера")
    try:
        port = int(s.get("port") or 22)
    except (TypeError, ValueError):
        raise SettingsError(f"{name}: порт должен быть числом")
    if not 1 <= port <= 65535:
        raise SettingsError(f"{name}: порт 1–65535")
    if not USER_RE.match(user):
        raise SettingsError(f"{name}: недопустимое имя пользователя SSH")
    key_path = str(s.get("key_path") or "").strip()
    notes = str(s.get("notes") or "").strip()[:200]
    if len(str(s.get("password") or "")) > 256:
        raise SettingsError(f"{name}: слишком длинный пароль")
    return {"name": name, "host": host, "port": port, "user": user, "key_path": key_path, "notes": notes}


def apply_update(raw: dict, payload: dict) -> dict:
    """Новый raw-конфиг из payload браузера. Пароли: пусто = оставить прежний (по orig_name)."""
    old = {s.get("name"): s for s in raw.get("servers") or []}
    servers, names = [], set()
    for item in payload.get("servers") or []:
        v = validate_server(item)
        if v["name"] in names:
            raise SettingsError(f"Повтор имени сервера: {v['name']}")
        names.add(v["name"])
        entry = {"name": v["name"], "host": v["host"], "port": v["port"], "user": v["user"]}
        prev = old.get(item.get("orig_name") or v["name"]) or {}
        pw = str(item.get("password") or "")
        if pw:
            entry.update(_secret_fields(pw, "password", "password_enc"))
        elif not item.get("clear_password"):
            for k in ("password_enc", "password", "password_env"):
                if prev.get(k):
                    entry[k] = prev[k]
        if v["key_path"]:
            entry["key_path"] = v["key_path"]
        if v["notes"]:
            entry["notes"] = v["notes"]
        for k in ("docker_cmd", "timeout"):        # расширенные поля, заданные вручную, сохраняем
            if prev.get(k) is not None:
                entry[k] = prev[k]
        servers.append(entry)
    new = {k: v for k, v in raw.items() if k not in ("servers", "interval", "telegram")}
    try:
        interval = int(payload.get("interval", raw.get("interval", 60)))
    except (TypeError, ValueError):
        raise SettingsError("Интервал опроса должен быть числом")
    if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise SettingsError(f"Интервал опроса: от {MIN_INTERVAL} до {MAX_INTERVAL} секунд")
    new["interval"] = interval
    tg_old, tg_in = raw.get("telegram") or {}, payload.get("telegram") or {}
    tg = {}
    chat = str(tg_in.get("chat_id", tg_old.get("chat_id") or "")).strip()
    if chat:
        if not re.match(r"^-?\d{1,20}$|^@\w{4,64}$", chat):
            raise SettingsError("Telegram chat id: число (например 123456789) или @канал")
        tg["chat_id"] = chat
    token = str(tg_in.get("token") or "").strip()
    if token:
        if not re.match(r"^\d{5,15}:[\w-]{20,80}$", token):
            raise SettingsError("Токен Telegram выглядит неверно (формат 123456:ABC...)")
        tg.update(_secret_fields(token, "token", "token_enc"))
    elif not tg_in.get("clear_token"):
        for k in ("token_enc", "token"):
            if tg_old.get(k):
                tg[k] = tg_old[k]
    for k in ("token_env", "chat_id_env"):
        if tg_old.get(k):
            tg[k] = tg_old[k]
    if tg:
        new["telegram"] = tg
    new["servers"] = servers
    return new


def server_for_test(raw: dict, item: dict):
    """ServerCfg для «Проверить подключение» из формы (пароль пустой -> сохранённый)."""
    import amnezia_monitor as am
    v = validate_server(item)
    pw = str(item.get("password") or "") or None
    if pw is None and not item.get("clear_password"):
        prev = {s.get("name"): s for s in raw.get("servers") or []}.get(item.get("orig_name") or v["name"]) or {}
        if prev.get("password_enc"):
            pw = secrets_store.unprotect(str(prev["password_enc"]))
        elif prev.get("password"):
            pw = str(prev["password"])
        elif prev.get("password_env"):
            pw = os.environ.get(str(prev["password_env"]))
    return am.ServerCfg(name=v["name"], host=v["host"], port=v["port"], user=v["user"],
                        key_path=os.path.expanduser(v["key_path"]) if v["key_path"] else None,
                        password=pw, timeout=8)


def _ssh_error_text(e: Exception) -> str:
    n = type(e).__name__
    if n == "AuthenticationException" or n == "BadAuthenticationType":
        return "SSH отклонил вход: неверный пользователь, пароль или ключ"
    if n in ("NoValidConnectionsError",) or isinstance(e, ConnectionRefusedError):
        return "соединение отклонено"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "таймаут"
    if isinstance(e, (ConnectionResetError, EOFError, BrokenPipeError)):
        return "сервер оборвал соединение — проверьте, что это SSH-порт и что IP не заблокирован (fail2ban)"
    if isinstance(e, FileNotFoundError):
        return "файл ключа не найден"
    if n == "SSHException":
        return "ошибка SSH (" + str(e)[:120] + ")"
    if n == "PasswordRequiredException":
        return "ключ защищён паролем — используйте ключ без пароля или вход по паролю"
    return n


def test_connection(srv, runner_factory=None, tcp=None) -> dict:
    """Только чтение: TCP до SSH-порта, вход по SSH, `docker ps`. Ничего не меняет на сервере."""
    import amnezia_monitor as am
    tcp = tcp or am.tcp_latency
    steps = []
    ms = tcp(srv.host, srv.port, 5.0)
    steps.append({"step": f"TCP {srv.host}:{srv.port}", "ok": ms is not None,
                  "detail": f"{ms} мс" if ms is not None else "порт недоступен (сервер выключен, неверный адрес/порт или фаервол)"})
    if ms is None:
        return {"ok": False, "steps": steps}
    if not srv.password and not srv.key_path:
        steps.append({"step": "Вход по SSH", "ok": False, "detail": "не задан ни пароль, ни ключ"})
        return {"ok": False, "steps": steps}
    runner = None
    try:
        t0 = time.monotonic()
        runner = (runner_factory or am.SSHRunner)(srv)
        steps.append({"step": f"Вход по SSH ({srv.user})", "ok": True,
                      "detail": f"{round((time.monotonic() - t0) * 1000)} мс"})
    except Exception as e:
        steps.append({"step": f"Вход по SSH ({srv.user})", "ok": False, "detail": _ssh_error_text(e)})
        return {"ok": False, "steps": steps}
    try:
        cmd = am.READ_ONLY_COMMANDS["docker_ps"].format(docker=srv.docker_cmd)
        rc, out, err = runner.run(cmd)
        if rc != 0 and srv.docker_cmd == "docker":
            rc, out, err = runner.run(am.READ_ONLY_COMMANDS["docker_ps"].format(docker="sudo -n docker"))
        if rc == 0:
            amn = [c["name"] + ("" if c["up"] else " (остановлен)") for c in am.parse_docker_ps(out)]
            steps.append({"step": "docker ps", "ok": True,
                          "detail": ("контейнеры Amnezia: " + ", ".join(amn)) if amn
                          else "Docker доступен, но контейнеров Amnezia не найдено"})
        else:
            steps.append({"step": "docker ps", "ok": False,
                          "detail": "нет доступа к Docker (нужен root или группа docker): " + (err.strip()[:120] or f"код {rc}")})
    except Exception as e:
        steps.append({"step": "docker ps", "ok": False, "detail": type(e).__name__})
    finally:
        try:
            runner.close()
        except Exception:
            pass
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


class SettingsContext:
    """Всё, что нужно HTTP-обработчику для страницы настроек (только в режиме приложения)."""

    def __init__(self, config_path, token: str, data_dir: str = "", on_saved=None, on_quit=None,
                 runner_factory=None, tcp=None, app_mode: bool = True):
        self.config_path = str(config_path)
        self.token = token
        self.data_dir = data_dir
        self.on_saved = on_saved
        self.on_quit = on_quit
        self.runner_factory = runner_factory
        self.tcp = tcp
        self.app_mode = app_mode

    def get(self) -> dict:
        v = public_view(read_raw(self.config_path))
        v.update({"data_dir": self.data_dir, "config_path": self.config_path,
                  "encryption": "DPAPI (текущий пользователь Windows)" if secrets_store.IS_WINDOWS
                  else "нет (файл с правами 600)", "can_quit": bool(self.on_quit)})
        return v

    def save(self, payload: dict) -> dict:
        new = apply_update(read_raw(self.config_path), payload)
        write_raw(self.config_path, new)
        if self.on_saved:
            self.on_saved()
        return self.get()

    def test(self, payload: dict) -> dict:
        srv = server_for_test(read_raw(self.config_path), payload.get("server") or {})
        return test_connection(srv, self.runner_factory, self.tcp)
