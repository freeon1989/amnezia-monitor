# -*- coding: utf-8 -*-
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import logging  # noqa: E402
logging.getLogger("paramiko").setLevel(logging.CRITICAL + 1)
import amnezia_monitor as am  # noqa: E402
import demo_data  # noqa: E402

NOW = 1_800_000_000

WG_DUMP = (
    "wg0\tPRIVKEY=\tSRVPUB=\t51820\toff\n"
    f"wg0\tPEER1=\t(none)\t198.51.100.5:51000\t10.8.1.2/32\t{NOW - 30}\t1000\t2000\t25\n"
    f"wg0\tPEER2=\t(none)\t198.51.100.6:51001\t10.8.1.3/32\t{NOW - 600}\t10\t20\toff\n"
    "wg0\tPEER3=\t(none)\t(none)\t10.8.1.4/32\t0\t0\t0\toff\n"
)
# AmneziaWG: в строке интерфейса бывают дополнительные поля обфускации
AWG_DUMP = "awg0\tPRIV=\tPUB=\t42000\toff\t4\t40\t70\t0\t0\t1\t2\t3\t4\n" + \
    f"awg0\tPEERA=\t(none)\t198.51.100.7:1\t10.8.1.5/32\t{NOW - 100}\t5\t6\t25\n"


class ParserTests(unittest.TestCase):
    def test_wg_dump(self):
        peers = am.parse_wg_dump(WG_DUMP, NOW)
        self.assertEqual([p["pubkey"] for p in peers], ["PEER1=", "PEER2=", "PEER3="])
        self.assertEqual([p["online"] for p in peers], [True, False, False])
        self.assertIsNone(peers[2]["endpoint"])
        self.assertEqual(peers[0]["rx"], 1000)
        self.assertNotIn("PRIVKEY=", repr(peers))   # приватный ключ не попадает в результат

    def test_awg_dump_extra_fields(self):
        peers = am.parse_wg_dump(AWG_DUMP, NOW)
        self.assertEqual(len(peers), 1)
        self.assertTrue(peers[0]["online"])
        self.assertNotIn("PRIV=", repr(peers))

    def test_threshold_boundary(self):
        d = f"wg0\tK=\t(none)\t1.2.3.4:1\t10.0.0.2/32\t{NOW - 180}\t0\t0\toff\n"
        self.assertFalse(am.parse_wg_dump(d, NOW)[0]["online"])
        d = d.replace(str(NOW - 180), str(NOW - 179))
        self.assertTrue(am.parse_wg_dump(d, NOW)[0]["online"])

    def test_clients_table(self):
        t = '[{"clientId":"PEER1=","userData":{"clientName":"Телефон"}},{"clientId":"X"}]'
        self.assertEqual(am.parse_clients_table(t), {"PEER1=": "Телефон"})
        self.assertEqual(am.parse_clients_table("garbage"), {})

    def test_docker_ps(self):
        cs = am.parse_docker_ps("amnezia-awg|Up 2 days|amneziavpn/amnezia-awg\n"
                                "amnezia-xray|Exited (1) 1 min ago|x\nportainer|Up|portainer\n")
        self.assertEqual([(c["name"], c["type"], c["up"]) for c in cs],
                         [("amnezia-awg", "awg", True), ("amnezia-xray", "xray", False)])

    def test_meminfo_stat_net(self):
        m = am.parse_meminfo("MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 250 kB\n")
        self.assertEqual(m["percent"], 75.0)
        a = am.parse_proc_stat("cpu  100 0 0 900 0 0 0 0 0 0\n")
        b = am.parse_proc_stat("cpu  150 0 0 950 0 0 0 0 0 0\n")
        self.assertEqual(am.cpu_percent(a, b), 50.0)
        n1 = am.parse_net_dev("  lo: 1 0 0 0 0 0 0 0 1 0 0 0 0 0 0 0\n eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n")
        n2 = {"eth0": (7000, 2600)}
        self.assertNotIn("lo", n1)
        self.assertEqual(am.net_rates(n1, n2, 60), (100.0, 10.0))
        self.assertEqual(am.parse_loadavg("0.5 0.4 0.3 1/2 3"), (0.5, 0.4, 0.3))

    def test_whitelist(self):
        for key in ("uptime", "loadavg", "nproc", "meminfo", "stat", "netdev", "docker_ps"):
            for d in ("docker", "sudo -n docker"):
                self.assertTrue(am._ALLOWED_RE.match(am.READ_ONLY_COMMANDS[key].format(docker=d)), key)
        self.assertTrue(am._ALLOWED_RE.match(am.READ_ONLY_COMMANDS["wg_dump"].format(docker="docker", c="amnezia-awg")))
        self.assertTrue(am._ALLOWED_RE.match(am.READ_ONLY_COMMANDS["clients"].format(docker="docker", c="amnezia-awg", t="awg")))
        for bad in ("rm -rf /", "docker stop amnezia-awg", "docker exec x sh -c 'wg set wg0 peer X remove'".replace("wg set", "rm"),
                    "systemctl restart docker", "cat /etc/shadow"):
            self.assertFalse(am._ALLOWED_RE.match(bad), bad)


