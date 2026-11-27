# -*- coding: utf-8 -*-
"""Режим каскада: выключен по умолчанию, миграция старой базы, импорт без секретов, опрос звеньев."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import amnezia_monitor as am
import app_settings as st
import cascade
import dashboard_api as api
import demo_data


class ParseAndSettings(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(cascade.parse_cascade({}).enabled)
        self.assertFalse(cascade.parse_cascade({"cascade": {"enabled": False}}).enabled)
        raw = st.apply_update({}, {"servers": [{"name": "a", "host": "203.0.113.1"}]})
        self.assertNotIn("cascade", raw)
        v = st.public_view(raw)
        self.assertEqual(v["cascade"], {"enabled": False, "links": []})

    def test_preserve_when_omitted(self):
        raw = st.apply_update({}, {"servers": [
            {"name": "Питер", "host": "203.0.113.21"},
            {"name": "Амстердам", "host": "203.0.113.22"},
        ], "cascade": {"enabled": True, "links": [{"entry": "Питер", "exits": ["Амстердам"]}]}})
        self.assertTrue(raw["cascade"]["enabled"])
        raw2 = st.apply_update(raw, {"servers": [
            {"orig_name": "Питер", "name": "Питер", "host": "203.0.113.21"},
            {"orig_name": "Амстердам", "name": "Амстердам", "host": "203.0.113.22"},
        ], "interval": 60})
        self.assertEqual(raw2["cascade"], raw["cascade"])

    def test_validate_links(self):
        servers = [{"name": "Питер", "host": "203.0.113.21"},
                   {"name": "Амстердам", "host": "203.0.113.22"}]
        with self.assertRaises(st.SettingsError):
            st.apply_update({}, {"servers": servers, "cascade": {"enabled": True, "links": []}})
        with self.assertRaises(st.SettingsError):
            st.apply_update({}, {"servers": servers, "cascade": {
                "enabled": True, "links": [{"entry": "Питер", "exits": ["Неизвестный"]}]}})
        with self.assertRaises(st.SettingsError):
            st.apply_update({}, {"servers": servers, "cascade": {
                "enabled": True, "links": [{"entry": "Питер", "exits": ["Питер"]}]}})
        raw = st.apply_update({}, {"servers": servers, "cascade": {
            "enabled": True, "links": [{"entry": "Питер", "exits": ["Амстердам"]}]}})
        self.assertEqual(raw["cascade"]["links"][0]["exits"], ["Амстердам"])

    def test_load_config_ignores_bad_links(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        try:
            Path(path).write_text(
                "servers:\n  - {name: a, host: 203.0.113.1, password: x}\n"
                "cascade:\n  enabled: true\n  links:\n    - {entry: missing, exits: [a]}\n",
                encoding="utf-8")
            cfg = am.load_config(path)
            self.assertTrue(cfg.cascade.enabled)
            self.assertEqual(cfg.cascade.links, [])
        finally:
            os.unlink(path)

    def test_public_view_has_no_secrets(self):
        raw = st.apply_update({}, {"servers": [
            {"name": "a", "host": "203.0.113.1", "password": "SUPERSECRET"},
            {"name": "b", "host": "203.0.113.2", "password": "SUPERSECRET2"},
        ], "cascade": {"enabled": True, "links": [{"entry": "a", "exits": ["b"]}]}})
        text = str(st.public_view(raw))
        self.assertNotIn("SUPERSECRET", text)


class HopsAndPoll(unittest.TestCase):
    def test_old_db_keeps_rows(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE polls (id INTEGER PRIMARY KEY, ts REAL, server TEXT, reachable INTEGER, "
                "ssh_ok INTEGER, tcp_ms REAL, ping_ms REAL, uptime_s REAL, load1 REAL, nproc INTEGER, "
                "cpu_pct REAL, mem_pct REAL, mem_used INTEGER, mem_total INTEGER, rx_bps REAL, "
                "tx_bps REAL, peers_total INTEGER, peers_online INTEGER, error TEXT);"
                "CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);"
                "INSERT INTO polls(ts, server, reachable, ssh_ok) VALUES (1, 'old', 1, 1);"
                "INSERT INTO state(key, value) VALUES ('keep', '\"yes\"');"
            )
            db.commit()
            db.close()
            store = am.Store(path)
            self.assertEqual(store.db.execute("SELECT server FROM polls").fetchone()[0], "old")
            self.assertEqual(store.get_state("keep"), "yes")
            cols = [r[1] for r in store.db.execute("PRAGMA table_info(hops)")]
            self.assertEqual(set(cols) >= {"ts", "entry", "exit", "ping_ms", "ok", "error"}, True)
            store.db.close()
        finally:
            os.unlink(path)

    def test_hop_command_whitelist(self):
        cmd = cascade.hop_ping_command("203.0.113.22")
        self.assertEqual(cmd, "ping -c 2 -W 2 203.0.113.22")
        self.assertTrue(am._ALLOWED_RE.match(cmd))
        self.assertFalse(am._ALLOWED_RE.match("ping -c 2 -W 2 203.0.113.22; rm -rf /"))
        self.assertFalse(am._ALLOWED_RE.match("ping -c 8 1.1.1.1"))
        with self.assertRaises(cascade.CascadeError):
            cascade.hop_ping_command("203.0.113.22; id")

    def test_parse_ping(self):
        self.assertEqual(cascade.parse_ping_ms("rtt min/avg/max/mdev = 10.1/28.4/40.0/1.2 ms\n"), 28.4)
        self.assertIsNone(cascade.parse_ping_ms(""))

    def test_collect_hops_with_fake_runner(self):
        cfg = am.Config(servers=demo_data.CASCADE_DEMO_SERVERS, interval=60,
                        cascade=demo_data.cascade_demo_cfg().cascade)
        runner = demo_data.FakeRunner(cfg.servers[0], 0, 1_700_000_000)
        hops = cascade.collect_hops(runner, [("Амстердам", "203.0.113.22")])
        self.assertEqual(hops[0]["exit"], "Амстердам")
        self.assertTrue(hops[0]["ok"])
        self.assertIsInstance(hops[0]["ping_ms"], float)

    def test_do_check_writes_hops_only_when_enabled(self):
        cfg = demo_data.cascade_demo_cfg()
        store = am.Store(":memory:")
        now = 1_700_000_000.0
        evs = am.do_check(cfg, store, am.Notifier(cfg, force_print=False),
                          runner_factory=lambda s: demo_data.FakeRunner(s, 0, now),
                          probe=lambda s: (20.0, 18.0), now=now)
        self.assertTrue(isinstance(evs, list))
        rows = list(store.db.execute("SELECT entry, exit, ok FROM hops"))
        self.assertEqual(sorted((r["entry"], r["exit"]) for r in rows),
                         [("Питер", "Амстердам"), ("Питер", "Вена")])
        self.assertTrue(all(r["ok"] for r in rows))

        cfg_off = am.Config(servers=demo_data.CASCADE_DEMO_SERVERS, interval=60,
                            cascade=cascade.empty())
        store2 = am.Store(":memory:")
        am.do_check(cfg_off, store2, am.Notifier(cfg_off, force_print=False),
                    runner_factory=lambda s: demo_data.FakeRunner(s, 0, now),
                    probe=lambda s: (20.0, 18.0), now=now)
        self.assertEqual(store2.db.execute("SELECT count(*) c FROM hops").fetchone()["c"], 0)


class ApiAndImport(unittest.TestCase):
    def test_snapshot_off_has_no_chain(self):
        path, cfg, now = demo_data.demo_store(now=1_700_000_000)
        store = am.Store(path)
        snap = api.api_snapshot(store, cfg, now)
        self.assertEqual(snap["cascade"], {"enabled": False, "links": []})
        self.assertFalse(any("role" in s for s in snap["servers"]))
        store.db.close()

    def test_cascade_demo_snapshot(self):
        path, cfg, now = demo_data.demo_store_cascade(now=1_700_000_000)
        store = am.Store(path)
        cas = api.api_cascade(store, cfg, now)
        self.assertTrue(cas["enabled"])
        self.assertEqual(cas["links"][0]["entry"]["name"], "Питер")
        exits = {x["name"]: x for x in cas["links"][0]["exits"]}
        self.assertIn("Амстердам", exits)
        self.assertIn("Вена", exits)
        self.assertIsNotNone(exits["Амстердам"]["hop_ms"])
        clients = {c["client"] for c in cas["links"][0]["entry"]["clients"]}
        self.assertIn("Иван — ноутбук", clients)
        self.assertIn("Мама", clients)
        via = {c["client"] for c in exits["Амстердам"]["clients"]}
        self.assertEqual(via, clients)
        self.assertTrue(all(c["via_entry"] == "Питер" for c in exits["Амстердам"]["clients"]))
        servers = api.api_servers(store, cfg, now)
        roles = {s["name"]: s.get("role") for s in servers}
        self.assertEqual(roles["Питер"], "entry")
        self.assertEqual(roles["Вена"], "exit")
        piter = next(s for s in servers if s["name"] == "Питер")
        ams = next(s for s in servers if s["name"] == "Амстердам")
        self.assertEqual((ams["peers_online"], ams["peers_total"]),
                         (piter["peers_online"], piter["peers_total"]))
        d_ams = api.api_server(store, cfg, "Амстердам", now)
        via = [u for u in d_ams["users"] if u.get("via_entry") == "Питер"]
        self.assertIn("Иван — ноутбук", {u["client"] for u in via})
        self.assertTrue(any(u.get("tunnel") for u in d_ams["users"]))
        html = __import__("dashboard").render_html(api.api_snapshot(store, cfg, now), demo=True)
        self.assertIn("Цепочка каскада", html)
        self.assertNotIn("CHANGE_ME", html)
        store.db.close()

    def test_import_matches_by_host(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE servers (id TEXT, name TEXT, host TEXT, role TEXT, ssh_secret_enc TEXT);"
                "CREATE TABLE chains (id TEXT, name TEXT, entry_id TEXT, exit_id TEXT);"
                "INSERT INTO servers VALUES ('e1','Entry RU','203.0.113.21','entry','SECRET_SHOULD_NOT_LEAK');"
                "INSERT INTO servers VALUES ('x1','Exit AMS','203.0.113.22','exit','SECRET2');"
                "INSERT INTO chains VALUES ('c1','main','e1','x1');"
            )
            db.commit()
            db.close()
            am_servers = [{"name": "Питер", "host": "203.0.113.21"},
                          {"name": "Амстердам", "host": "203.0.113.22"}]
            out = cascade.import_topology(path, am_servers)
            self.assertTrue(out["found"])
            self.assertEqual(out["links"], [{"entry": "Питер", "exits": ["Амстердам"]}])
            blob = str(out)
            self.assertNotIn("SECRET", blob)
        finally:
            os.unlink(path)

    def test_import_unmatched(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE servers (id TEXT, name TEXT, host TEXT, role TEXT);"
                "INSERT INTO servers VALUES ('e1','Piter','203.0.113.21','entry');"
                "INSERT INTO servers VALUES ('x1','AMS','203.0.113.22','exit');"
            )
            db.commit()
            db.close()
            out = cascade.import_topology(path, [{"name": "Питер", "host": "203.0.113.21"}])
            self.assertFalse(out["found"])
            self.assertTrue(any(u["name"] == "AMS" for u in out["unmatched"]))
        finally:
            os.unlink(path)

    def test_tunnel_peer_detection(self):
        peers = [{"client": "каскад: Питер", "endpoint": "203.0.113.21:51820", "online": True, "rx": 1, "tx": 2, "pubkey": "ab"}]
        self.assertTrue(cascade.is_tunnel_peer(peers[0], "203.0.113.21"))
        self.assertFalse(cascade.is_tunnel_peer(peers[0], "203.0.113.99"))


if __name__ == "__main__":
    unittest.main()
