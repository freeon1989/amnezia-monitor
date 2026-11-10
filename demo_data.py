# -*- coding: utf-8 -*-
"""Режим --demo: фальшивые выводы команд для 4 тестовых серверов, 3 опроса подряд.
Никаких подключений к сети и отправки в Telegram."""
import base64
import hashlib
import json
import os
import tempfile

import amnezia_monitor as am

DEMO_SERVERS = [
    am.ServerCfg(name="test1", host="203.0.113.11", password="CHANGE_ME_1"),
    am.ServerCfg(name="test2", host="203.0.113.12", password="CHANGE_ME_2"),
    am.ServerCfg(name="test3", host="203.0.113.13", password="CHANGE_ME_3"),
    am.ServerCfg(name="test4", host="203.0.113.14", password="CHANGE_ME_4"),
]


def fake_key(seed: str) -> str:
    return base64.b64encode(hashlib.sha256(seed.encode()).digest()).decode()


CLIENTS = {  # server -> [(client name, container)]
    "test1": [("Дмитрий — телефон", "amnezia-awg"), ("Дмитрий — ноутбук", "amnezia-awg"),
              ("Мама", "amnezia-awg"), ("Офис", "amnezia-awg")],
    "test2": [("Брат", "amnezia-wireguard"), ("Телевизор", "amnezia-wireguard")],
    "test3": [("Дача — роутер", "amnezia-awg")],
    "test4": [("Резервный", "amnezia-awg")],
}

# Сценарий по тикам: какие клиенты с «свежим» handshake; состояние серверов/контейнеров; CPU
SCENARIO = [
    {"online": {"Дмитрий — телефон", "Мама", "Брат", "Дача — роутер", "Резервный"},
     "down": set(), "xray_up": True, "cpu_busy": {"test1": 0.25, "test2": 0.10, "test3": 0.05, "test4": 0.03}},
    {"online": {"Дмитрий — телефон", "Дмитрий — ноутбук", "Мама", "Брат", "Дача — роутер", "Резервный"},
     "down": set(), "xray_up": True, "cpu_busy": {"test1": 0.40, "test2": 0.12, "test3": 0.05, "test4": 0.03}},
    {"online": {"Дмитрий — ноутбук", "Мама", "Брат", "Телевизор", "Резервный"},
     "down": {"test3"}, "xray_up": False, "cpu_busy": {"test1": 0.93, "test2": 0.15, "test3": 0.0, "test4": 0.04}},
]
TICK = 60


