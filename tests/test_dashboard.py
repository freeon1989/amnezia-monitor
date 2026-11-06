# -*- coding: utf-8 -*-
import json
import logging
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.getLogger("paramiko").setLevel(logging.CRITICAL + 1)
import amnezia_monitor as am  # noqa: E402
import dashboard  # noqa: E402
import dashboard_api as api  # noqa: E402
import demo_data  # noqa: E402

NOW = 1_800_000_000.0


class DNSParsing(unittest.TestCase):
    def test_adguard_and_unbound(self):
        text = "\n".join([
            '{"T":"2026-10-31T19:00:00+03:00","QH":"rr3.sn-4g5e.googlevideo.com","QT":"A","IP":"10.8.1.2"}',
            "[1800000000] unbound[1:0] info: 10.8.1.3 www.bbc.co.uk. A IN",
            "Oct 31 19:00:01 unbound[1:0]: info: 10.8.1.3 github.com. AAAA IN",
            "[1800000000] unbound[1:0] info: 10.8.1.3 1.0.8.10.in-addr.arpa. PTR IN",   # служебное — пропуск
            "[1800000000] unbound[1:0] info: start of service",                          # не запрос
            "garbage",
        ])
        e = am.parse_dns_log(text)
        self.assertEqual([x["domain"] for x in e], ["rr3.sn-4g5e.googlevideo.com", "www.bbc.co.uk", "github.com"])
        self.assertEqual(e[0]["ip"], "10.8.1.2")
        self.assertEqual(e[1]["ts"], 1800000000.0)
        self.assertIsNone(e[2]["ts"])
        self.assertEqual(am.root_domain("rr3.sn-4g5e.googlevideo.com"), "googlevideo.com")
        self.assertEqual(am.root_domain("www.bbc.co.uk"), "bbc.co.uk")
        self.assertEqual(am.root_domain("github.com"), "github.com")

    def test_ip_mapping_and_dedup(self):
        cfg = am.Config(servers=[am.ServerCfg(name="s", host="203.0.113.1")])
        store = am.Store(":memory:")
        peers = [{"pubkey": "K1=", "allowed_ips": "10.8.1.2/32", "client": "Телефон", "online": True,
                  "handshake": int(NOW) - 10, "rx": 1, "tx": 2, "endpoint": None, "container": "amnezia-awg"}]
        log1 = "[%d] u info: 10.8.1.2 a.example.com. A IN\n[%d] u info: 10.8.1.9 b.example.org. A IN\n" % (
            int(NOW) - 10, int(NOW) - 5)
        r = am.PollResult(server="s", ts=NOW, ssh_ok=True, reachable=True, tcp_ms=1, peers=peers)
        am.attach_dns(r, log1)
        am.process(store, cfg, r)
        # второй опрос: tail вернул старые строки + одну новую — старые не должны задвоиться
        r2 = am.PollResult(server="s", ts=NOW + 60, ssh_ok=True, reachable=True, tcp_ms=1, peers=peers)
        am.attach_dns(r2, log1 + "[%d] u info: 10.8.1.2 c.example.com. A IN\n" % (int(NOW) + 50))
        am.process(store, cfg, r2)
        rows = store.db.execute("SELECT client, site FROM dns ORDER BY id").fetchall()
        self.assertEqual([(x[0], x[1]) for x in rows],
                         [("Телефон", "example.com"), ("10.8.1.9", "example.org"), ("Телефон", "example.com")])

    def test_no_log_status(self):
        r = am.PollResult(server="s", ts=NOW)
        am.attach_dns(r, "   \n")
        self.assertEqual(r.dns_status, "no_log")

    def test_epoch_unbound_line(self):
        e = am.parse_dns_log("[1793965136] unbound[1:0] info: 10.8.1.2 example.com. A IN\n")
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["ts"], 1793965136.0)
        self.assertEqual(e[0]["ip"], "10.8.1.2")
        self.assertEqual(e[0]["domain"], "example.com")
        self.assertEqual(e[0]["qtype"], "A")

    def test_read_paths_statuses_and_rotation(self):
        cmd = am.READ_ONLY_COMMANDS["dns_log"].format(docker="docker", c="amnezia-dns", n=3000)
        self.assertIn("/opt/unbound/etc/unbound/unbound.log", cmd)
        self.assertIn("/opt/unbound/etc/unbound/log-archive/unbound-*.log", cmd)
        self.assertIn("/opt/adguardhome/work/data/querylog.json", cmd)
        self.assertTrue(am._ALLOWED_RE.match(cmd))
        self.assertLessEqual(am.DNS_READ_BYTES * (1 + am.DNS_ARCHIVE_FILES), 50 * 1024 * 1024)
        now = 1_793_965_200.0
        missing = "AMNEZIA_DNS_ACTIVE_MISSING\n"
        self.assertEqual(am.interpret_dns_capture(missing, 0, now)["status"], "no_log")
        self.assertEqual(am.interpret_dns_capture("", 1, now)["status"], "unreadable")
        self.assertEqual(am.interpret_dns_capture("\n", 0, now)["status"], "empty")
        self.assertEqual(am.interpret_dns_capture("AMNEZIA_DNS_ACTIVE_MISSING\nAMNEZIA_DNS_FILE\n", 0, now)["status"], "empty")
        garbage = "AMNEZIA_DNS_FILE\nthis is not a dns line\n"
        self.assertEqual(am.interpret_dns_capture(garbage, 0, now)["status"], "unsupported")
        fresh = "[1793965136] unbound[1:0] info: 10.8.1.2 example.com. A IN"
        old = "[1793000000] unbound[1:0] info: 10.8.1.2 old.example.net. A IN"
        cap = "AMNEZIA_DNS_FILE\n%s\n%s\n" % (old, fresh)
        info = am.interpret_dns_capture(cap, 0, now)
        self.assertEqual(info["status"], "ok")
        self.assertEqual([x["domain"] for x in info["entries"]], ["example.com"])
        self.assertLess(info["coverage_sec"], 24 * 3600)
        self.assertIn("не достраивает", info["note"])
        # повторное чтение архива не удваивает уже сохранённые строки
        cfg = am.Config(servers=[am.ServerCfg(name="s", host="203.0.113.1")])
        store = am.Store(":memory:")
        peers = [{"pubkey": "K1=", "allowed_ips": "10.8.1.2/32", "client": "Телефон", "online": True,
                  "handshake": int(now) - 10, "rx": 1, "tx": 2, "endpoint": None, "container": "amnezia-awg"}]
        r = am.PollResult(server="s", ts=now, ssh_ok=True, reachable=True, tcp_ms=1, peers=peers)
        am.apply_dns_result(r, info)
        am.process(store, cfg, r)
        r2 = am.PollResult(server="s", ts=now + 60, ssh_ok=True, reachable=True, tcp_ms=1, peers=peers)
        again = am.interpret_dns_capture(cap + "[1793965180] unbound[1:0] info: 10.8.1.2 news.example.org. A IN\n",
                                         0, now + 60)
        am.apply_dns_result(r2, again)
        am.process(store, cfg, r2)
        rows = store.db.execute("SELECT domain FROM dns ORDER BY ts").fetchall()
        self.assertEqual([x[0] for x in rows], ["example.com", "news.example.org"])

    def test_whitelist_dns(self):
        c = am.READ_ONLY_COMMANDS["dns_log"].format(docker="docker", c="amnezia-dns", n=3000)
        self.assertTrue(am._ALLOWED_RE.match(c))
        c = am.READ_ONLY_COMMANDS["dns_docker_logs"].format(docker="sudo -n docker", c="amnezia-dns", n=3000)
        self.assertTrue(am._ALLOWED_RE.match(c))
        self.assertFalse(am._ALLOWED_RE.match("docker logs --tail 10 amnezia-dns; rm -rf /"))


