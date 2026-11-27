# -*- coding: utf-8 -*-
"""Опциональный режим каскада: цепочка вход → выходы, задержка по звеньям, клиенты за входом.

По умолчанию выключен: панель и опрос работают как раньше. На серверы ничего не записывает —
с входа на выход выполняется только ping (чтение). Импорт из Cascade Control читает
из локальной базы только имена, адреса и роли, без секретов.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# IP/имя для ping с входного узла: без пробелов и shell-метасимволов.
HOP_HOST_RE = re.compile(r"^[A-Za-z0-9.:\[\]\-]{1,253}$")
_PING_AVG_RE = re.compile(
    r"= [\d.]+/([\d.]+)/"
    r"|(?:Average|Среднее) = (\d+)"
    r"|rtt .*?= [\d.]+/([\d.]+)/",
    re.I,
)


class CascadeError(ValueError):
    pass


@dataclass
class CascadeLink:
    entry: str
    exits: list = field(default_factory=list)


@dataclass
class CascadeCfg:
    enabled: bool = False
    links: list = field(default_factory=list)  # list[CascadeLink]


def empty() -> CascadeCfg:
    return CascadeCfg(enabled=False, links=[])


def _unique(seq):
    out, seen = [], set()
    for x in seq:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def parse_cascade(raw, server_names=None, strict: bool = False) -> CascadeCfg:
    """Читает секцию cascade из YAML. Без секции — выключено.
    strict=True — ошибка на неизвестное имя (сохранение настроек).
    Иначе битые ссылки отбрасываются, чтобы панель не переставала запускаться."""
    if not raw or not isinstance(raw, dict):
        return empty()
    sec = raw.get("cascade") if "cascade" in raw else raw
    if not sec or not isinstance(sec, dict):
        return empty()
    enabled = bool(sec.get("enabled", False))
    names = set(server_names) if server_names is not None else None
    links = []
    for i, item in enumerate(sec.get("links") or []):
        if not isinstance(item, dict):
            if strict:
                raise CascadeError(f"cascade.links[{i}]: ожидается объект с entry и exits")
            continue
        entry = str(item.get("entry") or "").strip()
        exits = [str(x).strip() for x in (item.get("exits") or []) if str(x).strip()]
        exits = _unique(exits)
        if not entry:
            if strict:
                raise CascadeError(f"cascade.links[{i}]: укажите входной сервер")
            continue
        if names is not None and entry not in names:
            if strict:
                raise CascadeError(f"Вход «{entry}» не найден среди серверов мониторинга")
            continue
        bad = [x for x in exits if x == entry or (names is not None and x not in names)]
        if bad and strict:
            raise CascadeError("Выход «%s» нельзя привязать к входу «%s»" % (bad[0], entry))
        exits = [x for x in exits if x != entry and (names is None or x in names)]
        if not exits:
            if strict:
                raise CascadeError(f"У входа «{entry}» укажите хотя бы один выход")
            continue
        links.append(CascadeLink(entry=entry, exits=exits))
    # один вход не должен повторяться
    seen = set()
    uniq = []
    for ln in links:
        if ln.entry in seen:
            if strict:
                raise CascadeError(f"Вход «{ln.entry}» указан больше одного раза")
            continue
        seen.add(ln.entry)
        uniq.append(ln)
    if enabled and strict and not uniq:
        raise CascadeError("Включили режим каскада, но нет ни одной связки вход → выход")
    return CascadeCfg(enabled=enabled, links=uniq)


def to_raw(cfg: CascadeCfg) -> dict:
    return {
        "enabled": bool(cfg.enabled),
        "links": [{"entry": ln.entry, "exits": list(ln.exits)} for ln in cfg.links],
    }


def public_view(cfg: CascadeCfg) -> dict:
    return to_raw(cfg)


def roles_for(cfg: CascadeCfg) -> dict:
    """Имя сервера -> 'entry' / 'exit'. Выход, который сам является входом другой связки, — entry."""
    if not cfg or not cfg.enabled:
        return {}
    out = {}
    for ln in cfg.links:
        for ex in ln.exits:
            out.setdefault(ex, "exit")
        out[ln.entry] = "entry"
    return out


def exits_of(cfg: CascadeCfg, entry: str) -> list:
    for ln in cfg.links:
        if ln.entry == entry:
            return list(ln.exits)
    return []


def entry_of(cfg: CascadeCfg, name: str) -> Optional[str]:
    """Вход, к которому привязан этот выход (или сам name, если это вход)."""
    if not cfg or not cfg.enabled:
        return None
    for ln in cfg.links:
        if ln.entry == name or name in ln.exits:
            return ln.entry
    return None


def hop_targets(cfg: CascadeCfg, servers) -> dict:
    """Имя входа -> [(имя выхода, host), ...]. Пусто, если режим выключен."""
    if not cfg or not cfg.enabled:
        return {}
    by_name = {s.name: s for s in servers}
    out = {}
    for ln in cfg.links:
        pairs = []
        for ex in ln.exits:
            srv = by_name.get(ex)
            if srv and HOP_HOST_RE.match(srv.host or ""):
                pairs.append((ex, srv.host))
        if pairs:
            out[ln.entry] = pairs
    return out


def hop_ping_command(host: str) -> str:
    host = str(host or "").strip()
    if not HOP_HOST_RE.match(host):
        raise CascadeError("недопустимый адрес для проверки звена")
    return "ping -c 2 -W 2 " + host


def parse_ping_ms(text: str) -> Optional[float]:
    """Средний RTT в мс из вывода ping (Linux/BusyBox и русский Windows)."""
    if not text:
        return None
    m = re.search(r"= [\d.]+/([\d.]+)/", text) or re.search(r"(?:Average|Среднее) = (\d+)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def collect_hops(runner, targets) -> list:
    """targets: [(name, host), ...]. Команды только из белого списка (hop_ping_command)."""
    hops = []
    for name, host in targets or []:
        item = {"exit": name, "host": host, "ping_ms": None, "ok": False, "error": None}
        try:
            cmd = hop_ping_command(host)
            rc, out, err = runner.run(cmd)
            ms = parse_ping_ms(out or "")
            if ms is not None:
                item["ping_ms"] = ms
                item["ok"] = True
            else:
                item["error"] = (err or out or f"код {rc}").strip()[:200] or "нет ответа"
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {e}"[:200]
        hops.append(item)
    return hops



def _asdict(row) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return dict(row)


def endpoint_host(endpoint: str) -> str:
    ep = (endpoint or "").strip()
    if not ep or ep in ("(none)", "none"):
        return ""
    if ep.startswith("["):
        end = ep.find("]")
        return ep[1:end].lower() if end > 1 else ""
    # host:port, но IPv6 без скобок не режем по последнему :
    if ep.count(":") == 1:
        return ep.rsplit(":", 1)[0].lower()
    return ep.lower()


def is_tunnel_peer(peer: dict, entry_host: str) -> bool:
    eh = (entry_host or "").strip().lower()
    if not eh:
        return False
    return endpoint_host(_asdict(peer).get("endpoint") or "") == eh


def clients_from_peers(peers, entry_host=None) -> list:
    """Клиенты узла. Если дан entry_host, пиры каскадного туннеля помечаются отдельно."""
    out = []
    for p in peers or []:
        p = _asdict(p)
        tunnel = bool(entry_host) and is_tunnel_peer(p, entry_host)
        out.append({
            "client": p.get("client") or (p.get("pubkey") or "")[:12],
            "pubkey": p.get("pubkey") or "",
            "online": bool(p.get("online")),
            "rx": p.get("rx") or 0,
            "tx": p.get("tx") or 0,
            "endpoint": p.get("endpoint") or "",
            "tunnel": tunnel,
        })
    return out


# ---------------------------------------------------------------------------
# Импорт топологии из локальной базы Cascade Control (только чтение, без секретов)
# ---------------------------------------------------------------------------

def default_cc_db_paths() -> list:
    paths = [Path.home() / ".cascade-control" / "cascade.db"]
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        paths.append(Path(appdata) / "CascadeControl" / "cascade.db")
        paths.append(Path(appdata) / "cascade-control" / "cascade.db")
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        paths.append(Path(local) / "CascadeControl" / "cascade.db")
    env = os.environ.get("CASCADE_DATA_DIR")
    if env:
        paths.insert(0, Path(env).expanduser() / "cascade.db")
    # уникальные существующие — поиск оставляет и несуществующие кандидатов
    out, seen = [], set()
    for p in paths:
        try:
            key = str(p.resolve()) if p.exists() else str(p)
        except OSError:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def find_cascade_db(explicit=None) -> Path:
    if explicit:
        p = Path(str(explicit)).expanduser()
        if not p.is_file():
            raise CascadeError("Файл базы Cascade Control не найден")
        return p
    for p in default_cc_db_paths():
        if p.is_file():
            return p
    raise CascadeError(
        "База Cascade Control не найдена. Укажите путь к cascade.db "
        "(обычно папка .cascade-control в домашнем каталоге)."
    )


def _norm_host(host: str) -> str:
    return (host or "").strip().lower().replace("[", "").replace("]", "")


def import_topology(db_path, am_servers) -> dict:
    """Читает роли и цепочки из cascade.db. Не выбирает столбцы *_enc и прочие секреты.

    am_servers: [{"name", "host"}, ...] уже добавленные в монитор.
    Возвращает предложенные links и список несопоставленных узлов CC (без секретов).
    """
    path = find_cascade_db(db_path)
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as e:
        raise CascadeError("Не удалось открыть базу Cascade Control: " + str(e)[:120])
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "servers" not in tables:
            raise CascadeError("В этом файле нет таблицы серверов Cascade Control")
        cols = {r[1] for r in db.execute("PRAGMA table_info(servers)")}
        for need in ("id", "name", "host", "role"):
            if need not in cols:
                raise CascadeError("Непонятный формат базы Cascade Control")
        # Только безопасные столбцы — никаких *_enc, ключей, паролей.
        cc = list(db.execute("SELECT id, name, host, role FROM servers"))
        chains = []
        if "chains" in tables:
            ccols = {r[1] for r in db.execute("PRAGMA table_info(chains)")}
            if "entry_id" in ccols and "exit_id" in ccols:
                chains = list(db.execute("SELECT entry_id, exit_id FROM chains"))
    except sqlite3.Error as e:
        raise CascadeError("Не удалось прочитать базу Cascade Control: " + str(e)[:120])
    finally:
        db.close()

    by_id = {r["id"]: r for r in cc}
    am_by_host = {}
    am_by_name = {}
    for s in am_servers or []:
        name, host = str(s.get("name") or "").strip(), _norm_host(s.get("host") or "")
        if name:
            am_by_name[name.lower()] = name
        if host:
            am_by_host[host] = name

    def match(row) -> Optional[str]:
        host = _norm_host(row["host"])
        if host and host in am_by_host:
            return am_by_host[host]
        nm = str(row["name"] or "").strip().lower()
        return am_by_name.get(nm)

    proposed = {}  # entry_am_name -> [exits]
    unmatched = []
    matched = []

    def remember_unmatched(row, reason):
        unmatched.append({
            "name": str(row["name"] or ""),
            "role": str(row["role"] or ""),
            "reason": reason,
        })

    pairs = [(r["entry_id"], r["exit_id"]) for r in chains if r["exit_id"]]
    if not pairs:
        entries = [r for r in cc if str(r["role"] or "") == "entry"]
        exits = [r for r in cc if str(r["role"] or "") == "exit"]
        if len(entries) == 1:
            pairs = [(entries[0]["id"], ex["id"]) for ex in exits]

    used_ids = set()
    for eid, xid in pairs:
        er, xr = by_id.get(eid), by_id.get(xid)
        if not er or not xr:
            continue
        used_ids.add(eid)
        used_ids.add(xid)
        em, xm = match(er), match(xr)
        if not em:
            remember_unmatched(er, "добавьте этот вход в монитор (тот же адрес или имя)")
            continue
        if not xm:
            remember_unmatched(xr, "добавьте этот выход в монитор (тот же адрес или имя)")
            continue
        if em == xm:
            continue
        proposed.setdefault(em, [])
        if xm not in proposed[em]:
            proposed[em].append(xm)
        matched.append({"cc": er["name"], "monitor": em, "role": "entry"})
        matched.append({"cc": xr["name"], "monitor": xm, "role": "exit"})

    for row in cc:
        if row["id"] in used_ids:
            continue
        if match(row):
            continue
        if str(row["role"] or "") in ("entry", "exit"):
            remember_unmatched(row, "нет в связках или не сопоставлен с сервером монитора")

    # дедуп unmatched по имени
    um, seen_u = [], set()
    for u in unmatched:
        key = (u["name"], u["reason"])
        if key in seen_u:
            continue
        seen_u.add(key)
        um.append(u)

    links = [{"entry": e, "exits": xs} for e, xs in proposed.items()]
    return {
        "db_path": str(path),
        "links": links,
        "matched": matched,
        "unmatched": um,
        "found": bool(links),
    }


# ---------------------------------------------------------------------------
# Агрегация для панели (только чтение SQLite)
# ---------------------------------------------------------------------------

def _latest_poll(store, name: str):
    return store.db.execute(
        "SELECT * FROM polls WHERE server=? ORDER BY ts DESC LIMIT 1", (name,)
    ).fetchone()


def _latest_peers(store, name: str, ts=None) -> list:
    if ts is None:
        row = store.db.execute("SELECT MAX(ts) m FROM peers WHERE server=?", (name,)).fetchone()
        ts = row["m"] if row else None
    if ts is None:
        return []
    return list(store.db.execute(
        "SELECT * FROM peers WHERE server=? AND ts=? ORDER BY online DESC, client", (name, ts)
    ))


def _latest_hop(store, entry: str, exit_name: str):
    try:
        return store.db.execute(
            "SELECT * FROM hops WHERE entry=? AND exit=? ORDER BY ts DESC LIMIT 1",
            (entry, exit_name),
        ).fetchone()
    except Exception:
        return None


def _poll_brief(row, host=None) -> dict:
    if not row:
        return {"status": "nodata", "host": host, "tcp_ms": None, "ping_ms": None,
                "rx_bps": None, "tx_bps": None, "peers_online": None, "peers_total": None}
    reachable = bool(row["reachable"] if "reachable" in row.keys() else True)
    ssh_ok = bool(row["ssh_ok"])
    status = "up" if ssh_ok else ("down" if not reachable else "ssh_fail")
    return {
        "status": status,
        "host": host,
        "tcp_ms": row["tcp_ms"],
        "ping_ms": row["ping_ms"],
        "rx_bps": row["rx_bps"],
        "tx_bps": row["tx_bps"],
        "peers_online": row["peers_online"],
        "peers_total": row["peers_total"],
        "error": row["error"] if "error" in row.keys() else None,
    }


def api_cascade(store, cfg, now=None, theme_fn=None) -> dict:
    """Снимок цепочек для панели. Если режим выключен — {enabled: False} без метрик."""
    cas = getattr(cfg, "cascade", None) or empty()
    if not cas.enabled:
        return {"enabled": False, "links": []}
    hosts = {s.name: s.host for s in (cfg.servers if cfg is not None else [])}

    def theme(name):
        return theme_fn(cfg, name) if theme_fn else None

    chains = []
    for ln in cas.links:
        entry_poll = _latest_poll(store, ln.entry)
        entry_peers = _latest_peers(store, ln.entry, entry_poll["ts"] if entry_poll else None)
        clients = [c for c in clients_from_peers(entry_peers) if not c["tunnel"]]
        hops = []
        for ex in ln.exits:
            poll = _latest_poll(store, ex)
            hop_row = _latest_hop(store, ln.entry, ex)
            ex_peers = _latest_peers(store, ex, poll["ts"] if poll else None)
            tunnel = _asdict(next((p for p in ex_peers if is_tunnel_peer(p, hosts.get(ln.entry, ""))), None))
            hop = _poll_brief(poll, hosts.get(ex))
            hop.update({
                "name": ex,
                "role": "exit",
                "hop_ms": hop_row["ping_ms"] if hop_row else None,
                "hop_ok": bool(hop_row["ok"]) if hop_row else None,
                "theme": theme(ex),
                "tunnel_rx": tunnel.get("rx"),
                "tunnel_tx": tunnel.get("tx"),
                # Клиенты входа, видимые на этом выходе «через вход».
                # Если выходов несколько, точно разделить трафик по выходам
                # без журналов Xray нельзя — показываем одних и тех же клиентов входа.
                "clients": [
                    {"client": c["client"], "online": c["online"], "rx": c["rx"], "tx": c["tx"],
                     "via_entry": ln.entry}
                    for c in clients
                ],
            })
            hops.append(hop)
        chains.append({
            "entry": {
                **_poll_brief(entry_poll, hosts.get(ln.entry)),
                "name": ln.entry,
                "role": "entry",
                "theme": theme(ln.entry),
                "clients": [{"client": c["client"], "online": c["online"], "rx": c["rx"], "tx": c["tx"]}
                            for c in clients],
            },
            "exits": hops,
        })
    return {"enabled": True, "links": chains}


def annotate_servers(servers: list, cfg) -> list:
    """Добавляет role/cascade только при включённом режиме (иначе список не трогаем)."""
    cas = getattr(cfg, "cascade", None) or empty()
    if not cas.enabled:
        return servers
    roles = roles_for(cas)
    entry_counts = {}
    for s in servers:
        name = s.get("name")
        role = roles.get(name)
        if not role:
            continue
        s["role"] = role
        s["cascade_entry"] = entry_of(cas, name)
        if role == "entry":
            s["cascade_exits"] = exits_of(cas, name)
            entry_counts[name] = (s.get("peers_online"), s.get("peers_total"))
    for s in servers:
        if s.get("role") != "exit":
            continue
        counts = entry_counts.get(s.get("cascade_entry"))
        if counts and counts[1] is not None:
            s["peers_online"], s["peers_total"] = counts
            s["peers_from_entry"] = True
    return servers