class FakeRunner(am.Runner):
    def __init__(self, srv, tick, now):
        self.srv, self.tick, self.now = srv, tick, now
        self.sc = SCENARIO[tick]

    def run(self, cmd):
        if not am._ALLOWED_RE.match(cmd):   # та же проверка белого списка, что и в SSHRunner
            raise PermissionError(cmd)
        n, t, sc = self.srv.name, self.tick, self.sc
        idx = int(n[-1])
        if cmd == "cat /proc/uptime":
            return 0, f"{1234567 + idx * 86400 + t * TICK}.12 999.0\n", ""
        if cmd == "cat /proc/loadavg":
            l = round(sc["cpu_busy"][n] * 2 * 1.1, 2)
            return 0, f"{l} {l*0.8:.2f} {l*0.6:.2f} 2/180 12345\n", ""
        if cmd == "nproc":
            return 0, "2\n", ""
        if cmd == "cat /proc/meminfo":
            total = 2 * 1024 * 1024 if idx != 4 else 1024 * 1024
            avail = int(total * (0.62 - 0.05 * idx))
            return 0, f"MemTotal: {total} kB\nMemFree: 100000 kB\nMemAvailable: {avail} kB\n", ""
        if cmd == "head -1 /proc/stat":
            total = 1_000_000 + t * 12000          # 2 ядра * 60 с * 100 Гц
            busy = 200_000 + sum(int(12000 * SCENARIO[k]["cpu_busy"][n]) for k in range(1, t + 1))
            idle = total - busy
            return 0, f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\n", ""
        if cmd == "cat /proc/net/dev":
            rate = {"test1": 900_000, "test2": 250_000, "test3": 40_000, "test4": 5_000}[n] * (1 + t)
            rx = 10**10 + rate * TICK * t
            tx = 2 * 10**10 + int(rate * 1.1) * TICK * t
            return 0, ("Inter-|   Receive  |  Transmit\n face |bytes packets ...\n"
                       f"    lo: 5000 50 0 0 0 0 0 0 5000 50 0 0 0 0 0 0\n"
                       f"  eth0: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0\n"), ""
        if cmd.endswith("ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'"):
            rows = []
            conts = sorted({c for _, c in CLIENTS[n]})
            for c in conts:
                rows.append(f"{c}|Up 12 days|amneziavpn/{c}:latest")
            if n == "test1":
                rows.append("amnezia-xray|" + ("Up 12 days" if sc["xray_up"] else "Exited (1) 30 seconds ago")
                            + "|amneziavpn/amnezia-xray:latest")
                rows.append("amnezia-dns|Up 12 days|amneziavpn/amnezia-dns:latest")
            rows.append("portainer|Up 3 days|portainer/portainer-ce")   # не Amnezia — игнорируется
            return 0, "\n".join(rows) + "\n", ""
        if "clientsTable" in cmd:
            c = cmd.split()[2]
            data = [{"clientId": fake_key(nm), "userData": {"clientName": nm, "creationDate": "Mon Sep 1 2026"}}
                    for nm, cc in CLIENTS[n] if cc == c]
            return 0, json.dumps(data, ensure_ascii=False), ""
        if "show all dump" in cmd:
            c = cmd.split()[2]
            lines = [f"wg0\tPRIVATE_KEY_NEVER_STORED=\t{fake_key(n + 'srv')}\t51820\toff"]
            for i, (nm, cc) in enumerate(CLIENTS[n]):
                if cc != c:
                    continue
                if nm in sc["online"]:
                    hs = int(self.now) - (20 + i * 17)
                elif nm == "Телевизор" and t < 2:
                    hs = 0
                else:
                    hs = int(self.now) - 3600 * (i + 1) - 600
                ep = f"198.51.100.{20 + i}:{40000 + i}" if hs else "(none)"
                rx = (i + 1) * 150_000_000 + t * 3_000_000
                tx = (i + 1) * 900_000_000 + t * 25_000_000
                lines.append(f"wg0\t{fake_key(nm)}\t(none)\t{ep}\t10.8.1.{i + 2}/32\t{hs}\t{rx}\t{tx}\t25")
            return 0, "\n".join(lines) + "\n", ""
        return 1, "", "unknown"


def run_demo(args) -> int:
    print("ДЕМО-РЕЖИМ: тестовые данные, подключений к серверам и отправки в Telegram нет.\n")
    cfg = am.Config(servers=DEMO_SERVERS, interval=TICK, cpu_threshold=85, mem_threshold=90)
    db = args.db or os.path.join(tempfile.mkdtemp(prefix="amnezia-demo-"), "demo.db")
    store = am.Store(db)
    notifier = am.Notifier(cfg, force_print=True)
    import time
    base = time.time() - TICK * (len(SCENARIO) - 1)
    for tick in range(len(SCENARIO)):
        now = base + tick * TICK
        sc = SCENARIO[tick]
        probe = lambda s: (None, None) if s.name in sc["down"] else (38.0 + int(s.name[-1]) * 7, 35.0)
        print(f"--- Опрос {tick + 1} ---")
        evs = am.do_check(cfg, store, notifier, runner_factory=lambda s, t=tick, nw=now: FakeRunner(s, t, nw),
                          probe=probe, now=now)
        if not evs:
            print("(изменений нет)")
        print()
    print(am.build_summary(store, cfg, 24, now=base + TICK * (len(SCENARIO) - 1)))
    print(f"\nБаза демо: {db}")
    return 0


# ---------------------------------------------------------------------------
# История за 24 ч для веб-панели (--demo dashboard)
# ---------------------------------------------------------------------------
import random  # noqa: E402

