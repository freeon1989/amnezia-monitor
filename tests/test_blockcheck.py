# -*- coding: utf-8 -*-
"""Проверка блокировок: вердикты на подставных результатах, клиент check-host, локальные пробы."""
import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import amnezia_monitor as am  # noqa: E402
import app_settings as st  # noqa: E402
import blockcheck as bc  # noqa: E402
import dashboard  # noqa: E402
import demo_data  # noqa: E402
import secrets_store  # noqa: E402

NOW = 1_800_000_000.0
OK = {"ok": True, "ms": 30.0}
FAIL = {"ok": False, "status": "timeout"}


def nodes(ok, n):
    return [dict(OK) for _ in range(ok)] + [dict(FAIL) for _ in range(n - ok)]


class PortVerdict(unittest.TestCase):
    def v(self, local, ru, world, proto="tcp"):
        return bc.port_verdict(proto, {"status": local} if local else None, ru, world, "SSH")

    def test_available_everywhere(self):
        r = self.v("ok", nodes(3, 3), nodes(8, 8))
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(r["title"], "Доступен везде")

    def test_dpi_reset_after_connect_is_ru_block(self):
        for local in ("reset_after_connect", "silent_after_connect", "reset_after_banner", "timeout_after_banner"):
            r = self.v(local, nodes(3, 3), nodes(8, 8))
            self.assertEqual(r["verdict"], "blocked_ru", local)
            self.assertEqual(r["cause"], "dpi")
            self.assertIn("ТСПУ/DPI", r["title"])
            self.assertIn("SSH", r["title"])

    def test_ip_block_when_ru_cannot_connect(self):
        r = self.v("timeout", nodes(0, 3), nodes(8, 8))
        self.assertEqual((r["verdict"], r["cause"]), ("blocked_ru", "ip"))
        r = self.v(None, nodes(0, 3), nodes(7, 8))          # без локальной проверки, по узлам РФ
        self.assertEqual((r["verdict"], r["cause"]), ("blocked_ru", "ip"))
        r = self.v("refused", [], nodes(8, 8))                # узлов РФ нет, локально отказ
        self.assertEqual((r["verdict"], r["cause"]), ("blocked_ru", "ip"))

    def test_isp_block_when_only_this_pc_fails(self):
        r = self.v("timeout", nodes(3, 3), nodes(8, 8))
        self.assertEqual((r["verdict"], r["cause"]), ("blocked_ru", "isp"))

    def test_down_everywhere(self):
        r = self.v("timeout", nodes(0, 3), nodes(0, 8))
        self.assertEqual(r["verdict"], "down")

    def test_partial(self):
        self.assertEqual(self.v("ok", nodes(3, 3), nodes(3, 8))["verdict"], "partial")     # мир меньшинством
        self.assertEqual(self.v("ok", nodes(0, 3), nodes(0, 8))["verdict"], "partial")     # из РФ есть, из мира нет
        self.assertEqual(self.v("ok", nodes(0, 3), nodes(8, 8))["verdict"], "partial")     # узлы РФ не могут, ПК может
        self.assertEqual(self.v("ok", nodes(1, 3), nodes(8, 8))["verdict"], "partial")
        r = self.v("ok", nodes(3, 3), nodes(6, 8))                                          # большинство мира — ок
        self.assertEqual(r["verdict"], "ok")
        self.assertIn("6 из 8", r["detail"])

    def test_without_world_data(self):
        self.assertEqual(self.v("ok", [], [])["verdict"], "ok")
        r = self.v("reset_after_banner", [], [])
        self.assertEqual(r["verdict"], "blocked_ru")
        self.assertIn("низкая", r["detail"])
        self.assertEqual(self.v("timeout", [], [])["verdict"], "unknown")
        self.assertEqual(self.v(None, [], [])["verdict"], "unknown")

    def test_pending_nodes_are_ignored(self):
        r = self.v("ok", [{"ok": None}] * 3, nodes(8, 8) + [{"ok": None}])
        self.assertEqual(r["verdict"], "ok")

    def test_tls_alert_counts_as_alive(self):
        self.assertEqual(self.v("tls_alert", nodes(3, 3), nodes(8, 8), "tls")["verdict"], "ok")

    def test_udp_is_unverifiable(self):
        r = self.v(None, [], [], proto="udp")
        self.assertEqual(r["verdict"], "unverifiable")
        self.assertEqual(r["title"], "Не проверяется извне")


