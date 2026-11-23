# -*- coding: utf-8 -*-
"""Проверка блокировок: доступны ли порты сервера из России и из-за рубежа.

Только внешние проверки. На сервер программа здесь не входит и ничего на нём не меняет:
  * с этого ПК (обычно он в России): TCP-подключение, для SSH — чтение баннера и обмен версиями
    (до входа, без пароля), для портов Xray/Reality — TLS-рукопожатие (ClientHello);
  * с узлов check-host.net в России и в других странах: check-ping и check-tcp (публичный API).

Признак ТСПУ/DPI: TCP-соединение устанавливается, а потом обмен данными обрывается сбросом (RST)
или замирает (таймаут), при этом из-за рубежа порт отвечает нормально.

UDP (AmneziaWG/WireGuard) снаружи честно не проверить: сервер молчит на пакеты без ключа клиента,
а check-host проверяет только TCP. Для UDP показывается косвенный признак — сколько клиентов AWG
были на связи при последнем опросе.
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

log = logging.getLogger("amnezia_monitor.blockcheck")

APP_VERSION = "1.3.0"
COOLDOWN_SEC = 30 * 60              # результат кешируется на 30 минут
RUN_STALE_SEC = 10 * 60             # «идёт проверка» дольше этого — считаем зависшей
CHECKHOST_BACKOFF_SEC = 15 * 60     # после limit_exceeded check-host не трогаем 15 минут
RECHECK_DELAY_SEC = 150             # плохой результат подтверждаем повторной проверкой через 2,5 минуты
LOCAL_DEBOUNCE_SEC = 10             # ручная проверка с этого ПК — не чаще раза в 10 секунд
BAD_VERDICTS = {"blocked_ru", "down", "partial"}
MAX_RU_NODES = 11
MAX_WORLD_NODES = 11
# Страны со своей цензурой не годятся как «мир»: их провалы ничего не говорят о РФ.
EXCLUDED_WORLD = {"ru", "by", "ir", "cn", "tm", "kp"}
WORLD_PREFERENCE = ["de", "nl", "fi", "se", "pl", "fr", "gb", "ch", "at", "us", "cz", "lt", "lv", "ee",
                    "it", "es", "pt", "no", "dk", "ro", "bg", "hu", "rs", "md", "tr", "jp", "sg", "hk", "ca"]
DEFAULT_SNI = "www.googletagmanager.com"   # маскировочный сайт Reality по умолчанию в Amnezia

# --------------------------------------------------------------------------- статусы локальных проверок
LOCAL_OK = {"ok", "tls_alert", "not_ssh"}
LOCAL_DPI = {"reset_after_connect", "silent_after_connect", "closed_after_connect",
             "reset_after_banner", "timeout_after_banner", "closed_after_banner"}
LOCAL_CONN_FAIL = {"refused", "timeout", "unreachable"}

LOCAL_TEXT = {
    "ok": "доступен",
    "tls_alert": "сервер ответил TLS-ошибкой — порт живой",
    "not_ssh": "порт ответил, но не SSH-баннером",
    "refused": "соединение отклонено (RST в ответ на подключение)",
    "timeout": "нет ответа на подключение (таймаут)",
    "unreachable": "сеть до сервера недоступна",
    "reset_after_connect": "TCP подключился, затем сброс соединения (RST)",
    "silent_after_connect": "TCP подключился, затем тишина (таймаут обмена)",
    "closed_after_connect": "TCP подключился, затем соединение закрыто",
    "reset_after_banner": "баннер SSH получен, после обмена версиями — сброс (RST)",
    "timeout_after_banner": "баннер SSH получен, после обмена версиями — тишина",
    "closed_after_banner": "баннер SSH получен, после обмена версиями соединение закрыто",
    "error": "ошибка проверки",
    "skipped": "не проверялся",
}

KIND_LABEL = {"ssh": "SSH", "xray": "Xray/Reality", "awg": "AmneziaWG", "wireguard": "WireGuard",
              "openvpn": "OpenVPN", "cloak": "Cloak", "shadowsocks": "Shadowsocks", "ipsec": "IPsec",
              "tor": "Tor", "manual": "порт"}

VERDICT_TITLE = {
    "ok": "Доступен везде",
    "blocked_ru": "Признаки блокировки в РФ",
    "down": "Недоступен отовсюду",
    "partial": "Частично доступен",
    "unknown": "Недостаточно данных",
    "unverifiable": "Не проверяется извне",
}
# Чем больше — тем важнее показать на баннере сервера.
SEVERITY = {"unverifiable": 0, "ok": 1, "unknown": 2, "partial": 3, "down": 4, "blocked_ru": 5}


def _classify_os_error(e: BaseException) -> str:
    if isinstance(e, ConnectionRefusedError):
        return "refused"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(e, ConnectionResetError):
        return "reset"
    return "unreachable"


def _connect(host: str, port: int, timeout: float):
    t0 = time.monotonic()
    sock = socket.create_connection((host, port), timeout=timeout)
    return sock, round((time.monotonic() - t0) * 1000, 1)


def probe_tcp(host: str, port: int, timeout: float = 6.0) -> dict:
    """Только TCP-подключение."""
    try:
        sock, ms = _connect(host, port, timeout)
    except OSError as e:
        st = _classify_os_error(e)
        return {"status": "refused" if st == "reset" else st, "ms": None}
    sock.close()
    return {"status": "ok", "ms": ms, "detail": "TCP-соединение установлено"}


def probe_ssh(host: str, port: int, timeout: float = 6.0, banner_timeout: float = 8.0) -> dict:
    """TCP + баннер сервера + наш баннер + первые байты обмена ключами. Вход не выполняется."""
    try:
        sock, ms = _connect(host, port, timeout)
    except OSError as e:
        st = _classify_os_error(e)
        return {"status": "refused" if st == "reset" else st, "ms": None}
    try:
        sock.settimeout(banner_timeout)
        buf = b""
        try:
            while b"\n" not in buf and len(buf) < 512:
                chunk = sock.recv(256)
                if not chunk:
                    return {"status": "closed_after_connect", "ms": ms}
                buf += chunk
        except ConnectionResetError:
            return {"status": "reset_after_connect", "ms": ms}
        except (socket.timeout, TimeoutError):
            return {"status": "silent_after_connect", "ms": ms}
        except OSError:
            return {"status": "reset_after_connect", "ms": ms}
        line = buf.split(b"\n", 1)[0].strip().decode("ascii", "replace")
        if not line.startswith("SSH-"):
            return {"status": "not_ssh", "ms": ms, "detail": "ответ не похож на SSH"}
        banner = line[:80]
        # Обмен версиями: ТСПУ часто рвёт соединение именно здесь.
        try:
            sock.sendall(b"SSH-2.0-AmneziaMonitor_probe\r\n")
            rest = buf.split(b"\n", 1)[1] if b"\n" in buf else b""
            if not rest:          # ждём начала обмена ключами (KEXINIT) от сервера
                rest = sock.recv(64)
                if not rest:
                    return {"status": "closed_after_banner", "ms": ms, "banner": banner}
            else:                 # KEXINIT уже пришёл: коротко ждём, не оборвут ли нас после нашего баннера
                sock.settimeout(2.0)
                try:
                    if sock.recv(64) == b"":
                        return {"status": "closed_after_banner", "ms": ms, "banner": banner}
                except (socket.timeout, TimeoutError):
                    pass
        except ConnectionResetError:
            return {"status": "reset_after_banner", "ms": ms, "banner": banner}
        except (socket.timeout, TimeoutError):
            return {"status": "timeout_after_banner", "ms": ms, "banner": banner}
        except OSError:
            return {"status": "reset_after_banner", "ms": ms, "banner": banner}
        return {"status": "ok", "ms": ms, "banner": banner, "detail": "SSH-баннер: " + banner}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def probe_tls(host: str, port: int, sni: Optional[str] = None, timeout: float = 6.0,
              hs_timeout: float = 8.0) -> dict:
    """TCP + TLS ClientHello. Сертификат не проверяется: важно только, ответил ли сервер."""
    try:
        sock, ms = _connect(host, port, timeout)
    except OSError as e:
        st = _classify_os_error(e)
        return {"status": "refused" if st == "reset" else st, "ms": None}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock.settimeout(hs_timeout)
    try:
        tls = ctx.wrap_socket(sock, server_hostname=sni or DEFAULT_SNI, do_handshake_on_connect=False)
    except OSError:
        sock.close()
        return {"status": "error", "ms": ms}
    try:
        tls.do_handshake()
        ver = tls.version() or "TLS"
        return {"status": "ok", "ms": ms, "detail": f"TLS-рукопожатие прошло ({ver})"}
    except (ssl.SSLEOFError, ssl.SSLZeroReturnError):
        return {"status": "closed_after_connect", "ms": ms}
    except ssl.SSLError as e:
        return {"status": "tls_alert", "ms": ms, "detail": "TLS-ошибка: " + (e.reason or type(e).__name__)}
    except ConnectionResetError:
        return {"status": "reset_after_connect", "ms": ms}
    except (socket.timeout, TimeoutError):
        return {"status": "silent_after_connect", "ms": ms}
    except OSError:
        return {"status": "reset_after_connect", "ms": ms}
    finally:
        try:
            tls.close()
        except OSError:
            pass


def local_probe(target: dict, host: str, sni: Optional[str] = None) -> dict:
    kind = target.get("probe")
    try:
        if kind == "ssh":
            r = probe_ssh(host, target["port"])
        elif kind == "tls":
            r = probe_tls(host, target["port"], sni)
        elif kind == "tcp":
            r = probe_tcp(host, target["port"])
        else:
            return {"status": "skipped", "ms": None, "detail": LOCAL_TEXT["skipped"]}
    except Exception as e:  # pragma: no cover — сеть может преподнести что угодно
        r = {"status": "error", "ms": None, "detail": type(e).__name__}
    r.setdefault("detail", LOCAL_TEXT.get(r["status"], r["status"]))
    return r

# --------------------------------------------------------------------------- check-host.net


class CheckHostError(Exception):
    pass


class CheckHostLimited(CheckHostError):
    pass


def _requests_get(url: str, params, timeout: float):
    import requests
    r = requests.get(url, params=params, timeout=timeout,
                     headers={"Accept": "application/json", "User-Agent": f"AmneziaMonitor/{APP_VERSION}"})
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body


class CheckHost:
    """Мини-клиент публичного API check-host.net с паузой между запросами."""
    BASE = "https://check-host.net"
    _nodes_cache: dict = {}
    _nodes_ts: float = 0.0
    _gate = threading.Lock()
    _last_call = 0.0

    def __init__(self, http_get: Callable = None, sleep: Callable = time.sleep, min_gap: float = 1.5,
                 clock: Callable = time.monotonic):
        self.http_get = http_get or _requests_get
        self.sleep, self.min_gap, self.clock = sleep, min_gap, clock

    def _get(self, path: str, params=None) -> dict:
        with CheckHost._gate:
            wait = CheckHost._last_call + self.min_gap - self.clock()
            if wait > 0:
                self.sleep(wait)
            CheckHost._last_call = self.clock()
        try:
            status, body = self.http_get(self.BASE + path, params, 20)
        except Exception as e:
            raise CheckHostError(f"check-host.net недоступен ({type(e).__name__})")
        if status == 429 or (isinstance(body, dict) and body.get("error") == "limit_exceeded"):
            raise CheckHostLimited("check-host.net ограничил частоту запросов")
        if status != 200 or not isinstance(body, dict):
            raise CheckHostError(f"check-host.net ответил кодом {status}")
        if body.get("error"):
            raise CheckHostError("check-host.net: " + str(body["error"])[:80])
        return body

    def nodes(self) -> dict:
        if CheckHost._nodes_cache and time.time() - CheckHost._nodes_ts < 6 * 3600:
            return CheckHost._nodes_cache
        body = self._get("/nodes/hosts")
        out = {}
        for name, v in (body.get("nodes") or {}).items():
            loc = (v or {}).get("location") or []
            if len(loc) >= 1:
                out[name] = {"cc": str(loc[0]).lower(), "country": loc[1] if len(loc) > 1 else "",
                             "city": loc[2] if len(loc) > 2 else ""}
        if not out:
            raise CheckHostError("check-host.net не вернул список узлов")
        CheckHost._nodes_cache, CheckHost._nodes_ts = out, time.time()
        return out

    def start(self, kind: str, host: str, nodes: list) -> dict:
        body = self._get(f"/check-{kind}", [("host", host)] + [("node", n) for n in nodes])
        if not body.get("request_id"):
            raise CheckHostError("check-host.net не принял проверку")
        return body

    def result(self, request_id: str) -> dict:
        return self._get(f"/check-result/{request_id}")

    def wait(self, request_ids: list, deadline: float = 30.0, poll: float = 3.0) -> dict:
        """Опрашивает результаты, пока все узлы не ответят или не выйдет время."""
        done, t_end = {}, self.clock() + deadline
        pending = list(request_ids)
        self.sleep(poll)
        while pending:
            for rid in list(pending):
                res = self.result(rid)
                done[rid] = res
                if res and all(v is not None for v in res.values()):
                    pending.remove(rid)
            if not pending or self.clock() >= t_end:
                break
            self.sleep(poll)
        return done


def pick_nodes(nodes: dict, max_ru: int = MAX_RU_NODES, max_world: int = MAX_WORLD_NODES) -> tuple:
    ru = sorted(n for n, v in nodes.items() if v["cc"] == "ru")[:max_ru]
    pool = sorted((n for n, v in nodes.items() if v["cc"] not in EXCLUDED_WORLD),
                  key=lambda n: (WORLD_PREFERENCE.index(nodes[n]["cc"]) if nodes[n]["cc"] in WORLD_PREFERENCE
                                 else len(WORLD_PREFERENCE), n))
    world, used = [], set()
    for n in pool:                      # сначала по одному узлу на страну
        if nodes[n]["cc"] not in used and len(world) < max_world:
            world.append(n)
            used.add(nodes[n]["cc"])
    for n in pool:
        if n not in world and len(world) < max_world:
            world.append(n)
    return ru, world


def parse_tcp_result(value) -> Optional[dict]:
    """Результат check-tcp одного узла. None — узел ещё не ответил."""
    if value is None:
        return None
    item = value[0] if isinstance(value, list) and value else value
    if not isinstance(item, dict):
        return {"ok": False, "status": "error", "error": "нет ответа узла"}
    if "time" in item and item.get("time") is not None:
        return {"ok": True, "status": "ok", "ms": round(float(item["time"]) * 1000, 1)}
    err = str(item.get("error") or "ошибка")
    low = err.lower()
    st = "timeout" if "timed out" in low or "timeout" in low else "refused" if "refused" in low else \
        "reset" if "reset" in low else "error"
    return {"ok": False, "status": st, "error": err[:80]}


def parse_ping_result(value) -> Optional[dict]:
    if value is None:
        return None
    try:
        pings = value[0] if isinstance(value, list) else None
    except (IndexError, TypeError):
        pings = None
    if not pings or pings == [None]:
        return {"found": False, "ms": None, "ok": 0, "n": 0}
    oks = [p[1] for p in pings if isinstance(p, list) and p and p[0] == "OK" and len(p) > 1]
    return {"found": bool(oks), "ok": len(oks), "n": len(pings),
            "ms": round(sum(oks) / len(oks) * 1000, 1) if oks else None}

# --------------------------------------------------------------------------- вердикт


def _group(nodes: list) -> dict:
    got = [n for n in nodes if n.get("ok") is not None]
    ok = [n for n in got if n["ok"]]
    ms = [n["ms"] for n in ok if n.get("ms") is not None]
    return {"n": len(got), "ok": len(ok), "ms": round(sum(ms) / len(ms), 1) if ms else None}


def port_verdict(proto: str, local: Optional[dict], ru_nodes: list, world_nodes: list,
                 label: str = "порт") -> dict:
    """Чистая функция: вердикт по одному порту.

    proto — tcp/tls/udp; local — {"status": …} проверки с этого ПК (или None);
    ru_nodes / world_nodes — [{"ok": True/False/None}] узлов check-host.
    -> {"verdict", "cause" (dpi/ip/isp для blocked_ru), "title", "detail"}."""
    if proto == "udp":
        return {"verdict": "unverifiable", "cause": None, "title": VERDICT_TITLE["unverifiable"],
                "detail": "UDP: сервер молчит на пакеты без ключа клиента, а check-host проверяет только TCP"}
    ru, w = _group(ru_nodes), _group(world_nodes)
    ls = (local or {}).get("status")
    local_ok, local_dpi, local_fail = ls in LOCAL_OK, ls in LOCAL_DPI, ls in LOCAL_CONN_FAIL
    world_up = w["n"] > 0 and w["ok"] * 2 >= w["n"]
    world_down = w["n"] > 0 and w["ok"] == 0
    ru_up = ru["n"] > 0 and ru["ok"] * 2 >= ru["n"]
    ru_down = ru["n"] > 0 and ru["ok"] == 0

    def out(verdict, kind, detail):
        title = VERDICT_TITLE[verdict]
        if verdict == "blocked_ru":
            title = {"dpi": f"Признаки блокировки {label} в РФ (ТСПУ/DPI сброс соединения)",
                     "ip": f"Признаки блокировки {label} в РФ (порт или IP недоступен)",
                     "isp": f"{label} не открывается с этого ПК (провайдер или локальная сеть)"}.get(kind, title)
        return {"verdict": verdict, "cause": kind, "title": title, "detail": detail}

    if w["n"] == 0:      # из-за рубежа данных нет (check-host недоступен)
        if local_dpi:
            return out("blocked_ru", "dpi", "С этого ПК TCP подключается, но обмен обрывается. "
                                            "Из-за рубежа не проверено — уверенность низкая.")
        if local_ok and not ru_down:
            return out("ok", None, "Доступен с этого ПК" + (" и с узлов в РФ" if ru_up else "")
                       + ". Из-за рубежа не проверено.")
        return out("unknown", None, "Нет данных из-за рубежа, сравнить не с чем.")
    if world_up:
        if local_dpi:
            extra = (" Узлы check-host проверяют только TCP-подключение, поэтому сброс после подключения "
                     "виден только с этого ПК." if ru_up else "")
            return out("blocked_ru", "dpi", "Из-за рубежа порт отвечает, а с этого ПК TCP подключается и "
                                            "сразу обрывается — типичная картина ТСПУ/DPI." + extra)
        if local_fail and (ru_down or ru["n"] == 0):
            return out("blocked_ru", "ip", "Из-за рубежа порт отвечает, а из России подключиться нельзя.")
        if local_fail and ru_up:
            return out("blocked_ru", "isp", "Узлы в РФ подключаются, а с этого ПК — нет. Похоже на блокировку "
                                            "у вашего провайдера или в локальной сети.")
        if ru_down and not local_ok:
            return out("blocked_ru", "ip", "Из-за рубежа порт отвечает, а с узлов в РФ — нет.")
        if ru_down and local_ok:
            return out("partial", None, "С этого ПК и из-за рубежа доступен, а узлы check-host в РФ не "
                                        "подключаются (возможна блокировка у их провайдеров).")
        if ru["n"] > 0 and not ru_up:
            return out("partial", None, f"Из РФ подключились только {ru['ok']} из {ru['n']} узлов.")
        if w["ok"] < w["n"]:
            return out("ok", None, f"Доступен; из-за рубежа ответили {w['ok']} из {w['n']} узлов.")
        return out("ok", None, "Порт отвечает и из России, и из-за рубежа.")
    if world_down:
        if local_ok or ru["ok"] > 0:
            return out("partial", None, "Из России порт отвечает, а из-за рубежа — нет "
                                        "(возможно, фильтр на самом сервере или у хостера).")
        return out("down", None, "Порт не отвечает ни из России, ни из-за рубежа: сервер выключен, "
                                 "служба не запущена или порт закрыт.")
    return out("partial", None, f"Из-за рубежа ответили только {w['ok']} из {w['n']} узлов.")


def server_verdict(ports: list) -> dict:
    """Итог по серверу из вердиктов портов: блокировка важнее всего, затем «недоступен» и т. д."""
    checked = [p for p in ports if p["verdict"] != "unverifiable"]
    if not checked:
        return {"verdict": "unverifiable", "title": VERDICT_TITLE["unverifiable"], "ports": []}
    verdicts = {p["verdict"] for p in checked}
    blocked = [p for p in checked if p["verdict"] == "blocked_ru"]
    if blocked:
        main = blocked[0]
        names = ", ".join(f"{p['label']}:{p['port']}" for p in blocked)
        return {"verdict": "blocked_ru", "title": main["title"] if len(blocked) == 1 else
                f"Признаки блокировки в РФ: {names}", "ports": [p["port"] for p in blocked]}
    if len(verdicts) == 1:
        v = verdicts.pop()
        return {"verdict": v, "title": VERDICT_TITLE[v], "ports": [p["port"] for p in checked]}
    bad = [p for p in checked if p["verdict"] in ("down", "partial")]
    if bad:
        names = ", ".join(f"{p['label']}:{p['port']}" for p in bad)
        return {"verdict": "partial", "title": f"Частично доступен — проблемы: {names}",
                "ports": [p["port"] for p in bad]}
    # ok + unknown
    return {"verdict": "ok", "title": VERDICT_TITLE["ok"] + " (часть портов без данных)", "ports": []}

# --------------------------------------------------------------------------- цели проверки


def port_key(t: dict) -> str:
    return f"{int(t['port'])}/{'udp' if t.get('proto') == 'udp' else 'tcp'}"


def targets_for(srv, discovered: Optional[list]) -> list:
    """SSH-порт сервера + порты VPN, уже известные из опроса (docker ps / wg dump) + заданные вручную."""
    out = [{"port": int(srv.port), "proto": "tcp", "kind": "ssh", "probe": "ssh", "label": "SSH",
            "source": "ssh"}]
    seen = {(int(srv.port), "tcp")}
    for d in discovered or []:
        proto = d.get("proto", "tcp")
        key = (int(d["port"]), "udp" if proto == "udp" else "tcp")
        if key in seen:
            continue
        seen.add(key)
        kind = d.get("type") or "manual"
        probe = None if proto == "udp" else ("tls" if kind == "xray" else "tcp")
        out.append({"port": key[0], "proto": "udp" if proto == "udp" else ("tls" if probe == "tls" else "tcp"),
                    "kind": kind, "probe": probe, "label": KIND_LABEL.get(kind, kind),
                    "container": d.get("container"), "source": "poll"})
    for port, proto in getattr(srv, "block_ports", None) or []:
        key = (int(port), "udp" if proto == "udp" else "tcp")
        if key in seen:
            continue
        seen.add(key)
        probe = None if proto == "udp" else ("tls" if proto == "tls" else "tcp")
        label = "UDP" if proto == "udp" else ("TLS" if proto == "tls" else "TCP")
        out.append({"port": key[0], "proto": proto, "kind": "manual", "probe": probe, "label": label,
                    "source": "manual"})
    return out

# --------------------------------------------------------------------------- проверка сервера


def _awg_evidence(store, server: str) -> Optional[dict]:
    try:
        row = store.db.execute("SELECT MAX(ts) FROM peers WHERE server=?", (server,)).fetchone()
        if not row or row[0] is None:
            return None
        ts = row[0]
        r = store.db.execute("SELECT COUNT(*) n, SUM(online) o FROM peers WHERE server=? AND ts=?",
                             (server, ts)).fetchone()
        return {"ts": ts, "peers": r["n"] or 0, "online": r["o"] or 0}
    except Exception:
        return None


def check_server(srv, discovered: Optional[list], checkhost: Optional[CheckHost] = None,
                 local: Callable = local_probe, now: Optional[float] = None,
                 checkhost_note: Optional[str] = None, awg: Optional[dict] = None,
                 only: Optional[set] = None, prior: Optional[dict] = None) -> dict:
    """Полная проверка одного сервера. checkhost=None — только с этого ПК (с пояснением checkhost_note).

    only — множество ключей "порт/proto": проверить только эти порты (повторная проверка);
    prior — {ключ: {"ru": [...], "world": [...]}} — данные узлов check-host прошлой проверки,
    берутся, если check-host сейчас не опрашивался (пауза из-за лимита или ошибка)."""
    t0 = time.time()
    now = now or t0
    targets = targets_for(srv, discovered)
    if only is not None:
        targets = [t for t in targets if port_key(t) in only]
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(targets)))) as pool:
        locals_ = list(pool.map(lambda t: local(t, srv.host, getattr(srv, "block_sni", None)), targets))

    ch = {"ok": False, "error": checkhost_note, "links": [], "ru_nodes": 0, "world_nodes": 0}
    ping = {"ru": [], "world": []}
    node_res = [{"ru": [], "world": []} for _ in targets]
    if checkhost is not None:
        try:
            nodes = checkhost.nodes()
            ru_n, world_n = pick_nodes(nodes)
            all_n = ru_n + world_n
            ch.update({"ru_nodes": len(ru_n), "world_nodes": len(world_n)})
            reqs = {}
            p = checkhost.start("ping", srv.host, all_n)
            reqs["ping"] = p["request_id"]
            ch["links"].append(p.get("permanent_link"))
            for i, t in enumerate(targets):
                if t["proto"] == "udp":
                    continue
                r = checkhost.start("tcp", f"{srv.host}:{t['port']}", all_n)
                reqs[i] = r["request_id"]
                ch["links"].append(r.get("permanent_link"))
            results = checkhost.wait(list(reqs.values()))

            def meta(n):
                m = nodes.get(n, {})
                return {"node": n.split(".")[0], "cc": m.get("cc", ""), "country": m.get("country", ""),
                        "city": m.get("city", "")}
            pr = results.get(reqs["ping"], {}) or {}
            for grp, lst in (("ru", ru_n), ("world", world_n)):
                for n in lst:
                    v = parse_ping_result(pr.get(n))
                    ping[grp].append(dict(meta(n), **(v or {"found": None, "ms": None})))
            for i, rid in reqs.items():
                if i == "ping":
                    continue
                res = results.get(rid, {}) or {}
                for grp, lst in (("ru", ru_n), ("world", world_n)):
                    for n in lst:
                        v = parse_tcp_result(res.get(n))
                        node_res[i][grp].append(dict(meta(n), **(v or {"ok": None, "status": "pending"})))
            ch["ok"], ch["error"] = True, None
        except CheckHostLimited as e:
            ch["error"], ch["limited"] = str(e) + " — проверены только порты с этого ПК", True
        except CheckHostError as e:
            ch["error"] = str(e) + " — проверены только порты с этого ПК"
        except Exception as e:  # pragma: no cover
            log.exception("check-host")
            ch["error"] = f"ошибка check-host ({type(e).__name__}) — проверены только порты с этого ПК"

    ports = []
    for t, loc, nr in zip(targets, locals_, node_res):
        if not ch["ok"] and prior and port_key(t) in prior and t["proto"] != "udp":
            nr = {"ru": prior[port_key(t)].get("ru") or [], "world": prior[port_key(t)].get("world") or []}
            t = dict(t, nodes_from_prior=True)
        v = port_verdict(t["proto"], loc if t["probe"] else None, nr["ru"], nr["world"], t["label"])
        if t["proto"] == "udp" and awg and awg.get("peers"):
            if awg["online"]:
                v["detail"] += (f". При последнем удачном опросе на связи были {awg['online']} из {awg['peers']} "
                                "клиентов — у них UDP-порт проходит")
            else:
                v["detail"] += (". При последнем удачном опросе на связи никого не было — "
                                "по этим данным не понять, проходит ли UDP")
        ports.append(dict(t, local=loc, ru=nr["ru"], world=nr["world"],
                          summary={"ru": _group(nr["ru"]), "world": _group(nr["world"])}, **v))
    sv = server_verdict(ports)

    def ping_sum(lst):
        got = [x for x in lst if x.get("found") is not None]
        ms = [x["ms"] for x in got if x.get("ms") is not None]
        return {"n": len(got), "found": sum(1 for x in got if x["found"]),
                "ms": round(sum(ms) / len(ms), 1) if ms else None}
    return {"server": srv.name, "host": srv.host, "ts": now, "duration": round(time.time() - t0, 1),
            "verdict": sv["verdict"], "title": sv["title"], "ports": ports,
            "ping": {"ru": ping["ru"], "world": ping["world"],
                     "summary": {"ru": ping_sum(ping["ru"]), "world": ping_sum(ping["world"])}},
            "checkhost": ch, "awg": awg}

# --------------------------------------------------------------------------- хранение и запуск


def latest(store, server: str) -> Optional[dict]:
    r = store.db.execute("SELECT ts, data FROM block_checks WHERE server=? ORDER BY ts DESC LIMIT 1",
                         (server,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["data"])
    except ValueError:
        return None


def history(store, server: str, limit: int = 20) -> list:
    return [{"ts": r["ts"], "verdict": r["verdict"]} for r in store.db.execute(
        "SELECT ts, verdict FROM block_checks WHERE server=? ORDER BY ts DESC LIMIT ?", (server, limit))]


def change_message(server: str, prev: Optional[str], cur: dict) -> Optional[str]:
    """Текст события при смене вердикта; None — сообщать не о чем."""
    v = cur["verdict"]
    if prev == v or v in ("unknown", "unverifiable"):
        return None
    if prev is None and v == "ok":
        return None
    icon = {"ok": "✅", "blocked_ru": "🚫", "down": "🔴", "partial": "⚠️"}.get(v, "ℹ️")
    suffix = " (подтверждено повторной проверкой)" if (cur.get("recheck") or {}).get("confirmed") else ""
    return f"{icon} {server}: блокировки — {cur['title']}{suffix}"


def save_result(store, res: dict, keep_days: int = 30) -> Optional[str]:
    prev = store.db.execute("SELECT verdict FROM block_checks WHERE server=? ORDER BY ts DESC LIMIT 1",
                            (res["server"],)).fetchone()
    store.db.execute("INSERT INTO block_checks(ts, server, verdict, data) VALUES (?,?,?,?)",
                     (res["ts"], res["server"], res["verdict"], json.dumps(res, ensure_ascii=False)))
    store.db.execute("DELETE FROM block_checks WHERE server=? AND ts < ?",
                     (res["server"], res["ts"] - keep_days * 86400))
    msg = change_message(res["server"], prev["verdict"] if prev else None, res)
    if msg:
        store.add_event(res["ts"], res["server"], "block_change", "blocks", msg)
    store.commit()
    return msg

# --------------------------------------------------------------------------- повторная проверка


def needs_recheck(res: dict) -> list:
    """Ключи портов с плохим вердиктом (blocked/dpi/ip/isp, partial, down) — их надо перепроверить."""
    return [port_key(p) for p in res.get("ports") or [] if p.get("verdict") in BAD_VERDICTS]


def merge_recheck(first: dict, second: dict, now: float) -> dict:
    """Итог после повторной проверки.

    Порт, который снова плохой, — подтверждён (берутся свежие данные повторной проверки).
    Порт, который во второй раз в порядке, — кратковременный сбой: его вердикт берётся из повторной
    проверки, а первый плохой результат сохраняется в поле transient (без события и без 🚫).
    Время результата остаётся временем первой (полной) проверки — от него считается кулдаун."""
    again = {port_key(p): p for p in second.get("ports") or []}
    want = set(needs_recheck(first))
    ports, confirmed, transient = [], [], []
    for p in first.get("ports") or []:
        k = port_key(p)
        q = again.get(k)
        if k not in want or q is None:
            ports.append(p)
            continue
        if q["verdict"] in BAD_VERDICTS:
            ports.append(dict(q, confirmed=True, first_verdict=p["verdict"]))
            confirmed.append(k)
        else:
            ports.append(dict(q, transient={"verdict": p["verdict"], "title": p["title"],
                                            "detail": p.get("detail"), "local": p.get("local")}))
            transient.append(k)
    sv = server_verdict(ports)
    out = dict(first, ports=ports, verdict=sv["verdict"], title=sv["title"])
    out["recheck"] = {"ts": now, "delay": round(now - first["ts"]), "confirmed": confirmed,
                      "transient": transient, "checkhost": bool((second.get("checkhost") or {}).get("ok")),
                      "first_verdict": first["verdict"], "first_title": first["title"]}
    if second.get("checkhost", {}).get("ok"):
        out["checkhost_recheck"] = second["checkhost"]
    return out


def pending_for(store, server: str) -> Optional[dict]:
    p = store.get_state(f"blockpending:{server}")
    return p if isinstance(p, dict) and isinstance(p.get("first"), dict) else None

# --------------------------------------------------------------------------- ручная проверка с этого ПК


def local_state(status: Optional[str]) -> str:
    if status in LOCAL_OK:
        return "ok"
    if status in LOCAL_DPI:
        return "dpi"
    if status in LOCAL_CONN_FAIL:
        return "fail"
    return "skipped" if status in (None, "skipped") else "error"


LOCAL_STATE_TITLE = {"ok": "доступен с этого ПК", "dpi": "подключается и обрывается (похоже на ТСПУ/DPI)",
                     "fail": "не подключается с этого ПК", "skipped": "UDP — с ПК не проверить",
                     "error": "ошибка проверки"}

# --------------------------------------------------------------------------- для панели


def status_for(store, server: str, now: Optional[float] = None) -> dict:
    """Для панели: последний результат, история, кулдаун, «идёт проверка», перепроверка и ручная проверка."""
    now = now or time.time()
    res = latest(store, server)
    run = store.get_state(f"blockrun:{server}")
    running = bool(run) and now - float(run) < RUN_STALE_SEC
    pend = pending_for(store, server)
    last_full = max([x for x in ((res or {}).get("ts"), (pend or {}).get("first", {}).get("ts")) if x] or [0])
    retry = max(0, int(last_full + COOLDOWN_SEC - now)) if last_full else 0
    rechecking = None
    if pend:
        f = pend["first"]
        rechecking = {"since": f["ts"], "due": pend.get("due"), "in_sec": max(0, int((pend.get("due") or now) - now)),
                      "verdict": f["verdict"], "title": f["title"], "ports": pend.get("ports") or needs_recheck(f),
                      "running": bool(pend.get("running"))}
    loc = store.get_state(f"blocklocal:{server}")
    return {"server": server, "result": res, "history": history(store, server), "running": running,
            "retry_after": retry, "available_at": (last_full + COOLDOWN_SEC) if retry else now,
            "cooldown_sec": COOLDOWN_SEC, "now": now, "rechecking": rechecking,
            "local": loc if isinstance(loc, dict) else None, "local_debounce_sec": LOCAL_DEBOUNCE_SEC,
            "recheck_delay_sec": RECHECK_DELAY_SEC}


class BlockChecker:
    """Запуск проверок в фоне: «Проверить сейчас», повторная проверка, ручная проверка с ПК, расписание."""

    def __init__(self, store_factory: Callable, get_cfg: Callable, checkhost_factory: Callable = None,
                 local: Callable = local_probe, clock: Callable = time.time,
                 recheck_delay: float = RECHECK_DELAY_SEC, timer: Callable = None):
        self.store_factory, self.get_cfg = store_factory, get_cfg
        self.checkhost_factory = checkhost_factory or CheckHost
        self.local, self.clock = local, clock
        self.recheck_delay = recheck_delay
        self.timer = timer if timer is not None else self._thread_timer   # timer(delay, fn) — для тестов
        self.lock = threading.Lock()
        self.running: set = set()
        self.local_last: dict = {}

    @staticmethod
    def _thread_timer(delay: float, fn: Callable):
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        return t

    def _server(self, name: str):
        return next((s for s in self.get_cfg().servers if s.name == name), None)

    # ----------------------------------------------------------------- полная проверка (с check-host)
    def start(self, name: str, wait: bool = False) -> dict:
        srv = self._server(name)
        if srv is None:
            raise ValueError("неизвестный сервер")
        store = self.store_factory()
        try:
            st = status_for(store, name, self.clock())
        finally:
            store.db.close()
        if st["running"] or name in self.running:
            return {"started": False, "running": True, "message": "проверка уже идёт"}
        if st["rechecking"]:
            return {"started": False, "rechecking": True,
                    "message": "идёт повторная проверка найденных проблем — дождитесь её результата"}
        if st["retry_after"] > 0:
            at = time.strftime("%H:%M", time.localtime(st["available_at"]))
            return {"started": False, "retry_after": st["retry_after"], "available_at": st["available_at"],
                    "message": f"полная проверка будет доступна в {at} (через {max(1, -(-st['retry_after'] // 60))}"
                               " мин); проверка с этого ПК доступна сейчас"}
        with self.lock:
            if name in self.running:
                return {"started": False, "running": True, "message": "проверка уже идёт"}
            self.running.add(name)
        th = threading.Thread(target=self._run_one, args=(srv,), name=f"blockcheck-{name}", daemon=True)
        th.start()
        if wait:
            th.join()
        return {"started": True, "message": "проверка запущена"}

    def _checkhost_or_note(self, store, now):
        backoff = store.get_state("checkhost_backoff_until") or 0
        if backoff > now:
            return None, (f"check-host.net ограничил частоту запросов, пауза ещё {int((backoff - now) // 60) + 1}"
                          " мин — проверены только порты с этого ПК")
        return self.checkhost_factory(), None

    def _run_one(self, srv) -> Optional[dict]:
        store = self.store_factory()
        try:
            now = self.clock()
            store.set_state(f"blockrun:{srv.name}", now)
            store.commit()
            checkhost, note = self._checkhost_or_note(store, now)
            discovered = (store.get_state(f"srv:{srv.name}", {}) or {}).get("ports")
            res = check_server(srv, discovered, checkhost, self.local, now=now, checkhost_note=note,
                               awg=_awg_evidence(store, srv.name))
            if res["checkhost"].get("limited"):
                store.set_state("checkhost_backoff_until", now + CHECKHOST_BACKOFF_SEC)
            bad = needs_recheck(res)
            if bad:
                # Не сохраняем сразу: статус, событие и 🚫 — только если повторная проверка подтвердит.
                store.set_state(f"blockpending:{srv.name}", {"first": res, "ports": bad,
                                                              "due": now + self.recheck_delay})
                store.commit()
                log.info("Проверка блокировок %s: %s — перепроверка через %d с", srv.name, res["verdict"],
                         self.recheck_delay)
                self.timer(self.recheck_delay, lambda: self.run_recheck(srv.name))
            else:
                save_result(store, res)
                log.info("Проверка блокировок %s: %s", srv.name, res["verdict"])
            return res
        except Exception:
            log.exception("Проверка блокировок %s не удалась", srv.name)
            return None
        finally:
            try:
                store.set_state(f"blockrun:{srv.name}", None)
                store.commit()
                store.db.close()
            except Exception:
                pass
            with self.lock:
                self.running.discard(srv.name)

    # ----------------------------------------------------------------- повторная проверка
    def run_recheck(self, name: str) -> Optional[dict]:
        """Повторная проверка плохих портов. С этого ПК — всегда; check-host — только если он не на паузе.
        Не считается в 30-минутный кулдаун ручного запуска."""
        with self.lock:
            if name in self.running:
                return None
            self.running.add(name)
        store = self.store_factory()
        try:
            pend = pending_for(store, name)
            if not pend:
                return None
            first = pend["first"]
            srv = self._server(name)
            if srv is None:                      # сервер удалили из настроек
                store.set_state(f"blockpending:{name}", None)
                store.commit()
                return None
            now = self.clock()
            store.set_state(f"blockpending:{name}", dict(pend, running=True))
            store.commit()
            want = set(pend.get("ports") or needs_recheck(first))
            prior = {port_key(p): {"ru": p.get("ru"), "world": p.get("world")} for p in first.get("ports") or []}
            checkhost, note = self._checkhost_or_note(store, now)
            discovered = (store.get_state(f"srv:{name}", {}) or {}).get("ports")
            try:
                second = check_server(srv, discovered, checkhost, self.local, now=now, checkhost_note=note,
                                      awg=_awg_evidence(store, name), only=want, prior=prior)
                if second["checkhost"].get("limited"):
                    store.set_state("checkhost_backoff_until", now + CHECKHOST_BACKOFF_SEC)
                final = merge_recheck(first, second, now)
            except Exception as e:
                log.exception("Повторная проверка %s не удалась", name)
                final = dict(first, recheck={"ts": now, "error": type(e).__name__, "confirmed": [],
                                             "transient": [], "first_verdict": first["verdict"],
                                             "first_title": first["title"]})
            store.set_state(f"blockpending:{name}", None)
            save_result(store, final)
            log.info("Повторная проверка %s: %s (подтверждено %s, кратковременно %s)", name, final["verdict"],
                     final["recheck"].get("confirmed"), final["recheck"].get("transient"))
            return final
        finally:
            try:
                store.commit()
                store.db.close()
            except Exception:
                pass
            with self.lock:
                self.running.discard(name)

    def due_rechecks(self, now: Optional[float] = None) -> list:
        """Перепроверки, которые пора выполнить (например, после перезапуска программы таймер потерян)."""
        now = now or self.clock()
        store = self.store_factory()
        try:
            return [s.name for s in self.get_cfg().servers
                    if (p := pending_for(store, s.name)) and (p.get("due") or 0) <= now
                    and (not p.get("running") or now - (p.get("due") or 0) > RUN_STALE_SEC)]
        finally:
            store.db.close()

    # ----------------------------------------------------------------- ручная проверка с этого ПК
    def local_check(self, name: str, port: Optional[int] = None, proto: Optional[str] = None) -> dict:
        """Сразу проверяет порты сервера только с этого ПК (без check-host и без кулдауна, антидребезг 10 с).
        port=None — все порты сервера. Результат хранится отдельно и не меняет статус блокировок."""
        srv = self._server(name)
        if srv is None:
            raise ValueError("неизвестный сервер")
        now = self.clock()
        store = self.store_factory()
        try:
            discovered = (store.get_state(f"srv:{name}", {}) or {}).get("ports")
            targets = targets_for(srv, discovered)
            if port is not None:
                key = f"{int(port)}/{'udp' if proto == 'udp' else 'tcp'}"
                targets = [t for t in targets if port_key(t) == key]
                if not targets:
                    raise ValueError("такого порта у сервера нет")
            dkey = (name, None if port is None else port_key(targets[0]))
            with self.lock:
                last = max(self.local_last.get(dkey, 0), self.local_last.get((name, None), 0))
                if now - last < LOCAL_DEBOUNCE_SEC:
                    wait = int(LOCAL_DEBOUNCE_SEC - (now - last)) + 1
                    return {"started": False, "retry_after": wait,
                            "message": f"только что проверяли — повторить можно через {wait} с"}
                self.local_last[dkey] = now
            with ThreadPoolExecutor(max_workers=max(1, min(6, len(targets)))) as pool:
                probes = list(pool.map(lambda t: (self.local(t, srv.host, getattr(srv, "block_sni", None))
                                                  if t.get("probe") else
                                                  {"status": "skipped", "ms": None, "detail": LOCAL_TEXT["skipped"]}),
                                       targets))
            prev = store.get_state(f"blocklocal:{name}") or {}
            ports = dict(prev.get("ports") or {}) if isinstance(prev, dict) else {}
            for t, r in zip(targets, probes):
                stt = local_state(r.get("status"))
                ports[port_key(t)] = {"port": t["port"], "proto": t["proto"], "label": t["label"], "ts": now,
                                      "status": r.get("status"), "ms": r.get("ms"),
                                      "detail": r.get("detail") or LOCAL_TEXT.get(r.get("status"), ""),
                                      "state": stt, "title": LOCAL_STATE_TITLE[stt]}
            rec = {"ts": now, "scope": "server" if port is None else port_key(targets[0]), "ports": ports,
                   "source": "ручная проверка · с этого ПК"}
            store.set_state(f"blocklocal:{name}", rec)
            store.commit()
            return {"started": True, "done": True, "local": rec,
                    "results": [ports[port_key(t)] for t in targets]}
        finally:
            store.db.close()

    # ----------------------------------------------------------------- расписание
    def due_servers(self, now: Optional[float] = None) -> list:
        """Серверы, которым пора на периодическую проверку."""
        cfg = self.get_cfg()
        if not cfg.block_check_enabled or not cfg.servers:
            return []
        now = now or self.clock()
        period = cfg.block_check_hours * 3600
        store = self.store_factory()
        try:
            out = []
            for s in cfg.servers:
                if pending_for(store, s.name):
                    continue
                res = latest(store, s.name)
                if res is None or now - res["ts"] >= period:
                    out.append(s)
            return out
        finally:
            store.db.close()

    def periodic_loop(self, stop: threading.Event, first_delay: float = 300, tick: float = 60):
        """Фоновая проверка по расписанию (по умолчанию раз в 6 часов). Серверы — по очереди.
        Заодно выполняет перепроверки, чей таймер потерялся (например, после перезапуска)."""
        if stop.wait(min(first_delay, 30)):
            return
        started = self.clock()
        while not stop.is_set():
            try:
                for name in self.due_rechecks():
                    if stop.is_set():
                        break
                    self.run_recheck(name)
                if self.clock() - started >= first_delay - 30:
                    for srv in self.due_servers():
                        if stop.is_set():
                            break
                        with self.lock:
                            if srv.name in self.running:
                                continue
                            self.running.add(srv.name)
                        self._run_one(srv)
            except Exception:
                log.exception("Ошибка фоновой проверки блокировок")
            stop.wait(tick)
