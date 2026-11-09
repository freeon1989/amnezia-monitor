#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
amnezia_monitor — агент мониторинга собственных серверов AmneziaVPN (только чтение).

Команды:
  check    — один проход опроса всех серверов
  run      — бесконечный цикл с интервалом из конфига
  summary  — сводка по истории (SQLite); --hours N, --send — отправить в Telegram
Режим --demo — работа на встроенных тестовых данных, без реальных серверов.

На серверах выполняются ТОЛЬКО команды чтения (см. READ_ONLY_COMMANDS).
Пароли и ключи никогда не пишутся в лог и в базу.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

log = logging.getLogger("amnezia_monitor")

ONLINE_HANDSHAKE_SEC = 180
DEFAULT_INTERVAL = 60
WG_TYPES = ("awg", "wireguard")
KNOWN_PREFIX = "amnezia"

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


@dataclass
class ServerCfg:
    name: str
    host: str
    port: int = 22
    user: str = "root"
    key_path: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)   # не попадает в repr/лог
    docker_cmd: str = "docker"
    timeout: int = 10
    notes: str = ""

    def safe(self) -> str:
        auth = "key" if self.key_path else ("password" if self.password else "agent")
        return f"{self.name} ({self.user}@{self.host}:{self.port}, auth={auth})"


@dataclass
class Config:
    servers: list
    interval: int = DEFAULT_INTERVAL
    db_path: str = "amnezia_monitor.db"
    telegram_token: Optional[str] = field(default=None, repr=False)
    telegram_chat_id: Optional[str] = None
    cpu_threshold: float = 85.0
    mem_threshold: float = 90.0
    handshake_online_sec: int = ONLINE_HANDSHAKE_SEC
    clients: dict = field(default_factory=dict)        # pubkey -> имя
    daily_summary_hour: Optional[int] = None           # час локального времени, напр. 9


def load_dotenv(path: Path) -> None:
    """Простейший загрузчик .env (KEY=VALUE), не перезаписывает уже заданные переменные."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _tg_token(tg: dict) -> Optional[str]:
    if tg.get("token_enc"):
        import secrets_store
        try:
            return secrets_store.unprotect(str(tg["token_enc"]))
        except Exception:
            log.error("Не удалось расшифровать токен Telegram — задайте его заново")
            return None
    if tg.get("token"):
        return str(tg["token"])
    return os.environ.get(tg.get("token_env", "TELEGRAM_BOT_TOKEN"))


def load_config(path: str, allow_empty: bool = False) -> Config:
    if yaml is None:
        raise ConfigError("Не установлен PyYAML: pip install -r requirements.txt")
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Файл конфигурации не найден: {path}")
    load_dotenv(p.parent / ".env")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    for forbidden in ("password", "password_env", "password_enc"):
        if forbidden in defaults:
            raise ConfigError("Пароль нельзя задавать в defaults: у каждого сервера должен быть свой "
                              "password или password_env")
    servers, names, pw_seen = [], set(), {}
    for i, s in enumerate(raw.get("servers") or []):
        s = {**defaults, **(s or {})}
        name, host = s.get("name"), s.get("host")
        if not name or not host:
            raise ConfigError(f"servers[{i}]: обязательны поля name и host")
        if name in names:
            raise ConfigError(f"Повтор имени сервера: {name}")
        names.add(name)
        password = s.get("password")
        if s.get("password_enc"):          # зашифровано приложением (DPAPI на Windows)
            import secrets_store
            try:
                password = secrets_store.unprotect(str(s["password_enc"]))
            except Exception:
                raise ConfigError(f"{name}: не удалось расшифровать пароль (файл создан другим пользователем "
                                  f"Windows?) — задайте пароль заново в настройках")
        if s.get("password_env"):
            password = os.environ.get(str(s["password_env"]))
            if not password:
                raise ConfigError(f"{name}: переменная окружения {s['password_env']} не задана")
        key_path = s.get("key_path")
        if key_path:
            key_path = os.path.expanduser(str(key_path))
        if password is not None:
            password = str(password)
            if password in pw_seen:
                log.warning("Серверы %s и %s используют одинаковый пароль — рекомендуем разные",
                            pw_seen[password], name)
            pw_seen.setdefault(password, name)
        servers.append(ServerCfg(
            name=str(name), host=str(host), port=int(s.get("port", 22)),
            user=str(s.get("user", "root")), key_path=key_path, password=password,
            docker_cmd=str(s.get("docker_cmd", "docker")), timeout=int(s.get("timeout", 10)),
            notes=str(s.get("notes") or ""),
        ))
    if not servers and not allow_empty:
        raise ConfigError("В конфиге нет ни одного сервера (секция servers)")
    tg = raw.get("telegram") or {}
    th = raw.get("thresholds") or {}
    return Config(
        servers=servers,
        interval=int(raw.get("interval", DEFAULT_INTERVAL)),
        db_path=str(raw.get("db_path", p.parent / "amnezia_monitor.db")),
        telegram_token=_tg_token(tg),
        telegram_chat_id=(str(tg["chat_id"]) if tg.get("chat_id") else None)
        or os.environ.get(tg.get("chat_id_env", "TELEGRAM_CHAT_ID")),
        cpu_threshold=float(th.get("cpu_percent", 85)),
        mem_threshold=float(th.get("mem_percent", 90)),
        handshake_online_sec=int(th.get("handshake_online_sec", ONLINE_HANDSHAKE_SEC)),
        clients={str(k): str(v) for k, v in (raw.get("clients") or {}).items()},
        daily_summary_hour=raw.get("daily_summary_hour"),
    )

# ---------------------------------------------------------------------------
# Парсеры (чистые функции — покрыты тестами)
# ---------------------------------------------------------------------------

def parse_uptime_seconds(text: str) -> Optional[float]:
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


def parse_loadavg(text: str) -> Optional[tuple]:
    try:
        a = text.split()
        return float(a[0]), float(a[1]), float(a[2])
    except (ValueError, IndexError):
        return None


def parse_meminfo(text: str) -> dict:
    vals = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            vals[m.group(1)] = int(m.group(2)) * 1024
    total = vals.get("MemTotal")
    avail = vals.get("MemAvailable", vals.get("MemFree"))
    if not total or avail is None:
        return {}
    used = total - avail
    return {"total": total, "used": used, "percent": round(used * 100.0 / total, 1)}


def parse_proc_stat(text: str) -> Optional[tuple]:
    """Возвращает (busy, total) jiffies из строки 'cpu ' в /proc/stat."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            nums = [int(x) for x in line.split()[1:]]
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            total = sum(nums[:8])
            return total - idle, total
    return None


