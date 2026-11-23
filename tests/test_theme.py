# -*- coding: utf-8 -*-
"""Оформление v1.4.0: страна сервера -> флаг/героиня/цвета, ассеты, GeoIP, настройки appearance."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import amnezia_monitor as am  # noqa: E402
import app_main  # noqa: E402
import app_settings as st  # noqa: E402
import dashboard  # noqa: E402
import dashboard_api as api  # noqa: E402
import secrets_store  # noqa: E402
import theme  # noqa: E402


class Detection(unittest.TestCase):
    def test_by_name(self):
        cases = {"Svecia": "se", "Вена-2": "at", "Kazahi": "kz", "Питер": "ru", "Frankfurt-1": "de",
                 "Германия-1": "de", "Amsterdam": "nl", "London": "gb", "UK-2": "gb", "Варшава": "pl",
                 "Paris": "fr", "Helsinki": "fi", "Нью-Дели": "in", "Тель-Авив": "il", "Гонконг": "hk",
                 "Charlotte": "us", "USA-1": "us",
                 # короткие слова — только целиком
                 "Ukraine": None, "Неделя-тест": None, "Swedbank": None, "test1": None, "": None}
        for name, cc in cases.items():
            self.assertEqual(theme.by_name(name), cc, name)

    def test_priority_manual_over_name(self):
        self.assertEqual(theme.detect_country("Frankfurt", manual="fi"), ("fi", "manual"))
        self.assertEqual(theme.detect_country("Frankfurt"), ("de", "name"))
        self.assertEqual(theme.detect_country("test1", "203.0.113.11"), ("", "none"))

    def test_theme_fallbacks(self):
        t = theme.theme_for("test1")
        self.assertEqual(t["key"], "default")
        self.assertIsNone(t["flag"])
        t = theme.theme_for("x", manual="es")             # флаг есть, своей героини нет -> общая
        self.assertEqual((t["key"], t["flag"], t["country_name"]), ("default", "/theme/flags/es.svg", "Испания"))
        t = theme.theme_for("x", manual="se")
        self.assertEqual(t["heroine"], "/theme/heroines/heroine_se.webp")
        t = theme.theme_for("x", manual="zz")             # неизвестный код: без флага, без падения
        self.assertIsNone(t["flag"])

    def test_assets_complete(self):
        for key in theme.THEMES:
            self.assertTrue((theme.ASSETS_DIR / "heroines" / f"heroine_{key}.webp").is_file(), key)
            bg = theme.ASSETS_DIR / "backgrounds" / f"bg_{key}.webp"
            self.assertTrue(bg.is_file(), key)
            self.assertLess(bg.stat().st_size, 260_000, key)
        for cc, _ in theme.known_countries():
            self.assertTrue((theme.ASSETS_DIR / "flags" / f"{cc}.svg").is_file(), cc)

    def test_asset_whitelist(self):
        self.assertEqual(theme.asset("/theme/flags/se.svg")[1], "image/svg+xml")
        self.assertEqual(theme.asset("/theme/heroines/heroine_se.webp")[1], "image/webp")
        self.assertEqual(theme.asset("/theme/backgrounds/bg_se.webp")[1], "image/webp")
        self.assertEqual(theme.theme_for("Svecia")["background"], "/theme/backgrounds/bg_se.webp")
        self.assertEqual(theme.theme_for("x", manual="es")["background"], "/theme/backgrounds/bg_default.webp")
        for bad in ("/theme/flags/../dashboard.svg", "/theme/flags/se.webp", "/theme/heroines/heroine_se.svg", "/theme/backgrounds/bg_se.svg",
                    "/theme/other/x.svg", "/theme/flags/SE.svg", "/theme/flags/nope.svg", "/theme/flags/%2e%2e.svg"):
            self.assertIsNone(theme.asset(bad), bad)

    def test_normalize_country(self):
        self.assertEqual(theme.normalize_country(" DE "), "de")
        self.assertEqual(theme.normalize_country("авто"), "")
        with self.assertRaises(ValueError):
            theme.normalize_country("Germany")


class GeoIP(unittest.TestCase):
    def setUp(self):
        self.cache = os.path.join(tempfile.mkdtemp(), "geo.json")

    def test_disabled_by_default_and_private_ip_never_sent(self):
        calls = []
        with mock.patch.object(theme, "_geo_fetch", side_effect=lambda *a: calls.append(a)):
            self.assertEqual(theme.detect_country("test1", "8.8.8.8", cache_path=self.cache), ("", "none"))
            self.assertIsNone(theme.by_geoip("192.168.1.5", self.cache))
            self.assertIsNone(theme.by_geoip("example.com", self.cache))
        self.assertEqual(calls, [])

    def test_lookup_cached(self):
        calls = []

        def fetch(ip):
            calls.append(ip)
            return "fi"
        self.assertEqual(theme.by_geoip("8.8.4.4", self.cache, background=False, fetch=fetch), "fi")
        self.assertEqual(theme.by_geoip("8.8.4.4", self.cache, background=False, fetch=fetch), "fi")
        self.assertEqual(calls, ["8.8.4.4"])
        self.assertEqual(theme.detect_country("srv", "8.8.4.4", geoip=True, cache_path=self.cache), ("fi", "geoip"))

    def test_network_error_is_quiet(self):
        def fetch(ip):
            raise OSError("offline")
        self.assertIsNone(theme.by_geoip("8.8.4.4", self.cache, background=False, fetch=fetch))


class Config(unittest.TestCase):
    def test_country_and_appearance_roundtrip(self):
        with mock.patch.object(secrets_store, "IS_WINDOWS", False):
            raw = st.apply_update({}, {"servers": [{"name": "a", "host": "203.0.113.1", "country": "SE"},
                                                    {"name": "b", "host": "203.0.113.2"}]})
            self.assertEqual(raw["servers"][0]["country"], "se")
            self.assertNotIn("country", raw["servers"][1])
            # старый клиент без поля country — значение сохраняется
            raw2 = st.apply_update(raw, {"servers": [{"name": "a", "host": "203.0.113.1", "orig_name": "a"}]})
            self.assertEqual(raw2["servers"][0]["country"], "se")
            # «Авто» — сбрасывает
            raw3 = st.apply_update(raw, {"servers": [{"name": "a", "host": "203.0.113.1", "country": ""}]})
            self.assertNotIn("country", raw3["servers"][0])
            with self.assertRaises(st.SettingsError):
                st.apply_update({}, {"servers": [{"name": "a", "host": "203.0.113.1", "country": "Sweden"}]})
            raw["appearance"] = {"animations": False, "geoip": True}
            p = Path(tempfile.mkdtemp()) / "servers.yaml"
            st.write_raw(p, raw)
            cfg = am.load_config(str(p))
            self.assertEqual(cfg.servers[0].country, "se")
            self.assertEqual((cfg.animations, cfg.geoip), (False, True))
            view = st.public_view(raw)
            self.assertEqual(view["servers"][0]["country"], "se")
            self.assertEqual(view["appearance"], {"animations": False, "geoip": True})
            self.assertIn({"code": "se", "name": "Швеция"}, view["countries"])

    def test_defaults(self):
        p = Path(tempfile.mkdtemp()) / "servers.yaml"
        st.write_raw(p, {"servers": [{"name": "a", "host": "203.0.113.1", "password": "x"}]})
        cfg = am.load_config(str(p))
        self.assertEqual((cfg.animations, cfg.geoip, cfg.servers[0].country), (True, False, ""))


class HTTP(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(secrets_store, "IS_WINDOWS", False)
        p.start()
        self.addCleanup(p.stop)
        self.dir = Path(tempfile.mkdtemp())
        self.cfgp = self.dir / "servers.yaml"
        st.write_raw(self.cfgp, st.apply_update({}, {"servers": [
            {"name": "Svecia", "host": "203.0.113.1", "password": "pw-a"},
            {"name": "b", "host": "203.0.113.2", "password": "pw-b", "country": "kz"}]}))
        self.holder = app_main.ConfigHolder(self.cfgp, self.dir / "m.db")
        self.polls = []
        self.ctx = st.SettingsContext(self.cfgp, "TOKEN123", str(self.dir), on_saved=lambda: self.polls.append(1),
                                      on_appearance=lambda: self.holder.reload(force=True))
        self.httpd = dashboard.make_server(lambda: am.Store(str(self.dir / "m.db")), self.holder.get,
                                           "127.0.0.1", 0, settings=self.ctx)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close()))

    def req(self, method, path, body=None, token="TOKEN123"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"X-Amnezia-Token": token} if token else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, raw, hdrs

    def test_theme_assets_route(self):
        code, body, h = self.req("GET", "/theme/heroines/heroine_se.webp", token=None)
        self.assertEqual((code, h["Content-Type"]), (200, "image/webp"))
        self.assertTrue(body.startswith(b"RIFF"))
        self.assertIn("max-age", h["Cache-Control"])
        code, body, h = self.req("GET", "/theme/flags/se.svg", token=None)
        self.assertEqual((code, h["Content-Type"]), (200, "image/svg+xml"))
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        for bad in ("/theme/flags/../../servers.yaml", "/theme/flags/xx.svg", "/theme/x"):
            self.assertEqual(self.req("GET", bad, token=None)[0], 404, bad)

    def test_page_csp_and_meta(self):
        code, body, h = self.req("GET", "/", token=None)
        self.assertIn("img-src 'self' data:", h["Content-Security-Policy"])
        self.assertIn('"animations": true', body.decode())

    def test_servers_have_theme(self):
        srv = {s["name"]: s for s in json.loads(self.req("GET", "/api/servers")[1])}
        self.assertEqual((srv["Svecia"]["theme"]["country"], srv["Svecia"]["theme"]["source"]), ("se", "name"))
        self.assertEqual((srv["b"]["theme"]["country"], srv["b"]["theme"]["source"]), ("kz", "manual"))
        self.assertEqual(srv["b"]["theme"]["flag"], "/theme/flags/kz.svg")

    def test_appearance_endpoint(self):
        self.assertEqual(self.req("POST", "/api/appearance", {"animations": False}, token="bad")[0], 403)
        self.assertEqual(self.req("POST", "/api/appearance", {"animations": "no"})[0], 400)
        code, body, _ = self.req("POST", "/api/appearance", {"animations": False})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["appearance"], {"animations": False, "geoip": False})
        self.assertEqual(self.polls, [])                       # без внеочередного опроса серверов
        raw = st.read_raw(self.cfgp)
        self.assertEqual(raw["appearance"]["animations"], False)
        self.assertEqual(len(raw["servers"]), 2)               # остальное не тронуто
        self.assertIn('"animations": false', self.req("GET", "/", token=None)[1].decode())
        self.assertFalse(self.holder.get().animations)


class Api(unittest.TestCase):
    def test_server_theme_without_cfg(self):
        self.assertEqual(api.server_theme(None, "Amsterdam")["country"], "nl")


if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_android_theme_data_in_sync():
    """android/.../ThemeData.kt и ThemeRes.kt сгенерированы из theme.py и не устарели."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "gen_android_theme.py"
    r = subprocess.run([sys.executable, str(script), "--check"], cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