# имя -> (сервер, контейнер, часы активности, средняя скорость скачивания байт/с, любимые домены)
DASH_USERS = [
    ("Дмитрий — телефон", "test1", "amnezia-awg", set(range(7, 24)), 180_000,
     ["youtube.com", "googlevideo.com", "telegram.org", "instagram.com", "cdninstagram.com", "x.com", "github.com"]),
    ("Дмитрий — ноутбук", "test1", "amnezia-awg", set(range(9, 20)), 420_000,
     ["github.com", "githubusercontent.com", "openai.com", "googleapis.com", "stackoverflow.com", "youtube.com", "microsoft.com"]),
    ("Мама", "test1", "amnezia-awg", set(range(10, 23)), 90_000,
     ["youtube.com", "googlevideo.com", "whatsapp.net", "instagram.com", "facebook.com", "rutube.ru"]),
    ("Офис", "test1", "amnezia-awg", set(range(9, 18)), 260_000,
     ["microsoft.com", "windowsupdate.com", "office.com", "zoom.us", "googleapis.com"]),
    ("Брат", "test2", "amnezia-wireguard", set(list(range(0, 2)) + list(range(16, 24))), 650_000,
     ["twitch.tv", "steamcontent.com", "discord.com", "youtube.com", "googlevideo.com", "reddit.com"]),
    ("Телевизор", "test2", "amnezia-wireguard", set(range(19, 24)), 1_100_000,
     ["netflix.com", "nflxvideo.net", "youtube.com", "googlevideo.com"]),
    ("Дача — роутер", "test3", "amnezia-awg", set(range(0, 24)), 30_000, ["ntp.org", "apple.com"]),
    ("Резервный", "test4", "amnezia-awg", set(range(8, 12)), 15_000, ["cloudflare.com"]),
]


END_ONLINE = {"Дмитрий — ноутбук", "Мама", "Брат", "Телевизор", "Резервный", "Дача — роутер"}