class ConfigTests(unittest.TestCase):
    def write(self, text, env=None):
        d = tempfile.mkdtemp()
        Path(d, "servers.yaml").write_text(text, encoding="utf-8")
        if env:
            Path(d, ".env").write_text(env, encoding="utf-8")
        return str(Path(d, "servers.yaml"))

    def test_example_loads(self):
        cfg = am.load_config(str(ROOT / "servers.yaml.example"))
        self.assertEqual(len(cfg.servers), 4)
        self.assertEqual(len({s.password for s in cfg.servers}), 4)   # у каждого свой пароль
        self.assertTrue(all(s.host.startswith("203.0.113.") for s in cfg.servers))
        self.assertNotIn("CHANGE_ME", repr(cfg.servers))               # пароль не в repr

    def test_password_env(self):
        os.environ.pop("T_SRV_PASS", None)
        p = self.write("servers:\n  - {name: a, host: 203.0.113.1, password_env: T_SRV_PASS}\n",
                       env="T_SRV_PASS=s3cret\n")
        self.assertEqual(am.load_config(p).servers[0].password, "s3cret")
        os.environ.pop("T_SRV_PASS", None)

    def test_missing_env_fails(self):
        os.environ.pop("T_NOPE", None)
        p = self.write("servers:\n  - {name: a, host: 203.0.113.1, password_env: T_NOPE}\n")
        with self.assertRaises(am.ConfigError):
            am.load_config(p)

    def test_shared_default_password_rejected(self):
        p = self.write("defaults: {password: x}\nservers:\n  - {name: a, host: 203.0.113.1}\n")
        with self.assertRaises(am.ConfigError):
            am.load_config(p)


class DemoEndToEnd(unittest.TestCase):
    def test_demo_events(self):
        cfg = am.Config(servers=demo_data.DEMO_SERVERS, interval=60)
        store = am.Store(":memory:")
        n = am.Notifier(cfg, force_print=True)
        allev = []
        for tick, sc in enumerate(demo_data.SCENARIO):
            now = NOW + tick * 60
            allev += am.do_check(cfg, store, n, runner_factory=lambda s, t=tick, nw=now: demo_data.FakeRunner(s, t, nw),
                                 probe=lambda s, sc=sc: (None, None) if s.name in sc["down"] else (10.0, 9.0), now=now)
        text = "\n".join(allev)
        for needle in ("подключился Дмитрий — ноутбук", "отключился Дмитрий — телефон",
                       "amnezia-xray остановлен", "высокая загрузка CPU", "test3: сервер недоступен"):
            self.assertIn(needle, text)
        s = am.build_summary(store, cfg, 24, now=NOW + 120)
        self.assertIn("Клиенты онлайн: 2 из 4", s)
        rows = store.db.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
        self.assertGreater(rows, 0)
        self.assertNotIn("PRIVATE_KEY", str(store.db.execute("SELECT * FROM peers").fetchall()))


# --------------------- Реальный SSH-путь: встроенный SSH-сервер paramiko --------------------
try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None


class _FakeSSHServer(paramiko.ServerInterface if paramiko else object):
    def __init__(self, password, log):
        self.password, self.log, self.cmd = password, log, None
        self.event = threading.Event()

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username, password) == ("monitor", self.password) else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        self.cmd = command.decode()
        self.log.append(self.cmd)
        self.event.set()
        return True


def _serve(sock, password, log, stop):
    host_key = paramiko.RSAKey.generate(2048)
    fake = demo_data.FakeRunner(am.ServerCfg(name="test1", host="x"), 1, time.time())
    while not stop.is_set():
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        t = paramiko.Transport(conn)
        t.add_server_key(host_key)
        srv = _FakeSSHServer(password, log)
        try:
            t.start_server(server=srv)
        except Exception:
            continue
        def handle(t=t, srv=srv):
            while t.is_active():
                ch = t.accept(5)
                if ch is None:
                    break
                srv.event.wait(5)
                rc, out, err = fake.run(srv.cmd) if am._ALLOWED_RE.match(srv.cmd) else (126, "", "denied")
                ch.sendall(out.encode())
                ch.send_exit_status(rc)
                ch.close()
                srv.event.clear()
        threading.Thread(target=handle, daemon=True).start()


@unittest.skipIf(paramiko is None, "paramiko не установлен")
class SSHPathTest(unittest.TestCase):
    def test_password_ssh_collect(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        port = sock.getsockname()[1]
        log, stop = [], threading.Event()
        threading.Thread(target=_serve, args=(sock, "CHANGE_ME_1", log, stop), daemon=True).start()
        os.environ["HOME"] = tempfile.mkdtemp()   # не трогаем настоящий known_hosts
        try:
            srv = am.ServerCfg(name="test1", host="127.0.0.1", port=port, user="monitor", password="CHANGE_ME_1")
            r = am.collect(srv, am.SSHRunner, time.time(), {}, 180)
            self.assertTrue(r.ssh_ok, r.error)
            self.assertIsNone(r.error)
            self.assertEqual(r.nproc, 2)
            self.assertEqual({c["name"] for c in r.containers}, {"amnezia-awg", "amnezia-xray", "amnezia-dns"})
            self.assertEqual(len(r.peers), 4)
            self.assertIn("Дмитрий — ноутбук", [p["client"] for p in r.peers if p["online"]])
            self.assertTrue(all(am._ALLOWED_RE.match(c) for c in log))
            # неверный пароль -> аккуратная ошибка без утечки пароля
            bad = am.ServerCfg(name="test1", host="127.0.0.1", port=port, user="monitor", password="WRONG")
            r2 = am.collect(bad, am.SSHRunner, time.time(), {}, 180)
            self.assertFalse(r2.ssh_ok)
            self.assertIn("AuthenticationException", r2.error)
            self.assertNotIn("WRONG", r2.error)
        finally:
            stop.set()
            sock.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
