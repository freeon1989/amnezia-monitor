# -*- coding: utf-8 -*-
"""Агрегации для веб-панели: читают только SQLite-историю (никаких подключений к серверам)."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional

DNS_NOTES = {
    "no_container": "На сервере нет контейнера amnezia-dns, поэтому сайты определить нельзя. "
                    "Сам VPN (WireGuard/AmneziaWG) видит только объём трафика, а не адреса сайтов.",
    "no_log": "Журнал DNS на сервере не найден: нет файла Unbound и пустой вывод docker logs. "
              "Программа ничего на сервере не включает.",
    "empty": "Журнал DNS на сервере есть, но запросов в прочитанном окне нет.",
    "unreadable": "Журнал DNS есть, но прочитать его не удалось. Это не значит, что он выключен.",
    "unsupported": "Журнал DNS прочитан, но его формат не распознан.",
    "nodata": "Данных пока нет — дождитесь первого опроса.",
}


def server_names(store, cfg=None) -> list:
    if cfg is not None:
        return [s.name for s in cfg.servers]
    return [r[0] for r in store.db.execute("SELECT DISTINCT server FROM polls ORDER BY server")]


def _hosts(cfg) -> dict:
    return {s.name: s.host for s in cfg.servers} if cfg is not None else {}


def api_servers(store, cfg=None, now: Optional[float] = None) -> list:
    now = now or time.time()
    out, hosts = [], _hosts(cfg)
    for n in server_names(store, cfg):
        last = store.db.execute("SELECT * FROM polls WHERE server=? ORDER BY ts DESC LIMIT 1", (n,)).fetchone()
        if not last:
            out.append({"name": n, "host": hosts.get(n), "status": "nodata"})
            continue
        out.append({
            "name": n, "host": hosts.get(n),
            "status": "up" if last["ssh_ok"] else "down",
            "age": round(now - last["ts"]), "tcp_ms": last["tcp_ms"], "cpu": last["cpu_pct"],
            "mem": last["mem_pct"], "peers_online": last["peers_online"], "peers_total": last["peers_total"],
            "rx_bps": last["rx_bps"], "tx_bps": last["tx_bps"], "error": last["error"],
        })
    return out


def bucketize(points: list, start: float, end: float, buckets: int, how: str = "avg") -> list:
    """points: [(ts, value)] -> список длиной buckets (avg/sum/max), пустые корзины = None (sum -> 0)."""
    size = (end - start) / buckets
    acc = [[] for _ in range(buckets)]
    for ts, v in points:
        if v is None or ts < start or ts > end:
            continue
        i = min(buckets - 1, int((ts - start) / size))
        acc[i].append(v)
    res = []
    for a in acc:
        if how == "sum":
            res.append(sum(a))
        elif not a:
            res.append(None)
        elif how == "max":
            res.append(max(a))
        else:
            res.append(sum(a) / len(a))
    return res


def peer_deltas(rows: list) -> dict:
    """rows (отсортированы по pubkey, ts) -> pubkey -> список (ts, d_down, d_up, dt).
    down = то, что сервер ОТПРАВИЛ клиенту (tx сервера), up = получил от клиента (rx).
    Сброс счётчиков (перезапуск интерфейса) даёт отрицательную разницу — тогда берём новое значение."""
    res = defaultdict(list)
    prev = {}
    for r in rows:
        pk = r["pubkey"]
        if pk in prev:
            p = prev[pk]
            dt = r["ts"] - p["ts"]
            if dt > 0:
                dd = r["tx"] - p["tx"]
                du = r["rx"] - p["rx"]
                res[pk].append((r["ts"], dd if dd >= 0 else r["tx"], du if du >= 0 else r["rx"], dt))
        prev[pk] = r
    return res


def api_server(store, cfg, name: str, now: Optional[float] = None, hours: float = 24,
               buckets: int = 48, spark_points: int = 30) -> dict:
    now = now or time.time()
    since = now - hours * 3600
    db = store.db
    last = db.execute("SELECT * FROM polls WHERE server=? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
    if not last:
        return {"name": name, "status": "nodata", "users": [], "containers": [], "events": [],
                "series": None, "dns": {"status": "nodata", "note": DNS_NOTES["nodata"]}}
    polls = db.execute("SELECT * FROM polls WHERE server=? AND ts>=? ORDER BY ts", (name, since)).fetchall()
    total = len(polls)
    ok = sum(1 for p in polls if p["ssh_ok"])
    lats = [p["tcp_ms"] for p in polls if p["tcp_ms"] is not None]

    # --- текущее состояние берём из последнего успешного опроса
    last_ok = last if last["ssh_ok"] else db.execute(
        "SELECT * FROM polls WHERE server=? AND ssh_ok=1 ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
    cur = {
        "status": "up" if last["ssh_ok"] else "down", "error": last["error"], "age": round(now - last["ts"]),
        "tcp_ms": last["tcp_ms"], "ping_ms": last["ping_ms"],
        "availability": round(100.0 * ok / total, 1) if total else None,
        "latency_avg": round(sum(lats) / len(lats), 1) if lats else None,
    }
    if last_ok:
        cur.update({"uptime_s": last_ok["uptime_s"], "load1": last_ok["load1"], "nproc": last_ok["nproc"],
                    "cpu": last_ok["cpu_pct"], "mem": last_ok["mem_pct"], "mem_used": last_ok["mem_used"],
                    "mem_total": last_ok["mem_total"], "rx_bps": last_ok["rx_bps"], "tx_bps": last_ok["tx_bps"],
                    "stale": not last["ssh_ok"]})
        if not last["ssh_ok"]:   # сервер недоступен — «текущей» скорости нет
            cur["rx_bps"] = cur["tx_bps"] = None
    ref_ts = last_ok["ts"] if last_ok else last["ts"]
    containers = [dict(name=c["name"], type=c["type"], up=bool(c["up"]), status=c["status"]) for c in db.execute(
        "SELECT * FROM containers WHERE server=? AND ts=? ORDER BY name", (name, ref_ts))]

    # --- ряды для графиков (48 корзин по 30 мин)
    dts = []
    for a, b in zip(polls, polls[1:]):
        dts.append((b["ts"], b["ts"] - a["ts"]))
    srv_down = [(p["ts"], (p["tx_bps"] or 0) * dt) for p, (_, dt) in zip(polls[1:], dts) if p["tx_bps"] is not None]
    srv_up = [(p["ts"], (p["rx_bps"] or 0) * dt) for p, (_, dt) in zip(polls[1:], dts) if p["rx_bps"] is not None]
    series = {
        "start": since, "end": now, "buckets": buckets,
        "cpu": bucketize([(p["ts"], p["cpu_pct"]) for p in polls], since, now, buckets),
        "mem": bucketize([(p["ts"], p["mem_pct"]) for p in polls], since, now, buckets),
        "online": bucketize([(p["ts"], p["peers_online"]) for p in polls], since, now, buckets, "max"),
        # ВНИМАНИЕ: счётчики сетевого интерфейса сервера, вкл. трафик к сайтам. ↓ = отдано сервером
        "down_bytes": bucketize(srv_down, since, now, buckets, "sum"),
        "up_bytes": bucketize(srv_up, since, now, buckets, "sum"),
        "avail": bucketize([(p["ts"], p["ssh_ok"]) for p in polls], since, now, buckets, "avg"),
    }

    # --- пользователи
    rows = db.execute("SELECT * FROM peers WHERE server=? AND ts>=? ORDER BY pubkey, ts", (name, since)).fetchall()
    deltas = peer_deltas(rows)
    latest = {r["pubkey"]: r for r in db.execute("SELECT * FROM peers WHERE server=? AND ts=?", (name, ref_ts))}
    users = []
    for pk, r in latest.items():
        d = deltas.get(pk, [])
        rate_down = rate_up = 0.0
        if d and d[-1][0] == ref_ts and not cur.get("stale"):
            rate_down, rate_up = d[-1][1] / d[-1][3], d[-1][2] / d[-1][3]
        spark = [round((x[1] + x[2]) / x[3]) for x in d[-spark_points:]]
        users.append({
            "client": r["client"], "pubkey": pk[:10] + "…", "container": r["container"],
            "online": bool(r["online"]) and not cur.get("stale"),
            "handshake_age": (now - r["handshake"]) if r["handshake"] else None,
            "endpoint": r["endpoint"].rsplit(":", 1)[0] if r["endpoint"] else None,
            "rate_down": None if cur.get("stale") else rate_down,
            "rate_up": None if cur.get("stale") else rate_up, "spark": spark,
            "down_24h": sum(x[1] for x in d), "up_24h": sum(x[2] for x in d),
            "down_total": r["tx"], "up_total": r["rx"],
            "hourly": bucketize([(x[0], x[1] + x[2]) for x in d], since, now, 24, "sum"),
        })
    users.sort(key=lambda u: (not u["online"], -(u["down_24h"] + u["up_24h"])))
    tot_down = sum(u["down_24h"] for u in users)
    tot_up = sum(u["up_24h"] for u in users)

    # --- DNS
    st = store.get_state(f"srv:{name}", {}) or {}
    dns_status = st.get("dns_status", "no_container")
    dns = {"status": dns_status, "note": st.get("dns_note") or DNS_NOTES.get(dns_status),
           "coverage_hours": None}
    if dns_status == "ok" or db.execute("SELECT 1 FROM dns WHERE server=? AND ts>=? LIMIT 1", (name, since)).fetchone():
        top = db.execute("SELECT site, COUNT(*) n FROM dns WHERE server=? AND ts>=? GROUP BY site "
                         "ORDER BY n DESC LIMIT 12", (name, since)).fetchall()
        per = defaultdict(list)
        for x in db.execute("SELECT client, site, COUNT(*) n FROM dns WHERE server=? AND ts>=? "
                            "GROUP BY client, site ORDER BY client, n DESC", (name, since)):
            if len(per[x["client"]]) < 8:
                per[x["client"]].append({"site": x["site"], "n": x["n"]})
        cnt = db.execute("SELECT COUNT(*) FROM dns WHERE server=? AND ts>=?", (name, since)).fetchone()[0]
        span = db.execute("SELECT MIN(ts) a, MAX(ts) b FROM dns WHERE server=? AND ts>=?",
                          (name, since)).fetchone()
        reach = (now - span["a"]) if span and span["a"] is not None else None
        coverage_hours = round(reach / 3600.0, 1) if reach is not None else None
        note = None
        if reach is not None and reach < 23 * 3600:
            note = ("Показаны фактические %.1f ч из 24. Более ранних записей в журнале сервера нет, "
                    "программа их не достраивает." % coverage_hours)
        dns.update({"status": "ok", "note": note, "queries": cnt, "coverage_hours": coverage_hours,
                    "top": [{"site": x["site"], "n": x["n"]} for x in top],
                    "per_user": [{"client": k, "total": sum(i["n"] for i in v), "top": v} for k, v in per.items()]})
        dns["per_user"].sort(key=lambda u: -u["total"])

    events = [{"ts": e["ts"], "kind": e["kind"], "message": e["message"]} for e in db.execute(
        "SELECT * FROM events WHERE server=? ORDER BY ts DESC LIMIT 25", (name,))]
    return {"name": name, "host": _hosts(cfg).get(name), "now": now, "current": cur, "containers": containers,
            "series": series, "users": users, "totals": {"down_24h": tot_down, "up_24h": tot_up},
            "dns": dns, "events": events}


def api_snapshot(store, cfg=None, now: Optional[float] = None) -> dict:
    """Всё сразу — для статического HTML."""
    now = now or time.time()
    return {"servers": api_servers(store, cfg, now),
            "details": {n: api_server(store, cfg, n, now) for n in server_names(store, cfg)},
            "generated": now}