def build_dashboard_demo(store, cfg, now: float, hours: int = 24, step: int = 600, seed: int = 7):
    """Заполняет базу правдоподобной историей за `hours` часов с шагом `step` секунд."""
    import datetime as _dt
    rnd = random.Random(seed)
    steps = hours * 3600 // step
    base = now - steps * step
    counters = {u[0]: [rnd.randint(2, 40) * 10**9, rnd.randint(1, 8) * 10**8] for u in DASH_USERS}  # tx, rx
    last_hs = {u[0]: 0 for u in DASH_USERS}
    cpu = {s.name: [10**6, 4 * 10**6] for s in cfg.servers}
    net = {s.name: [10**11, 2 * 10**11] for s in cfg.servers}
    ips = {}
    for srv in cfg.servers:
        for i, u in enumerate([u for u in DASH_USERS if u[1] == srv.name]):
            ips[u[0]] = f"10.8.1.{i + 2}"
    for k in range(steps + 1):
        ts = base + k * step
        hour = _dt.datetime.fromtimestamp(ts).hour
        frac_left = (steps - k) * step
        for srv in cfg.servers:
            n = srv.name
            r = am.PollResult(server=n, ts=ts)
            down = n == "test3" and frac_left < 1800
            r.tcp_ms = None if down else round(30 + int(n[-1]) * 9 + rnd.random() * 12, 1)
            r.ping_ms = r.tcp_ms
            r.reachable = not down
            if down:
                r.error = "SSH-порт недоступен"
                am.process(store, cfg, r)
                continue
            r.ssh_ok = True
            r.uptime_s = 1_300_000 + int(n[-1]) * 86400 + k * step
            r.nproc = 2
            srv_rate_d = srv_rate_u = 0
            peers = []
            for u in [u for u in DASH_USERS if u[1] == n]:
                name, _, cont, active_h, rate, _doms = u
                active = hour in active_h and rnd.random() > 0.12
                if name in END_ONLINE and frac_left < 2400:
                    active = True                      # чтобы в конце демо были онлайн-клиенты
                if name == "Дмитрий — телефон" and frac_left < 1300:
                    active = False                     # «отключился» в конце
                if active:
                    wave = 0.4 + rnd.random() * 1.4
                    if n == "test1" and frac_left < 700:
                        wave *= 2.5                    # всплеск нагрузки в конце
                    d = int(rate * wave * step)
                    counters[name][0] += d
                    counters[name][1] += int(d * rnd.uniform(0.05, 0.18))
                    last_hs[name] = int(ts - rnd.randint(5, 110))
                    srv_rate_d += d / step
                    srv_rate_u += d * 0.1 / step
                hs = last_hs[name]
                peers.append({"iface": "wg0", "pubkey": fake_key(name), "endpoint": f"198.51.100.{10 + len(peers)}:5{len(peers)}000" if hs else None,
                              "allowed_ips": ips[name] + "/32", "handshake": hs, "handshake_age": ts - hs if hs else None,
                              "rx": counters[name][1], "tx": counters[name][0],
                              "online": bool(hs) and ts - hs < 180, "container": cont, "client": name})
            r.peers = peers
            load = min(0.97, 0.04 + srv_rate_d / 3_500_000 + rnd.random() * 0.05)
            if n == "test1" and frac_left < 700:
                load = 0.91
            cpu[n][0] += int(2 * step * 100 * load)
            cpu[n][1] += 2 * step * 100
            r.cpu_raw = tuple(cpu[n])
            r.load = (round(load * 2.2, 2), round(load * 1.9, 2), round(load * 1.5, 2))
            total = 2 * 1024**3 if n != "test4" else 1024**3
            used = int(total * (0.35 + 0.08 * int(n[-1]) + load * 0.1))
            r.mem = {"total": total, "used": used, "percent": round(used * 100 / total, 1)}
            net[n][0] += int((srv_rate_u + srv_rate_d * 1.02) * step)  # вход: от клиентов + от сайтов
            net[n][1] += int((srv_rate_d + srv_rate_u * 1.02) * step)
            r.net = {"eth0": tuple(net[n])}
            conts = sorted({u[2] for u in DASH_USERS if u[1] == n})
            r.containers = [{"name": c, "status": "Up 15 days", "up": True, "type": am.container_type(c)} for c in conts]
            if n in ("test1", "test2"):
                r.containers.append({"name": "amnezia-dns", "status": "Up 15 days", "up": True, "type": "dns"})
            if n == "test1":
                xr = frac_left >= 1200
                r.containers.append({"name": "amnezia-xray", "status": "Up 15 days" if xr else "Exited (1) 15 minutes ago",
                                     "up": xr, "type": "xray"})
            # DNS: на test1 журнал включён (формат AdGuard), на test2 контейнер есть, но журнал выключен
            if n == "test1":
                lines = []
                for p in peers:
                    if not p["online"]:
                        continue
                    doms = next(u[5] for u in DASH_USERS if u[0] == p["client"])
                    for _ in range(rnd.randint(3, 14)):
                        dom = rnd.choices(doms, weights=[len(doms) - i for i in range(len(doms))])[0]
                        sub = rnd.choice(["", "www.", "api.", "cdn.", "rr3.sn-4g5e.", "i."])
                        q_ts = _dt.datetime.fromtimestamp(ts - rnd.randint(0, step - 1)).astimezone().isoformat()
                        lines.append(json.dumps({"T": q_ts, "QH": sub + dom, "QT": "A", "IP": p["allowed_ips"].split("/")[0]}))
                am.attach_dns(r, "\n".join(lines))
            elif n == "test2":
                am.attach_dns(r, "")
            am.process(store, cfg, r)


def demo_store(path=None, now=None):
    """Создаёт демо-базу для панели; возвращает (путь, cfg, now)."""
    import time
    now = now or time.time()
    path = path or os.path.join(tempfile.mkdtemp(prefix="amnezia-dash-demo-"), "demo.db")
    cfg = am.Config(servers=DEMO_SERVERS, interval=600)
    store = am.Store(path)
    build_dashboard_demo(store, cfg, now)
    store.db.close()
    return path, cfg, now
