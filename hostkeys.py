# -*- coding: utf-8 -*-
"""Ключи хостов SSH: своё хранилище программы и понятная ошибка при смене ключа.

* Свой файл known_hosts лежит в папке данных программы (%APPDATA%\\AmneziaMonitor\\known_hosts).
  Файл OpenSSH пользователя (~/.ssh/known_hosts) программа только читает и больше не перезаписывает.
* Если в своём файле есть запись для хоста — она главная. Иначе берётся запись из ~/.ssh/known_hosts.
* Согласование типа ключа ограничивается типами, которые уже известны для этого хоста. Так ключ
  ED25519 никогда не сравнивается с сохранённым RSA/ECDSA (ложная «смена ключа»).
* При настоящем несовпадении поднимается HostKeyMismatch со старыми и новым отпечатками.
  Новый ключ записывается только через trust_new_key() — после явного подтверждения пользователя
  и только если живой ключ сервера совпадает с отпечатком, который пользователь видел.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
from pathlib import Path
from typing import Optional

_lock = threading.Lock()
_store_path: Optional[Path] = None

# Алгоритмы подписи ключа хоста -> тип ключа в known_hosts
_RSA_ALGS = ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")


def set_store_path(path) -> None:
    """Вызывается программой при старте: где лежит свой known_hosts."""
    global _store_path
    _store_path = Path(path) if path else None


def store_path() -> Path:
    if _store_path is not None:
        return _store_path
    env = os.environ.get("AMNEZIA_KNOWN_HOSTS")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~/.amnezia-monitor/known_hosts"))


def user_known_hosts() -> Path:
    return Path(os.path.expanduser("~/.ssh/known_hosts"))


def host_entry_name(host: str, port: int) -> str:
    """Как OpenSSH и paramiko пишут хост: порт 22 без скобок, иначе [host]:port."""
    port = int(port or 22)
    return host if port == 22 else "[%s]:%d" % (host, port)


def fingerprint(key) -> str:
    """SHA256-отпечаток как у `ssh-keygen -lf`."""
    blob = key.asbytes() if hasattr(key, "asbytes") else bytes(key)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def key_type_label(name: str) -> str:
    n = str(name or "")
    if n.startswith("ssh-ed25519"):
        return "ED25519"
    if n.startswith("ecdsa-"):
        return "ECDSA"
    if n in _RSA_ALGS or n.startswith("ssh-rsa"):
        return "RSA"
    if n.startswith("ssh-dss"):
        return "DSA"
    return n


def describe(key) -> dict:
    return {"type": key_type_label(key.get_name()), "algo": key.get_name(), "fingerprint": fingerprint(key)}


def _load(path: Path):
    import paramiko
    hk = paramiko.HostKeys()
    if path.is_file():
        try:
            hk.load(str(path))
        except Exception:
            # Битые или незнакомые строки paramiko пропускает сам; сюда попадаем только при
            # ошибке чтения файла — тогда считаем, что записей нет.
            return paramiko.HostKeys()
    return hk


def lookup(host: str, port: int) -> tuple:
    """Известные ключи хоста: (dict тип->PKey, источник 'app'|'user'|None)."""
    name = host_entry_name(host, port)
    for source, path in (("app", store_path()), ("user", user_known_hosts())):
        entry = _load(path).lookup(name)
        if entry:
            return {k: entry[k] for k in entry.keys()}, source
    return {}, None


def known_fingerprints(known: dict) -> list:
    return [describe(k) for k in known.values()]


def allowed_host_key_algorithms(known: dict) -> set:
    algs = set()
    for name in known:
        if key_type_label(name) == "RSA":
            algs.update(_RSA_ALGS)
        else:
            algs.add(name)
    return algs


def disabled_algorithms_for(known: dict) -> Optional[dict]:
    """Оставить в согласовании только типы ключей, которые уже известны для этого хоста."""
    if not known:
        return None
    import paramiko
    allowed = allowed_host_key_algorithms(known)
    every = list(getattr(paramiko.Transport, "_preferred_keys", ()))
    disabled = [a for a in every if a not in allowed]
    if len(disabled) == len(every):      # не знаем ни одного из известных типов — не ограничиваем
        return None
    return {"keys": disabled}


class HostKeyMismatch(Exception):
    """Ключ сервера не совпадает с сохранённым. Подключение остановлено ДО входа (пароль не ушёл)."""

    def __init__(self, host: str, port: int, new_key, old: list, source: Optional[str],
                 type_changed: bool = False):
        self.host = host
        self.port = int(port or 22)
        self.new_key = new_key
        self.old = old
        self.source = source
        self.type_changed = type_changed
        super().__init__("ключ хоста %s изменился" % host_entry_name(host, port))

    def as_dict(self) -> dict:
        d = {"host": self.host, "port": self.port, "old": self.old,
             "new": describe(self.new_key) if self.new_key is not None else None,
             "source": "программа" if self.source == "app" else "~/.ssh/known_hosts",
             "type_changed": self.type_changed}
        return d


class VerifyPolicy:
    """MissingHostKeyPolicy для SSHClient без загруженных ключей: вся проверка здесь.

    paramiko вызывает missing_host_key() сразу после обмена ключами и до аутентификации,
    поэтому исключение отсюда не даёт отправить пароль на чужой сервер.
    """

    def __init__(self, host: str, port: int, known: dict, source: Optional[str], strict: bool = False):
        self.host, self.port, self.known, self.source, self.strict = host, int(port or 22), known, source, strict
        self.accepted_new = None

    def missing_host_key(self, client, hostname, key):
        if self.known:
            same = self.known.get(key.get_name())
            if same is not None and same == key:
                return
            raise HostKeyMismatch(self.host, self.port, key, known_fingerprints(self.known), self.source,
                                  type_changed=same is None)
        if self.strict:
            import paramiko
            raise paramiko.SSHException("ключ хоста %s неизвестен (строгая проверка)"
                                        % host_entry_name(self.host, self.port))
        self.accepted_new = key       # первый вход (TOFU) — запомнить после успешного входа


def remember_first_key(host: str, port: int, key) -> None:
    """Первый вход: дописать ключ в свой файл программы (не в ~/.ssh/known_hosts)."""
    _save_entry(host, port, key, replace=False)


def _save_entry(host: str, port: int, key, replace: bool) -> None:
    import paramiko
    path = store_path()
    name = host_entry_name(host, port)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        hk = _load(path)
        keep = paramiko.HostKeys()
        for e in hk._entries:
            if replace and name in e.hostnames:
                rest = [h for h in e.hostnames if h != name]
                if not rest:
                    continue
                e.hostnames = rest
            keep._entries.append(e)
        if not replace and keep.lookup(name) and key.get_name() in keep.lookup(name):
            return
        keep.add(name, key.get_name(), key)
        tmp = path.with_name(path.name + ".tmp")
        keep.save(str(tmp))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)


def fetch_server_key(host: str, port: int, timeout: float = 8.0, disabled: Optional[dict] = None):
    """Только обмен ключами, без входа: какой ключ хоста сейчас показывает сервер."""
    import paramiko
    sock = socket.create_connection((host, int(port or 22)), timeout=timeout)
    t = paramiko.Transport(sock, disabled_algorithms=disabled)
    try:
        t.banner_timeout = timeout
        t.start_client(timeout=timeout)
        return t.get_remote_server_key()
    finally:
        t.close()


class TrustError(ValueError):
    pass


def trust_new_key(host: str, port: int, expected_fingerprint: str, confirmed: bool,
                  fetch=fetch_server_key) -> dict:
    """Заменить сохранённый ключ хоста на текущий ключ сервера.

    Только при confirmed=True и только если живой ключ совпадает с expected_fingerprint —
    тем отпечатком, который пользователь видел и подтвердил. Пишется только свой файл программы.
    """
    if not confirmed:
        raise TrustError("нужно явное подтверждение замены ключа")
    exp = str(expected_fingerprint or "").strip()
    if not exp.startswith("SHA256:"):
        raise TrustError("не указан отпечаток нового ключа")
    key = fetch(host, port)
    if fingerprint(key) != exp:
        # сервер мог показать ключ другого типа; пробуем по одному каждый тип
        key = None
        import paramiko
        every = list(getattr(paramiko.Transport, "_preferred_keys", ()))
        for alg in every:
            try:
                k = fetch(host, port, disabled={"keys": [a for a in every if a != alg]})
            except Exception:
                continue
            if fingerprint(k) == exp:
                key = k
                break
        if key is None:
            raise TrustError("ключ сервера снова другой — замена отменена. Проверьте сервер.")
    _save_entry(host, port, key, replace=True)
    return {"ok": True, "host": host, "port": int(port or 22), "saved": describe(key),
            "file": str(store_path())}