def cpu_percent(prev: Optional[tuple], cur: Optional[tuple]) -> Optional[float]:
    if not prev or not cur:
        return None
    dt = cur[1] - prev[1]
    if dt <= 0:
        return None
    return round(max(0.0, min(100.0, (cur[0] - prev[0]) * 100.0 / dt)), 1)


def parse_net_dev(text: str) -> dict:
    """iface -> (rx_bytes, tx_bytes); lo и docker-мосты veth* пропускаются."""
    res = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        iface, data = line.split(":", 1)
        iface = iface.strip()
        if iface == "lo" or iface.startswith("veth"):
            continue
        f = data.split()
        if len(f) >= 9:
            res[iface] = (int(f[0]), int(f[8]))
    return res


def net_rates(prev: Optional[dict], cur: dict, dt: float) -> tuple:
    """Суммарные rx/tx байт/с по всем внешним интерфейсам между двумя опросами."""
    if not prev or dt <= 0:
        return None, None
    rx = tx = 0
    for iface, (r, t) in cur.items():
        if iface in prev:
            pr, pt = prev[iface]
            rx += max(0, r - pr)   # сброс счётчиков (перезагрузка) -> 0
            tx += max(0, t - pt)
    return rx / dt, tx / dt


def parse_docker_ps(text: str) -> list:
    """Строки формата '{{.Names}}|{{.Status}}|{{.Image}}' -> список контейнеров Amnezia."""
    out = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2 or not parts[0]:
            continue
        name, status = parts[0], parts[1]
        image = parts[2] if len(parts) > 2 else ""
        if not (name.startswith(KNOWN_PREFIX) or "amnezia" in image):
            continue
        out.append({"name": name, "status": status, "up": status.lower().startswith("up"),
                    "type": container_type(name)})
    return out


def container_type(name: str) -> str:
    n = name.lower()
    for t in ("awg", "wireguard", "xray", "openvpn", "cloak", "shadowsocks", "ipsec", "dns", "sftp", "tor"):
        if t in n:
            return t
    return "other"


def parse_wg_dump(text: str, now: float, online_sec: int = ONLINE_HANDSHAKE_SEC) -> list:
    """Разбор `awg/wg show all dump`.
    Строка интерфейса (5 полей): iface, private-key, public-key, listen-port, fwmark — ПРИВАТНЫЙ
    КЛЮЧ ИГНОРИРУЕТСЯ И НИГДЕ НЕ СОХРАНЯЕТСЯ.
    Строка пира (9 полей): iface, public-key, preshared-key, endpoint, allowed-ips,
    latest-handshake, rx, tx, persistent-keepalive.
    AmneziaWG может добавлять поля обфускации в строку интерфейса — поэтому пиры
    распознаются по числу полей == 9 и числовым handshake/rx/tx."""
    peers = []
    for line in text.splitlines():
        f = line.rstrip("\n").split("\t")
        if len(f) != 9:
            continue
        try:
            hs, rx, tx = int(f[5]), int(f[6]), int(f[7])
        except ValueError:
            continue
        age = (now - hs) if hs > 0 else None
        peers.append({
            "iface": f[0], "pubkey": f[1],
            "endpoint": None if f[3] in ("(none)", "") else f[3],
            "allowed_ips": f[4], "handshake": hs, "handshake_age": age,
            "rx": rx, "tx": tx,
            "online": age is not None and age < online_sec,
        })
    return peers