class ServerVerdict(unittest.TestCase):
    def p(self, verdict, port=22, label="SSH", title=None):
        return {"verdict": verdict, "port": port, "label": label, "title": title or bc.VERDICT_TITLE[verdict]}

    def test_block_wins(self):
        r = bc.server_verdict([self.p("ok", 443, "Xray/Reality"),
                               self.p("blocked_ru", title="Признаки блокировки SSH в РФ (ТСПУ/DPI сброс соединения)"),
                               self.p("unverifiable", 51820, "AmneziaWG")])
        self.assertEqual(r["verdict"], "blocked_ru")
        self.assertEqual(r["title"], "Признаки блокировки SSH в РФ (ТСПУ/DPI сброс соединения)")
        self.assertEqual(r["ports"], [22])
        two = bc.server_verdict([self.p("blocked_ru"), self.p("blocked_ru", 443, "Xray/Reality")])
        self.assertIn("SSH:22, Xray/Reality:443", two["title"])

    def test_all_same_and_mixed(self):
        self.assertEqual(bc.server_verdict([self.p("down"), self.p("down", 443)])["verdict"], "down")
        self.assertEqual(bc.server_verdict([self.p("ok"), self.p("down", 443)])["verdict"], "partial")
        self.assertEqual(bc.server_verdict([self.p("ok"), self.p("unknown", 443)])["verdict"], "ok")
        self.assertEqual(bc.server_verdict([self.p("unverifiable")])["verdict"], "unverifiable")


class Parsing(unittest.TestCase):
    def test_tcp_result(self):
        self.assertIsNone(bc.parse_tcp_result(None))
        self.assertEqual(bc.parse_tcp_result([{"time": 0.031, "address": "x"}]), {"ok": True, "status": "ok", "ms": 31.0})
        self.assertEqual(bc.parse_tcp_result([{"error": "Connection timed out"}])["status"], "timeout")
        self.assertEqual(bc.parse_tcp_result([{"error": "Connection refused"}])["status"], "refused")
        self.assertEqual(bc.parse_tcp_result([{"error": "Connection reset by peer"}])["status"], "reset")
        self.assertFalse(bc.parse_tcp_result([None])["ok"])

    def test_ping_result(self):
        r = bc.parse_ping_result([[["OK", 0.044, "1.2.3.4"], ["TIMEOUT", 3.0], ["OK", 0.046]]])
        self.assertEqual((r["found"], r["ok"], r["n"], r["ms"]), (True, 2, 3, 45.0))
        self.assertFalse(bc.parse_ping_result([[None]])["found"])
        self.assertIsNone(bc.parse_ping_result(None))

    def test_pick_nodes(self):
        n = {f"{cc}{i}.node.check-host.net": {"cc": cc, "country": "", "city": ""}
             for cc in ("ru", "ir", "de", "us", "nl", "by") for i in (1, 2)}
        ru, world = bc.pick_nodes(n, max_world=3)
        self.assertEqual(ru, ["ru1.node.check-host.net", "ru2.node.check-host.net"])
        self.assertEqual(world, ["de1.node.check-host.net", "nl1.node.check-host.net", "us1.node.check-host.net"])
        self.assertFalse(any(w.startswith(("ir", "by", "ru")) for w in bc.pick_nodes(n)[1]))

    def test_ports_from_docker_ps_and_wg(self):
        ps = ("amnezia-awg|Up 2 days|amneziavpn/amnezia-awg|0.0.0.0:42000->42000/udp, [::]:42000->42000/udp\n"
              "amnezia-xray|Up 2 days|amneziavpn/amnezia-xray|0.0.0.0:443->443/tcp, :::443->443/tcp\n"
              "cascade-xray|Up 1 day|teddysun/xray|\n"
              "amnezia-dns|Up 2 days|amneziavpn/amnezia-dns|53/udp\n"
              "portainer|Up 3 days|portainer/portainer-ce|0.0.0.0:9000->9000/tcp\n"
              "amnezia-openvpn|Exited (1) 1 hour ago|amneziavpn/openvpn|\n"
              "old|Up|img\n")
        got = am.parse_published_ports(ps)
        self.assertEqual([(x["container"], x["port"], x["proto"]) for x in got],
                         [("amnezia-awg", 42000, "udp"), ("amnezia-xray", 443, "tcp")])
        self.assertEqual(len(am.parse_docker_ps(ps)), 4)        # 4-е поле не ломает старый разбор
        dump = ("awg0\tPRIV=\tPUB=\t42000\toff\t4\t40\t70\t0\t0\t1\t2\t3\t4\n"
                "awg0\tPEER=\t(none)\t198.51.100.7:1\t10.8.1.5/32\t1\t5\t6\t25\n")
        self.assertEqual(am.parse_wg_listen_ports(dump), [42000])
        self.assertEqual(am.parse_wg_listen_ports("wg0\tPRIVKEY=\tSRVPUB=\t51820\toff\n"), [51820])

    def test_port_list(self):
        self.assertEqual(am.parse_port_list("8443/tls, 2053 51820/UDP;2053/tcp"),
                         [(8443, "tls"), (2053, "tcp"), (51820, "udp")])
        self.assertEqual(am.parse_port_list(""), [])
        for bad in ("70000", "abc", "22/sctp"):
            with self.assertRaises(ValueError):
                am.parse_port_list(bad)

    def test_targets(self):
        srv = am.ServerCfg(name="s", host="203.0.113.5", port=2222, block_ports=[(8443, "tls"), (2222, "tcp")])
        disc = [{"container": "amnezia-awg", "type": "awg", "port": 42000, "proto": "udp"},
                {"container": "amnezia-xray", "type": "xray", "port": 443, "proto": "tcp"},
                {"container": "amnezia-openvpn", "type": "openvpn", "port": 1194, "proto": "tcp"}]
        t = bc.targets_for(srv, disc)
        self.assertEqual([(x["port"], x["proto"], x["probe"]) for x in t],
                         [(2222, "tcp", "ssh"), (42000, "udp", None), (443, "tls", "tls"),
                          (1194, "tcp", "tcp"), (8443, "tls", "tls")])

    def test_config_block_settings(self):
        d = Path(tempfile.mkdtemp())
        (d / "servers.yaml").write_text(
            "block_check: {enabled: false, interval_hours: 12}\n"
            "servers:\n  - {name: a, host: 203.0.113.1, password: x, block_ports: '8443/tls, 51820/udp'}\n",
            encoding="utf-8")
        cfg = am.load_config(str(d / "servers.yaml"))
        self.assertFalse(cfg.block_check_enabled)
        self.assertEqual(cfg.block_check_hours, 12)
        self.assertEqual(cfg.servers[0].block_ports, [(8443, "tls"), (51820, "udp")])
        (d / "servers.yaml").write_text("servers:\n  - {name: a, host: 203.0.113.1, password: x}\n", encoding="utf-8")
        cfg = am.load_config(str(d / "servers.yaml"))
        self.assertTrue(cfg.block_check_enabled)
        self.assertEqual(cfg.block_check_hours, 6)