class Aggregation(unittest.TestCase):
    def test_bucketize(self):
        pts = [(0, 1), (10, 3), (55, 5), (99, None)]
        self.assertEqual(api.bucketize(pts, 0, 100, 2), [2.0, 5.0])
        self.assertEqual(api.bucketize(pts, 0, 100, 2, "sum"), [4, 5])
        self.assertEqual(api.bucketize([], 0, 100, 3), [None, None, None])

    def test_peer_deltas_and_reset(self):
        rows = [{"pubkey": "A", "ts": 0, "tx": 100, "rx": 10}, {"pubkey": "A", "ts": 60, "tx": 700, "rx": 70},
                {"pubkey": "A", "ts": 120, "tx": 50, "rx": 5}]   # сброс счётчиков
        d = api.peer_deltas(rows)["A"]
        self.assertEqual(d[0], (60, 600, 60, 60))
        self.assertEqual(d[1], (120, 50, 5, 60))

    def test_demo_snapshot(self):
        path, cfg, now = demo_data.demo_store(now=NOW)
        store = am.Store(path)
        servers = api.api_servers(store, cfg, now)
        self.assertEqual([s["name"] for s in servers], ["test1", "test2", "test3", "test4"])
        self.assertEqual({s["name"]: s["status"] for s in servers}["test3"], "down")
        d1 = api.api_server(store, cfg, "test1", now)
        self.assertEqual(d1["current"]["status"], "up")
        self.assertEqual(len(d1["series"]["down_bytes"]), 48)
        names = {u["client"]: u for u in d1["users"]}
        self.assertTrue(names["Дмитрий — ноутбук"]["online"])
        self.assertFalse(names["Дмитрий — телефон"]["online"])
        self.assertGreater(names["Дмитрий — ноутбук"]["rate_down"], 0)
        self.assertGreater(names["Дмитрий — ноутбук"]["down_24h"], 0)
        self.assertTrue(len(names["Мама"]["spark"]) > 5)
        self.assertEqual(d1["dns"]["status"], "ok")
        self.assertIn("youtube.com", [x["site"] for x in d1["dns"]["top"]])
        self.assertIn("Мама", [u["client"] for u in d1["dns"]["per_user"]])
        self.assertTrue(any("xray" in e["message"] for e in d1["events"]))
        d2 = api.api_server(store, cfg, "test2", now)
        self.assertEqual(d2["dns"]["status"], "no_log")
        self.assertIn("не найден", d2["dns"]["note"])
        d3 = api.api_server(store, cfg, "test3", now)
        self.assertEqual(d3["current"]["status"], "down")
        self.assertTrue(d3["current"]["stale"])
        self.assertIsNone(d3["users"][0]["rate_down"])
        self.assertEqual(d3["dns"]["status"], "no_container")
        snap = api.api_snapshot(store, cfg, now)
        json.dumps(snap, ensure_ascii=False)          # сериализуется
        html = dashboard.render_html(snap, demo=True)
        self.assertIn("window.__DATA__", html)
        self.assertNotIn("CHANGE_ME", html)            # пароли не утекают в панель


class HTTPServerTest(unittest.TestCase):
    def test_endpoints(self):
        path, cfg, _ = demo_data.demo_store()
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(lambda: am.Store(path), cfg, True))
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{port}"
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("Amnezia Monitor", page)
            self.assertIn('"demo": true', page)
            srv = json.loads(urllib.request.urlopen(base + "/api/servers").read())
            self.assertEqual(len(srv), 4)
            d = json.loads(urllib.request.urlopen(base + "/api/server?name=test1").read())
            self.assertEqual(d["name"], "test1")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/server?name=nope")
            self.assertEqual(cm.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