def parse_clients_table(text: str) -> dict:
    """clientsTable Amnezia: JSON-массив [{"clientId": <pubkey>, "userData": {"clientName": ...}}]."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    res = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("clientId"):
                name = (item.get("userData") or {}).get("clientName")
                if name:
                    res[str(item["clientId"])] = str(name)
    return res


_UNBOUND_RE = re.compile(r"(?:^\[(\d+)\].*?)?info: ([0-9a-fA-F.:]+) (\S+?)\.? (\w+) IN\b")
_SKIP_DOMAINS = (".in-addr.arpa", ".ip6.arpa", ".local", ".lan", ".internal", "localhost")


def parse_dns_log(text: str) -> list:
    """Разбор журнала DNS-запросов. Поддерживаются:
    * AdGuard Home querylog.json: {"T":"2026-10-31T19:00:00+03:00","QH":"example.com","QT":"A","IP":"10.8.1.2",...}
    * Unbound с log-queries: yes: "[1730390000] unbound[1:0] info: 10.8.1.2 example.com. A IN"
    Возвращает список словарей {line, ts, ip, domain, qtype}; ts может быть None."""
    res = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        item = None
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                d = None
            if isinstance(d, dict) and d.get("QH") and d.get("IP"):
                ts = None
                try:
                    ts = datetime.fromisoformat(str(d.get("T", "")).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
                item = {"ts": ts, "ip": str(d["IP"]), "domain": str(d["QH"]), "qtype": str(d.get("QT", ""))}
        else:
            m = _UNBOUND_RE.search(line)
            if m:
                item = {"ts": float(m.group(1)) if m.group(1) else None, "ip": m.group(2),
                        "domain": m.group(3), "qtype": m.group(4)}
        if item:
            dom = item["domain"].rstrip(".").lower()
            if not dom or dom.endswith(_SKIP_DOMAINS) or "." not in dom:
                continue
            item["domain"] = dom
            item["line"] = line
            res.append(item)
    return res


_SLD = {"co", "com", "org", "net", "gov", "edu", "ac", "msk", "spb"}


def root_domain(domain: str) -> str:
    """Упрощённо приводит имя к «сайту»: rr1.sn-abc.googlevideo.com -> googlevideo.com, bbc.co.uk -> bbc.co.uk."""
    parts = domain.rstrip(".").lower().split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def ip_to_client_map(peers: list) -> dict:
    """allowed-ips пира (10.8.1.2/32, ...) -> имя клиента."""
    m = {}
    for p in peers:
        for a in str(p.get("allowed_ips") or "").split(","):
            ip = a.strip().split("/")[0]
            if ip:
                m[ip] = p.get("client") or p.get("pubkey", "")[:8]
    return m


def attach_dns(res, text: str, status_if_empty: str = "no_log") -> None:
    """Разбирает журнал DNS и сопоставляет IP туннеля с клиентом."""
    entries = parse_dns_log(text)
    if not entries:
        res.dns_status = status_if_empty
        return
    m = ip_to_client_map(res.peers)
    for e in entries:
        e["client"] = m.get(e["ip"], e["ip"])
    res.dns = entries
    res.dns_status = "ok"


DNS_HISTORY_SEC = 24 * 3600
DNS_READ_BYTES = 524288          # хвост одного файла; вместе с архивами не больше ~50 МиБ
DNS_ARCHIVE_FILES = 96           # 24 ч при ротации раз в 15 минут
_DNS_MARK_MISSING = "AMNEZIA_DNS_ACTIVE_MISSING"
_DNS_MARK_FILE = "AMNEZIA_DNS_FILE"
_DNS_NOTES = {
    "no_log": "Файла журнала в контейнере amnezia-dns нет, и docker logs пуст. "
              "Запросы не пишутся, либо пишутся в путь, который программа ещё не знает.",
    "empty": "Журнал на сервере есть, но запросов в прочитанном окне нет. Это не «журнал выключен».",
    "unreadable": "Контейнер amnezia-dns работает, но прочитать журнал не удалось. "
                  "Программа ничего на сервере не меняет.",
    "unsupported": "Файл журнала прочитан, но строки запросов не распознаны. "
                   "Ожидается Unbound «[время] unbound[…] info: IP имя. A IN» или AdGuard querylog.json.",
}


def interpret_dns_capture(text: str, rc: int, now: float) -> dict:
    """Текст команды чтения → статус, записи не старше 24 ч и фактический охват.

    no_log — источника нет. empty — источник есть, запросов нет.
    unreadable — команда не выполнилась. unsupported — текст есть, формат чужой.
    Записи старше 24 ч отбрасываются и не достраиваются.
    """
    raw = text or ""
    if rc != 0 and not raw.strip():
        return {"status": "unreadable", "entries": [], "coverage_sec": None, "note": _DNS_NOTES["unreadable"]}
    active_missing = _DNS_MARK_MISSING in raw
    archive_count = raw.count(_DNS_MARK_FILE)
    body = []
    for line in raw.splitlines():
        s = line.strip()
        if s in (_DNS_MARK_MISSING, "AMNEZIA_DNS_ARCHIVES") or s.startswith(_DNS_MARK_FILE):
            continue
        if s.startswith("==>") and s.endswith("<=="):
            continue
        body.append(line)
    entries = parse_dns_log("\n".join(body))
    cutoff = now - DNS_HISTORY_SEC
    fresh = [e for e in entries if e["ts"] is None or e["ts"] >= cutoff]
    dropped_old = len(entries) - len(fresh)
    timed = [e["ts"] for e in fresh if e["ts"] is not None]
    if fresh:
        oldest = min(timed) if timed else None
        reach = (now - oldest) if oldest is not None else None
        note = None
        if reach is not None and reach < DNS_HISTORY_SEC - 1800:
            note = ("Показаны фактические %.1f ч. Более ранних строк в журнале сервера нет, "
                    "программа их не достраивает." % (reach / 3600.0))
        return {"status": "ok", "entries": fresh, "coverage_sec": reach, "note": note}
    if any(line.strip() for line in body):
        note = _DNS_NOTES["unsupported"]
        if dropped_old:
            note = "В журнале есть только запросы старше 24 часов. В окно суток они не входят."
            return {"status": "empty", "entries": [], "coverage_sec": None, "note": note}
        return {"status": "unsupported", "entries": [], "coverage_sec": None, "note": note}
    if (not active_missing) or archive_count:
        return {"status": "empty", "entries": [], "coverage_sec": None, "note": _DNS_NOTES["empty"]}
    return {"status": "no_log", "entries": [], "coverage_sec": None, "note": _DNS_NOTES["no_log"]}


def apply_dns_result(res, info: dict) -> None:
    m = ip_to_client_map(res.peers)
    for e in info["entries"]:
        e["client"] = m.get(e["ip"], e["ip"])
    res.dns = info["entries"]
    res.dns_status = info["status"]
    res.dns_note = info.get("note")
    res.dns_coverage_sec = info.get("coverage_sec")


def fmt_bytes(n: Optional[float], per_sec: bool = False) -> str:
    if n is None:
        return "—"
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    n = float(n)
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    s = f"{n:.0f} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}"
    return s + ("/с" if per_sec else "")


def fmt_duration(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    if sec < 60:
        return "<1 мин"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"

# ---------------------------------------------------------------------------
# Доступ к серверу: только чтение
# ---------------------------------------------------------------------------

READ_ONLY_COMMANDS = {
    "uptime": "cat /proc/uptime",
    "loadavg": "cat /proc/loadavg",
    "nproc": "nproc",
    "meminfo": "cat /proc/meminfo",
    "stat": "head -1 /proc/stat",
    "netdev": "cat /proc/net/dev",
    # docker_cmd подставляется из конфига ("docker" или "sudo -n docker")
    "docker_ps": "{docker} ps -a --format '{{{{.Names}}}}|{{{{.Status}}}}|{{{{.Image}}}}'",
    "wg_dump": "{docker} exec {c} sh -c 'awg show all dump 2>/dev/null || wg show all dump'",
    "clients": "{docker} exec {c} sh -c 'cat /opt/amnezia/{t}/clientsTable 2>/dev/null || "
               "cat /opt/amnezia/*/clientsTable 2>/dev/null'",
    # Только чтение. Активный журнал Amnezia Unbound и не более 96 ротированных файлов,
    # с каждого — не больше DNS_READ_BYTES. Старые пути AdGuard и /var/log/unbound остаются запасными.
    "dns_log": "{docker} exec {c} sh -c '"
               "tail -c " + str(DNS_READ_BYTES) + " /opt/unbound/etc/unbound/unbound.log 2>/dev/null "
               "|| echo " + _DNS_MARK_MISSING + "; "
               "for f in $(ls -1 /opt/unbound/etc/unbound/log-archive/unbound-*.log 2>/dev/null | sort | tail -n "
               + str(DNS_ARCHIVE_FILES) + "); do echo " + _DNS_MARK_FILE + "; tail -c " + str(DNS_READ_BYTES) + " \"$f\"; done; "
               "tail -n {n} /opt/adguardhome/work/data/querylog.json 2>/dev/null || "
               "tail -n {n} /var/log/unbound/unbound.log 2>/dev/null || tail -n {n} /var/log/unbound.log 2>/dev/null'",
    "dns_docker_logs": "{docker} logs --tail {n} {c}",
}
DNS_TAIL_LINES = 3000
# Белый список: любая команда, не начинающаяся с этих префиксов, будет отклонена.
_ALLOWED_RE = re.compile(r"^(cat /proc/|head -1 /proc/stat$|nproc$|(sudo -n )?docker (ps|exec \S+ sh -c '(awg|wg|cat|tail) )"
                         r"|(sudo -n )?docker logs --tail \d+ [\w.-]+$)")


class Runner:
    """Интерфейс выполнения команд на сервере."""
    def run(self, cmd: str) -> tuple:  # -> (rc, stdout, stderr)
        raise NotImplementedError

    def close(self):
        pass


class SSHRunner(Runner):
    def __init__(self, srv: ServerCfg):
        import paramiko  # импорт здесь, чтобы --demo и тесты работали без paramiko
        self.srv = srv
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        kh = Path(os.path.expanduser("~/.ssh/known_hosts"))
        if kh.is_file():
            try:
                self.client.load_host_keys(str(kh))
            except Exception:
                pass
        # Первый вход: ключ хоста запоминается (TOFU). Для строгой проверки
        # установите AMNEZIA_STRICT_HOSTKEY=1 и заранее добавьте хосты в known_hosts.
        if os.environ.get("AMNEZIA_STRICT_HOSTKEY") == "1":
            self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=srv.host, port=srv.port, username=srv.user,
            key_filename=srv.key_path or None, password=srv.password or None,
            look_for_keys=not srv.password and not srv.key_path,
            allow_agent=not srv.password,
            timeout=srv.timeout, banner_timeout=srv.timeout, auth_timeout=srv.timeout,
        )
        try:
            if kh.parent.is_dir():
                self.client.save_host_keys(str(kh))
        except Exception:
            pass

    def run(self, cmd: str) -> tuple:
        if not _ALLOWED_RE.match(cmd):
            raise PermissionError(f"Команда не из белого списка read-only: {cmd!r}")
        _, out, err = self.client.exec_command(cmd, timeout=self.srv.timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")

    def close(self):
        self.client.close()


def tcp_latency(host: str, port: int, timeout: float = 5.0) -> Optional[float]:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.monotonic() - t0) * 1000, 1)
    except OSError:
        return None


def icmp_ping(host: str, count: int = 2) -> Optional[float]:
    """Пинг с машины мониторинга (если есть утилита ping). Возвращает средний RTT в мс."""
    if sys.platform.startswith("win"):
        args = ["ping", "-n", str(count), "-w", "2000", host]
    else:
        args = ["ping", "-c", str(count), "-W", "2", host]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10,
                           encoding=("oem" if sys.platform.startswith("win") else None), errors="replace",
                           # в .exe без консоли не показывать мелькающее окно ping.exe
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"= [\d.]+/([\d.]+)/", r.stdout) or re.search(r"(?:Average|Среднее) = (\d+)", r.stdout)
    return float(m.group(1)) if m else None

# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
  id INTEGER PRIMARY KEY, ts REAL, server TEXT, reachable INTEGER, ssh_ok INTEGER,
  tcp_ms REAL, ping_ms REAL, uptime_s REAL, load1 REAL, nproc INTEGER, cpu_pct REAL,
  mem_pct REAL, mem_used INTEGER, mem_total INTEGER, rx_bps REAL, tx_bps REAL,
  peers_total INTEGER, peers_online INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS containers (
  id INTEGER PRIMARY KEY, ts REAL, server TEXT, name TEXT, type TEXT, up INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS peers (
  id INTEGER PRIMARY KEY, ts REAL, server TEXT, container TEXT, pubkey TEXT, client TEXT,
  endpoint TEXT, handshake INTEGER, rx INTEGER, tx INTEGER, online INTEGER);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL, server TEXT, kind TEXT, subject TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS dns (
  id INTEGER PRIMARY KEY, ts REAL, server TEXT, client TEXT, ip TEXT, domain TEXT, site TEXT, qtype TEXT);
CREATE INDEX IF NOT EXISTS idx_dns ON dns(server, ts);
CREATE INDEX IF NOT EXISTS idx_polls ON polls(server, ts);
CREATE INDEX IF NOT EXISTS idx_peers ON peers(server, ts);
CREATE INDEX IF NOT EXISTS idx_events ON events(ts);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            try:   # WAL: дашборд читает, пока агент пишет
                self.db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
        self.db.executescript(SCHEMA)
        self._migrate_dns_line_key()

    def _migrate_dns_line_key(self) -> None:
        """Старые базы не знают line_key. Индекс нельзя создавать до ALTER: executescript
        падает на CREATE INDEX и миграция ниже не выполняется."""
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(dns)")]
        if "line_key" not in cols:
            self.db.execute("ALTER TABLE dns ADD COLUMN line_key TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_dns_line ON dns(server, line_key)")
        self.db.commit()

    def get_state(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_state(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?,?)", (key, json.dumps(value)))

    def add_event(self, ts, server, kind, subject, message):
        self.db.execute("INSERT INTO events(ts, server, kind, subject, message) VALUES (?,?,?,?,?)",
                        (ts, server, kind, subject, message))

    def commit(self):
        self.db.commit()

# ---------------------------------------------------------------------------
# Опрос сервера
# ---------------------------------------------------------------------------

@dataclass
class PollResult:
    server: str
    ts: float
    reachable: bool = False
    ssh_ok: bool = False
    tcp_ms: Optional[float] = None
    ping_ms: Optional[float] = None
    uptime_s: Optional[float] = None
    load: Optional[tuple] = None
    nproc: Optional[int] = None
    cpu_raw: Optional[tuple] = None
    mem: dict = field(default_factory=dict)
    net: dict = field(default_factory=dict)
    containers: list = field(default_factory=list)
    peers: list = field(default_factory=list)     # с полями container, client
    error: Optional[str] = None
    dns: list = field(default_factory=list)       # записи журнала DNS за доступное окно
    dns_status: str = "no_container"              # ok | empty | no_log | unreadable | unsupported | no_container
    dns_note: Optional[str] = None
    dns_coverage_sec: Optional[float] = None


def collect(srv: ServerCfg, runner_factory: Callable[[ServerCfg], Runner], now: float,
            clients_map: dict, online_sec: int,
            probe: Optional[Callable[[ServerCfg], tuple]] = None) -> PollResult:
    res = PollResult(server=srv.name, ts=now)
    if probe is None:
        res.tcp_ms = tcp_latency(srv.host, srv.port, srv.timeout)
        res.ping_ms = icmp_ping(srv.host)
    else:
        res.tcp_ms, res.ping_ms = probe(srv)
    res.reachable = res.tcp_ms is not None or res.ping_ms is not None
    if res.tcp_ms is None:
        res.error = "SSH-порт недоступен"
        return res
    try:
        runner = runner_factory(srv)
    except Exception as e:  # ошибки аутентификации/сети; пароль в текст не попадает
        res.error = f"SSH: {type(e).__name__}"
        return res
    try:
        res.ssh_ok = True
        def out(key, **kw):
            cmd = READ_ONLY_COMMANDS[key].format(docker=srv.docker_cmd, **kw)
            rc, o, e = runner.run(cmd)
            return o if rc == 0 else ""
        res.uptime_s = parse_uptime_seconds(out("uptime"))
        res.load = parse_loadavg(out("loadavg"))
        try:
            res.nproc = int(out("nproc").strip())
        except ValueError:
            pass
        res.mem = parse_meminfo(out("meminfo"))
        res.cpu_raw = parse_proc_stat(out("stat"))
        res.net = parse_net_dev(out("netdev"))
        rc, ps, err = runner.run(READ_ONLY_COMMANDS["docker_ps"].format(docker=srv.docker_cmd))
        if rc != 0:
            res.error = "docker ps не выполнен (нужны права: группа docker или sudo -n docker)"
        res.containers = parse_docker_ps(ps)
        for c in res.containers:
            if c["type"] not in WG_TYPES or not c["up"]:
                continue
            names = dict(clients_map)
            names.update(parse_clients_table(out("clients", c=shlex.quote(c["name"]), t=c["type"])))
            # общий маппинг из конфига имеет приоритет над clientsTable
            names.update(clients_map)
            for p in parse_wg_dump(out("wg_dump", c=shlex.quote(c["name"])), now, online_sec):
                p["container"] = c["name"]
                p["client"] = names.get(p["pubkey"], p["pubkey"][:8] + "…")
                res.peers.append(p)
        dns_c = next((c for c in res.containers if c["type"] == "dns" and c["up"]), None)
        if dns_c:
            q = shlex.quote(dns_c["name"])
            cmd = READ_ONLY_COMMANDS["dns_log"].format(docker=srv.docker_cmd, c=q, n=DNS_TAIL_LINES)
            rc, captured, _err = runner.run(cmd)
            info = interpret_dns_capture(captured or "", rc, now)
            if info["status"] == "no_log":
                rc2, o, e = runner.run(READ_ONLY_COMMANDS["dns_docker_logs"].format(
                    docker=srv.docker_cmd, c=q, n=DNS_TAIL_LINES))
                logs = ((o or "") + "\n" + (e or "")).strip()
                if rc2 != 0 and not logs:
                    info = {"status": "unreadable", "entries": [], "coverage_sec": None,
                            "note": _DNS_NOTES["unreadable"]}
                elif logs:
                    parsed = interpret_dns_capture(logs, 0, now)
                    if parsed["entries"]:
                        info = parsed
                    elif parsed["status"] == "unsupported":
                        info = parsed
                    else:
                        info = {"status": "no_log", "entries": [], "coverage_sec": None,
                                "note": _DNS_NOTES["no_log"]}
            apply_dns_result(res, info)
    except Exception as e:
        res.error = f"Ошибка опроса: {type(e).__name__}: {e}"
    finally:
        runner.close()
    return res

# ---------------------------------------------------------------------------
# Обработка: метрики, события, запись
# ---------------------------------------------------------------------------

def process(store: Store, cfg: Config, r: PollResult) -> list:
    """Сохраняет результат, сравнивает с предыдущим состоянием, возвращает список сообщений-событий."""
    events = []
    key = f"srv:{r.server}"
    prev = store.get_state(key, {})
    first = not prev

    def ev(kind, subject, msg):
        events.append(msg)
        store.add_event(r.ts, r.server, kind, subject, msg)

    cpu = cpu_percent(tuple(prev["cpu_raw"]) if prev.get("cpu_raw") else None, r.cpu_raw)
    dt = r.ts - prev.get("ts", r.ts)
    rx_bps, tx_bps = net_rates(prev.get("net"), r.net, dt) if r.ssh_ok else (None, None)
    online = [p for p in r.peers if p["online"]]

    store.db.execute(
        "INSERT INTO polls(ts, server, reachable, ssh_ok, tcp_ms, ping_ms, uptime_s, load1, nproc, cpu_pct,"
        " mem_pct, mem_used, mem_total, rx_bps, tx_bps, peers_total, peers_online, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (r.ts, r.server, int(r.reachable), int(r.ssh_ok), r.tcp_ms, r.ping_ms, r.uptime_s,
         r.load[0] if r.load else None, r.nproc, cpu, r.mem.get("percent"), r.mem.get("used"),
         r.mem.get("total"), rx_bps, tx_bps, len(r.peers) if r.ssh_ok else None,
         len(online) if r.ssh_ok else None, r.error))
    for c in r.containers:
        store.db.execute("INSERT INTO containers(ts, server, name, type, up, status) VALUES (?,?,?,?,?,?)",
                         (r.ts, r.server, c["name"], c["type"], int(c["up"]), c["status"]))
    for p in r.peers:
        store.db.execute(
            "INSERT INTO peers(ts, server, container, pubkey, client, endpoint, handshake, rx, tx, online)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r.ts, r.server, p["container"], p["pubkey"], p["client"], p["endpoint"],
             p["handshake"], p["rx"], p["tx"], int(p["online"])))

    # --- доступность сервера
    up_now = r.ssh_ok
    was_up = prev.get("up")
    if was_up is not None and was_up != up_now:
        if up_now:
            ev("server_up", r.server, f"✅ {r.server}: сервер снова доступен")
        else:
            ev("server_down", r.server, f"🔴 {r.server}: сервер недоступен ({r.error or 'нет ответа'})")
    elif first and not up_now:
        ev("server_down", r.server, f"🔴 {r.server}: сервер недоступен ({r.error or 'нет ответа'})")

    new = {"ts": r.ts, "up": up_now, "cpu_raw": r.cpu_raw, "net": r.net,
           "containers": prev.get("containers", {}), "peers": prev.get("peers", {}),
           "cpu_alert": prev.get("cpu_alert", False), "mem_alert": prev.get("mem_alert", False)}

    if up_now:
        # --- контейнеры
        cur_c = {c["name"]: c["up"] for c in r.containers}
        prev_c = prev.get("containers", {})
        for name, is_up in cur_c.items():
            if name in prev_c and prev_c[name] != is_up:
                ev("container_" + ("up" if is_up else "down"), name,
                   f"{'✅' if is_up else '🔴'} {r.server}: контейнер {name} {'запущен' if is_up else 'остановлен'}")
            elif name not in prev_c and not first:
                ev("container_new", name, f"ℹ️ {r.server}: появился контейнер {name} "
                                          f"({'работает' if is_up else 'остановлен'})")
            elif first and not is_up:
                ev("container_down", name, f"🔴 {r.server}: контейнер {name} остановлен")
        for name in prev_c:
            if name not in cur_c:
                ev("container_gone", name, f"⚠️ {r.server}: контейнер {name} исчез")
        new["containers"] = cur_c

        # --- клиенты
        cur_p = {p["pubkey"]: {"online": p["online"], "client": p["client"], "c": p["container"]} for p in r.peers}
        prev_p = prev.get("peers", {})
        if not first:
            for pk, st in cur_p.items():
                was = prev_p.get(pk, {}).get("online", False)
                if st["online"] and not was:
                    peer = next(p for p in r.peers if p["pubkey"] == pk)
                    ep = f", {peer['endpoint'].rsplit(':', 1)[0]}" if peer["endpoint"] else ""
                    ev("client_connected", st["client"], f"🟢 {r.server}: подключился {st['client']} ({st['c']}{ep})")
                elif was and not st["online"]:
                    ev("client_disconnected", st["client"], f"⚪ {r.server}: отключился {st['client']} ({st['c']})")
        new["peers"] = cur_p

        # --- пороги
        if cpu is not None:
            was_hi = prev.get("cpu_alert", False)
            if not was_hi and cpu >= cfg.cpu_threshold:
                ev("cpu_high", r.server, f"🔥 {r.server}: высокая загрузка CPU {cpu:.0f}% (порог {cfg.cpu_threshold:.0f}%)")
                new["cpu_alert"] = True
            elif was_hi and cpu < cfg.cpu_threshold - 10:   # гистерезис 10 п.п.
                ev("cpu_ok", r.server, f"✅ {r.server}: CPU в норме ({cpu:.0f}%)")
                new["cpu_alert"] = False
        mp = r.mem.get("percent")
        if mp is not None:
            hi = mp >= cfg.mem_threshold
            if hi and not prev.get("mem_alert"):
                ev("mem_high", r.server, f"🔥 {r.server}: память занята на {mp:.0f}% (порог {cfg.mem_threshold:.0f}%)")
            elif not hi and prev.get("mem_alert"):
                ev("mem_ok", r.server, f"✅ {r.server}: память в норме ({mp:.0f}%)")
            new["mem_alert"] = hi

    # --- DNS: 24 ч, без повторной вставки тех же строк из ротированных файлов
    if up_now:
        new["dns_status"] = r.dns_status
        new["dns_note"] = r.dns_note
        new["dns_coverage_sec"] = r.dns_coverage_sec
        cutoff = r.ts - DNS_HISTORY_SEC
        store.db.execute("DELETE FROM dns WHERE server=? AND ts < ?", (r.server, cutoff))
        if r.dns:
            last = prev.get("dns_last")
            lines = [e["line"] for e in r.dns]
            start = 0
            if last and last in lines:
                start = len(lines) - lines[::-1].index(last)
            seen = set()
            for e in r.dns[start:]:
                if e["ts"] is not None and e["ts"] < cutoff:
                    continue
                key = hashlib.sha1(e["line"].encode("utf-8", "replace")).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                if store.db.execute("SELECT 1 FROM dns WHERE server=? AND line_key=?",
                                    (r.server, key)).fetchone():
                    continue
                store.db.execute(
                    "INSERT INTO dns(ts, server, client, ip, domain, site, qtype, line_key) VALUES (?,?,?,?,?,?,?,?)",
                    (e["ts"] or r.ts, r.server, e["client"], e["ip"], e["domain"],
                     root_domain(e["domain"]), e["qtype"], key))
            new["dns_last"] = lines[-1]
        else:
            new["dns_last"] = prev.get("dns_last")
    else:
        new["dns_status"] = prev.get("dns_status", "no_container")
        new["dns_note"] = prev.get("dns_note")
        new["dns_coverage_sec"] = prev.get("dns_coverage_sec")
        new["dns_last"] = prev.get("dns_last")

    store.set_state(key, new)
    store.commit()
    return events

# ---------------------------------------------------------------------------
# Уведомления
# ---------------------------------------------------------------------------

class Notifier:
    def __init__(self, cfg: Config, force_print: bool = False):
        self.token, self.chat = cfg.telegram_token, cfg.telegram_chat_id
        self.enabled = bool(self.token and self.chat) and not force_print

    def send(self, text: str):
        if not self.enabled:
            print(text)
            return
        import requests
        for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
            try:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                  json={"chat_id": self.chat, "text": chunk,
                                        "disable_web_page_preview": True}, timeout=15)
                if r.status_code != 200:
                    log.error("Telegram ответил %s", r.status_code)  # токен не логируем
                    print(text)
            except Exception as e:
                log.error("Ошибка отправки в Telegram: %s", type(e).__name__)
                print(text)

# ---------------------------------------------------------------------------
# Сводка
# ---------------------------------------------------------------------------

def build_summary(store: Store, cfg: Config, hours: float = 24, now: Optional[float] = None) -> str:
    now = now or time.time()
    since = now - hours * 3600
    db = store.db
    lines = [f"📊 Сводка AmneziaVPN за {hours:g} ч — {datetime.fromtimestamp(now).strftime('%d.%m.%Y %H:%M')}"]
    for srv in cfg.servers:
        n = srv.name
        last = db.execute("SELECT * FROM polls WHERE server=? ORDER BY ts DESC LIMIT 1", (n,)).fetchone()
        if not last:
            lines += ["", f"▪️ {n}: нет данных"]
            continue
        agg = db.execute(
            "SELECT COUNT(*) total, SUM(ssh_ok) ok, AVG(cpu_pct) cpu, MAX(cpu_pct) cpu_max, AVG(mem_pct) mem,"
            " MAX(peers_online) peak, AVG(rx_bps) rx, AVG(tx_bps) tx, AVG(tcp_ms) lat"
            " FROM polls WHERE server=? AND ts>=?", (n, since)).fetchone()
        avail = 100.0 * (agg["ok"] or 0) / agg["total"] if agg["total"] else 0
        status = "🟢 работает" if last["ssh_ok"] else "🔴 НЕДОСТУПЕН"
        lines += ["", f"▪️ {n} — {status}"]
        lines.append(f"   Доступность {avail:.1f}% ({agg['ok'] or 0}/{agg['total']} опросов), "
                     f"задержка SSH {agg['lat']:.0f} мс" if agg["lat"] is not None else
                     f"   Доступность {avail:.1f}% ({agg['ok'] or 0}/{agg['total']} опросов)")
        if last["ssh_ok"]:
            lines.append(f"   Аптайм {fmt_duration(last['uptime_s'])}, load {last['load1']}"
                         f" на {last['nproc']} ядр.")
            cpu_s = f"CPU сейчас {last['cpu_pct']:.0f}%" if last["cpu_pct"] is not None else "CPU сейчас —"
            if agg["cpu"] is not None:
                cpu_s += f" (сред. {agg['cpu']:.0f}%, макс. {agg['cpu_max']:.0f}%)"
            lines.append(f"   {cpu_s}; RAM {last['mem_pct']}% ({fmt_bytes(last['mem_used'])} из "
                         f"{fmt_bytes(last['mem_total'])})")
            lines.append(f"   Сеть сейчас ↓{fmt_bytes(last['rx_bps'], True)} ↑{fmt_bytes(last['tx_bps'], True)}"
                         f"; в среднем ↓{fmt_bytes(agg['rx'], True)} ↑{fmt_bytes(agg['tx'], True)}")
            cs = db.execute("SELECT name, up FROM containers WHERE server=? AND ts=? ORDER BY name",
                            (n, last["ts"])).fetchall()
            if cs:
                lines.append("   Контейнеры: " + ", ".join(f"{c['name']} {'✅' if c['up'] else '❌'}" for c in cs))
            ps = db.execute("SELECT * FROM peers WHERE server=? AND ts=? ORDER BY online DESC, client",
                            (n, last["ts"])).fetchall()
            on = [p for p in ps if p["online"]]
            lines.append(f"   Клиенты онлайн: {len(on)} из {len(ps)} (пик за период {agg['peak'] or 0})")
            for p in ps:
                if p["online"]:
                    lines.append(f"     🟢 {p['client']} — {fmt_duration(now - p['handshake'])} назад, "
                                 f"трафик ↓{fmt_bytes(p['tx'])} ↑{fmt_bytes(p['rx'])}")
                else:
                    seen = f"был {fmt_duration(now - p['handshake'])} назад" if p["handshake"] else "ни разу не подключался"
                    lines.append(f"     ⚪ {p['client']} — {seen}")
        elif last["error"]:
            lines.append(f"   Причина: {last['error']}")
    evs = db.execute("SELECT ts, message FROM events WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    lines += ["", f"🔔 События за период: {len(evs)}"]
    for e in evs[-15:]:
        lines.append(f"   {datetime.fromtimestamp(e['ts']).strftime('%d.%m %H:%M')} {e['message']}")
    if len(evs) > 15:
        lines.append(f"   … и ещё {len(evs) - 15}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def do_check(cfg: Config, store: Store, notifier: Notifier, runner_factory=None, probe=None, now=None) -> list:
    runner_factory = runner_factory or SSHRunner
    all_events = []
    from concurrent.futures import ThreadPoolExecutor
    ts = now or time.time()
    with ThreadPoolExecutor(max_workers=min(8, len(cfg.servers))) as pool:   # серверы опрашиваются параллельно
        results = list(pool.map(lambda s: collect(s, runner_factory, ts, cfg.clients,
                                                  cfg.handshake_online_sec, probe), cfg.servers))
    for srv, r in zip(cfg.servers, results):
        log.info("%s: ssh_ok=%s peers=%d/%d err=%s", srv.safe(), r.ssh_ok,
                 sum(p["online"] for p in r.peers), len(r.peers), r.error)
        all_events += process(store, cfg, r)
    if all_events:
        notifier.send("\n".join(all_events))
    return all_events


def maybe_daily(cfg: Config, store: Store, notifier: Notifier):
    if cfg.daily_summary_hour is None:
        return
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour >= int(cfg.daily_summary_hour) and store.get_state("daily_sent") != today:
        notifier.send(build_summary(store, cfg, 24))
        store.set_state("daily_sent", today)
        store.commit()


def run_dashboard(args) -> int:
    import dashboard
    import dashboard_api
    if args.demo:
        import demo_data
        db_path, cfg, now = demo_data.demo_store(args.db)
        print("ДЕМО-РЕЖИМ: панель показывает тестовые данные.")
    else:
        try:
            cfg = load_config(args.config)
        except ConfigError as e:
            print(f"Ошибка конфигурации: {e}", file=sys.stderr)
            return 2
        db_path = args.db or cfg.db_path
        Store(db_path).db.close()          # создать схему, если базы ещё нет
    if args.export_html:
        st = Store(db_path)
        snap = dashboard_api.api_snapshot(st, cfg)
        Path(args.export_html).write_text(dashboard.render_html(snap, demo=args.demo), encoding="utf-8")
        print(f"Сохранено: {args.export_html}")
        return 0
    bg = None
    if args.with_monitor and not args.demo:
        def bg():
            store, notifier = Store(db_path), Notifier(cfg)
            while True:
                t0 = time.time()
                try:
                    do_check(cfg, store, notifier)
                    maybe_daily(cfg, store, notifier)
                except Exception as e:
                    log.exception("Ошибка цикла: %s", e)
                time.sleep(max(1, cfg.interval - (time.time() - t0)))
    dashboard.serve(lambda: Store(db_path), cfg, args.host, args.port, bg, demo=args.demo)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Мониторинг серверов AmneziaVPN (только чтение)")
    ap.add_argument("-c", "--config", default=os.environ.get("AMNEZIA_MONITOR_CONFIG", "servers.yaml"))
    ap.add_argument("--db", help="путь к SQLite (по умолчанию из конфига)")
    ap.add_argument("--demo", "--dry-run", dest="demo", action="store_true",
                    help="встроенные тестовые данные, без подключения к серверам и без Telegram")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="один проход опроса")
    sub.add_parser("run", help="цикл опроса с интервалом из конфига")
    sp = sub.add_parser("summary", help="сводка по истории")
    sp.add_argument("--hours", type=float, default=24)
    sp.add_argument("--send", action="store_true", help="отправить в Telegram (если настроен)")
    dp = sub.add_parser("dashboard", help="веб-панель (по умолчанию http://127.0.0.1:8765)")
    dp.add_argument("--host", default="127.0.0.1", help="адрес; 0.0.0.0 — доступ из сети (без пароля!)")
    dp.add_argument("--port", type=int, default=8765)
    dp.add_argument("--with-monitor", action="store_true", help="заодно опрашивать серверы (как run) в фоне")
    dp.add_argument("--export-html", metavar="FILE", help="сохранить статичный HTML с данными и выйти")
    app = sub.add_parser("app", help="режим приложения: опрос + панель + настройки; на Windows — своё окно")
    import app_main
    app_main.add_args(app)
    args = ap.parse_args(argv)
    if args.cmd == "app":
        return app_main.run(args)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "dashboard":
        return run_dashboard(args)
    if args.demo:
        import demo_data
        return demo_data.run_demo(args)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        return 2
    store = Store(args.db or cfg.db_path)
    notifier = Notifier(cfg)

    if args.cmd == "check":
        evs = do_check(cfg, store, notifier)
        if not evs:
            print("Опрос выполнен, изменений нет.")
        return 0
    if args.cmd == "summary":
        text = build_summary(store, cfg, args.hours)
        if args.send:
            notifier.send(text)
        else:
            print(text)
        return 0
    if args.cmd == "run":
        print(f"Мониторинг запущен: {len(cfg.servers)} серв., интервал {cfg.interval} с. Ctrl+C — выход.")
        while True:
            t0 = time.time()
            try:
                do_check(cfg, store, notifier)
                maybe_daily(cfg, store, notifier)
            except Exception as e:
                log.exception("Ошибка цикла: %s", e)
            time.sleep(max(1, cfg.interval - (time.time() - t0)))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