class FakeHTTP:
    """Ответы check-host по пути: значение — dict или список (по очереди)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, params, timeout):
        path = url.replace(bc.CheckHost.BASE, "")
        self.calls.append((path, params))
        for prefix, resp in self.routes.items():
            if path.startswith(prefix):
                if isinstance(resp, list):
                    return 200, (resp.pop(0) if len(resp) > 1 else resp[0])
                if isinstance(resp, tuple):
                    return resp
                return 200, resp
        return 404, None


class CheckHostClient(unittest.TestCase):
    def setUp(self):
        bc.CheckHost._nodes_cache, bc.CheckHost._nodes_ts = {}, 0.0

    def client(self, http):
        return bc.CheckHost(http_get=http, sleep=lambda s: None, min_gap=0)

    def test_limit_exceeded(self):
        c = self.client(FakeHTTP({"/nodes/hosts": {"error": "limit_exceeded"}}))
        with self.assertRaises(bc.CheckHostLimited):
            c.nodes()
        c = self.client(FakeHTTP({"/nodes/hosts": (429, None)}))
        with self.assertRaises(bc.CheckHostLimited):
            c.nodes()
        c = self.client(FakeHTTP({"/nodes/hosts": (502, None)}))
        with self.assertRaises(bc.CheckHostError):
            c.nodes()

        def boom(*a):
            raise OSError("no net")
        with self.assertRaises(bc.CheckHostError):
            self.client(boom).nodes()

    def test_nodes_start_and_wait(self):
        http = FakeHTTP({
            "/nodes/hosts": {"nodes": {"ru1.node.check-host.net": {"location": ["ru", "Russia", "Moscow"]},
                                       "de1.node.check-host.net": {"location": ["de", "Germany", "Frankfurt"]}}},
            "/check-tcp": {"ok": 1, "request_id": "abc", "permanent_link": "https://check-host.net/check-report/abc"},
            "/check-result/abc": [{"ru1.node.check-host.net": None, "de1.node.check-host.net": [{"time": 0.05}]},
                                  {"ru1.node.check-host.net": [{"error": "Connection timed out"}],
                                   "de1.node.check-host.net": [{"time": 0.05}]}],
        })
        c = self.client(http)
        self.assertEqual(c.nodes()["ru1.node.check-host.net"]["city"], "Moscow")
        r = c.start("tcp", "203.0.113.5:22", ["ru1.node.check-host.net", "de1.node.check-host.net"])
        self.assertEqual(r["request_id"], "abc")
        self.assertIn(("node", "de1.node.check-host.net"), http.calls[-1][1])
        res = c.wait(["abc"])["abc"]
        self.assertIsNotNone(res["ru1.node.check-host.net"])          # дождался всех узлов
        self.assertEqual(sum(1 for p, _ in http.calls if p.startswith("/check-result")), 2)


def fake_local(status_by_port):
    def probe(t, host, sni=None):
        if t.get("probe") is None:
            return {"status": "skipped", "ms": None}
        return {"status": status_by_port.get(t["port"], "ok"), "ms": 40.0, "detail": "x"}
    return probe


class LimitedCheckHost:
    def nodes(self):
        raise bc.CheckHostLimited("check-host.net ограничил частоту запросов")


class CheckServer(unittest.TestCase):
    def srv(self):
        return demo_data.DEMO_SERVERS[0]          # test1, 203.0.113.11

    def test_dpi_with_demo_checkhost(self):
        disc = [{"container": "amnezia-xray", "type": "xray", "port": 443, "proto": "tcp"},
                {"container": "amnezia-awg", "type": "awg", "port": 51820, "proto": "udp"}]
        r = bc.check_server(self.srv(), disc, demo_data.DemoCheckHost(),
                            fake_local({22: "reset_after_banner"}), now=NOW,
                            awg={"ts": NOW, "peers": 3, "online": 2})
        self.assertEqual(r["verdict"], "blocked_ru")
        self.assertEqual(r["title"], "Признаки блокировки SSH в РФ (ТСПУ/DPI сброс соединения)")
        by = {p["port"]: p for p in r["ports"]}
        self.assertEqual(by[22]["summary"]["ru"], {"n": 3, "ok": 3, "ms": by[22]["summary"]["ru"]["ms"]})
        self.assertEqual(by[443]["verdict"], "ok")
        self.assertEqual(by[51820]["verdict"], "unverifiable")
        self.assertIn("2 из 3", by[51820]["detail"])
        self.assertEqual(r["ping"]["summary"]["world"]["found"], 8)
        self.assertTrue(r["checkhost"]["ok"])
        self.assertEqual(by[22]["kind"], "ssh")

    def test_checkhost_limited_falls_back_to_local(self):
        r = bc.check_server(self.srv(), [], LimitedCheckHost(), fake_local({}), now=NOW)
        self.assertTrue(r["checkhost"]["limited"])
        self.assertIn("только порты с этого ПК", r["checkhost"]["error"])
        self.assertEqual(r["verdict"], "ok")
        self.assertIn("Из-за рубежа не проверено", r["ports"][0]["detail"])


class StorageAndRunner(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = str(self.dir / "m.db")
        am.Store(self.db).db.close()
        self.cfg = am.Config(servers=list(demo_data.DEMO_SERVERS[:2]))
        self.clock = [NOW]

    def store(self):
        return am.Store(self.db)

    def test_events_only_on_change(self):
        s = self.store()
        base = {"server": "test1", "host": "h", "ports": [], "title": "Доступен везде"}
        self.assertIsNone(bc.save_result(s, dict(base, ts=NOW, verdict="ok")))           # первый «ок» — тихо
        self.assertIsNone(bc.save_result(s, dict(base, ts=NOW + 1, verdict="ok")))
        msg = bc.save_result(s, dict(base, ts=NOW + 2, verdict="blocked_ru", title="Признаки блокировки SSH в РФ"))
        self.assertIn("🚫 test1", msg)
        self.assertIn("✅", bc.save_result(s, dict(base, ts=NOW + 3, verdict="ok")))
        ev = [r["kind"] for r in s.db.execute("SELECT kind FROM events")]
        self.assertEqual(ev, ["block_change", "block_change"])
        self.assertEqual([h["verdict"] for h in bc.history(s, "test1")], ["ok", "blocked_ru", "ok", "ok"])

    def test_cooldown_and_start(self):
        chk = bc.BlockChecker(self.store, lambda: self.cfg, checkhost_factory=demo_data.DemoCheckHost,
                              local=demo_data.demo_local_probe, clock=lambda: self.clock[0])
        self.assertTrue(chk.start("test2", wait=True)["started"])
        s = self.store()
        status = bc.status_for(s, "test2", now=NOW + 60)
        self.assertEqual(status["result"]["verdict"], "ok")
        self.assertFalse(status["running"])
        self.assertEqual(status["retry_after"], bc.COOLDOWN_SEC - 60)
        self.clock[0] = NOW + 60
        r = chk.start("test2")
        self.assertFalse(r["started"])
        self.assertIn("через 29 мин", r["message"])
        self.clock[0] = NOW + bc.COOLDOWN_SEC + 1
        self.assertTrue(chk.start("test2", wait=True)["started"])
        with self.assertRaises(ValueError):
            chk.start("nope")

    def test_limited_sets_backoff(self):
        chk = bc.BlockChecker(self.store, lambda: self.cfg, checkhost_factory=LimitedCheckHost,
                              local=fake_local({}), clock=lambda: self.clock[0])
        chk.start("test1", wait=True)
        s = self.store()
        self.assertEqual(s.get_state("checkhost_backoff_until"), NOW + bc.CHECKHOST_BACKOFF_SEC)
        calls = []
        chk2 = bc.BlockChecker(self.store, lambda: self.cfg, checkhost_factory=lambda: calls.append(1),
                               local=fake_local({}), clock=lambda: NOW + 60)
        chk2.start("test2", wait=True)
        self.assertEqual(calls, [])                                  # во время паузы check-host не трогаем
        self.assertIn("пауза", bc.latest(self.store(), "test2")["checkhost"]["error"])

    def test_due_servers(self):
        chk = bc.BlockChecker(self.store, lambda: self.cfg, checkhost_factory=demo_data.DemoCheckHost,
                              local=demo_data.demo_local_probe, clock=lambda: self.clock[0])
        self.assertEqual([x.name for x in chk.due_servers(NOW)], ["test1", "test2"])
        chk.start("test1", wait=True)
        self.assertEqual([x.name for x in chk.due_servers(NOW + 3600)], ["test2"])
        self.assertEqual(len(chk.due_servers(NOW + 6 * 3600 + 1)), 2)
        self.cfg.block_check_enabled = False
        self.assertEqual(chk.due_servers(NOW + 99999), [])


class LocalProbes(unittest.TestCase):
    """Пробы с этого ПК против локальных сокетов: настоящий баннер, сброс после обмена, тишина."""

    def server(self, behaviour):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        self.addCleanup(srv.close)

        def run():
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                behaviour(c)
            except OSError:
                pass
            finally:
                c.close()
        threading.Thread(target=run, daemon=True).start()
        return srv.getsockname()[1]

    @staticmethod
    def rst(c):
        import struct
        c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    def test_good_ssh(self):
        def b(c):
            c.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
            c.recv(100)
            c.sendall(b"\x00\x00\x01\x00kexinit")
            time.sleep(0.3)
        r = bc.probe_ssh("127.0.0.1", self.server(b))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["banner"], "SSH-2.0-OpenSSH_9.6")

    def test_rst_after_banner_exchange(self):
        def b(c):
            c.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
            c.recv(100)
            self.rst(c)
        self.assertEqual(bc.probe_ssh("127.0.0.1", self.server(b))["status"], "reset_after_banner")

    def test_rst_right_after_connect(self):
        def b(c):
            time.sleep(0.1)
            self.rst(c)
        self.assertEqual(bc.probe_ssh("127.0.0.1", self.server(b))["status"], "reset_after_connect")

    def test_silence_after_connect(self):
        r = bc.probe_ssh("127.0.0.1", self.server(lambda c: time.sleep(2)), banner_timeout=0.5)
        self.assertEqual(r["status"], "silent_after_connect")

    def test_refused_and_tcp(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(bc.probe_tcp("127.0.0.1", port, timeout=2)["status"], "refused")
        self.assertEqual(bc.probe_tcp("127.0.0.1", self.server(lambda c: time.sleep(0.2)))["status"], "ok")

    def test_tls_reset_and_alert(self):
        def reset(c):
            c.recv(2000)               # ClientHello
            self.rst(c)
        self.assertEqual(bc.probe_tls("127.0.0.1", self.server(reset))["status"], "reset_after_connect")

        def alert(c):
            c.recv(2000)
            c.sendall(bytes([21, 3, 3, 0, 2, 2, 40]))       # TLS alert: handshake_failure
            time.sleep(0.2)
        self.assertEqual(bc.probe_tls("127.0.0.1", self.server(alert))["status"], "tls_alert")
        r = bc.probe_tls("127.0.0.1", self.server(lambda c: time.sleep(2)), hs_timeout=0.5)
        self.assertEqual(r["status"], "silent_after_connect")


class SettingsAndApi(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(secrets_store, "IS_WINDOWS", False)
        p.start()
        self.addCleanup(p.stop)
        self.dir = Path(tempfile.mkdtemp())
        self.cfgp = self.dir / "servers.yaml"

    def test_settings_roundtrip(self):
        raw = st.apply_update({}, {"servers": [{"name": "a", "host": "203.0.113.1", "password": "pw",
                                                "block_ports": "8443/TLS 51820/udp"}],
                                   "block_check": {"enabled": False, "interval_hours": 12}})
        self.assertEqual(raw["servers"][0]["block_ports"], "8443/tls, 51820/udp")
        self.assertEqual(raw["block_check"], {"enabled": False, "interval_hours": 12})
        v = st.public_view(raw)
        self.assertEqual(v["servers"][0]["block_ports"], "8443/tls, 51820/udp")
        self.assertFalse(v["block_check"]["enabled"])
        # сохранение без блока general не сбрасывает настройку фоновой проверки
        raw2 = st.apply_update(raw, {"servers": [{"orig_name": "a", "name": "a", "host": "203.0.113.1"}]})
        self.assertEqual(raw2["block_check"]["interval_hours"], 12)
        for bad in ({"block_ports": "99999"}, ):
            with self.assertRaises(st.SettingsError):
                st.apply_update({}, {"servers": [dict({"name": "a", "host": "203.0.113.1"}, **bad)]})
        with self.assertRaises(st.SettingsError):
            st.apply_update({}, {"servers": [], "block_check": {"interval_hours": 0}})

    def test_http_endpoints(self):
        st.write_raw(self.cfgp, st.apply_update({}, {"servers": [{"name": "test2", "host": "203.0.113.12",
                                                                  "password": "pw"}]}))
        db = str(self.dir / "m.db")
        am.Store(db).db.close()
        cfg = am.load_config(str(self.cfgp))
        chk = bc.BlockChecker(lambda: am.Store(db), lambda: cfg, checkhost_factory=demo_data.DemoCheckHost,
                              local=demo_data.demo_local_probe)
        ctx = st.SettingsContext(self.cfgp, "TOKEN123", str(self.dir), blocks=chk)
        httpd = dashboard.make_server(lambda: am.Store(db), lambda: cfg, "127.0.0.1", 0, settings=ctx)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))

        def req(method, path, body=None, token="TOKEN123"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            h = {"X-Amnezia-Token": token} if token else {}
            data = None
            if body is not None:
                data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            out = (r.status, json.loads(r.read().decode() or "{}"))
            c.close()
            return out

        code, j = req("GET", "/api/blocks?name=test2")
        self.assertEqual((code, j["result"]), (200, None))
        self.assertEqual(req("GET", "/api/blocks?name=nope")[0], 404)
        self.assertEqual(req("POST", "/api/blocks/check", {"name": "test2"}, token=None)[0], 403)
        self.assertEqual(req("POST", "/api/blocks/check", {"name": "nope"})[0], 400)
        code, j = req("POST", "/api/blocks/check", {"name": "test2"})
        self.assertTrue(j["started"])
        for _ in range(50):
            code, j = req("GET", "/api/blocks?name=test2")
            if j["result"] and not j["running"]:
                break
            time.sleep(0.1)
        self.assertEqual(j["result"]["verdict"], "ok")
        self.assertGreater(j["retry_after"], 0)
        code, j = req("POST", "/api/blocks/check", {"name": "test2"})
        self.assertFalse(j["started"])
        code, servers = req("GET", "/api/servers")
        self.assertEqual(servers[0]["block"]["verdict"], "ok")


if __name__ == "__main__":
    unittest.main()
