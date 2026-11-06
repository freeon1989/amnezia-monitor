# -*- coding: utf-8 -*-
"""Тесты режима приложения: настройки, шифрование, API настроек (токен/Origin/Host), один экземпляр."""
import base64
import http.client
import json
import logging
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.getLogger("paramiko").setLevel(logging.CRITICAL + 1)
import amnezia_monitor as am  # noqa: E402
import app_main  # noqa: E402
import app_settings as st  # noqa: E402
import app_window  # noqa: E402
import dashboard  # noqa: E402
import secrets_store  # noqa: E402

SECRET = "S3cret-Пароль-1"


def fake_dpapi(b: bytes) -> bytes:          # обратимое «шифрование» для тестов вне Windows
    return b"FAKEDPAPI" + bytes(x ^ 0x5A for x in b)


def fake_undpapi(b: bytes) -> bytes:
    assert b.startswith(b"FAKEDPAPI")
    return bytes(x ^ 0x5A for x in b[9:])


def windows_mocks():
    return [mock.patch.object(secrets_store, "IS_WINDOWS", True),
            mock.patch.object(secrets_store, "_dpapi_protect", fake_dpapi),
            mock.patch.object(secrets_store, "_dpapi_unprotect", fake_undpapi)]


class Secrets(unittest.TestCase):
    def test_plain_roundtrip(self):
        with mock.patch.object(secrets_store, "IS_WINDOWS", False):
            v = secrets_store.protect(SECRET)
            self.assertEqual(v, "plain:" + SECRET)
            self.assertEqual(secrets_store.unprotect(v), SECRET)

    def test_dpapi_path_mocked(self):
        ms = windows_mocks()
        for m in ms:
            m.start()
        try:
            v = secrets_store.protect(SECRET)
            self.assertTrue(v.startswith("dpapi:"))
            self.assertNotIn(SECRET, v)
            self.assertNotIn(SECRET.encode(), base64.b64decode(v[6:]))
            self.assertEqual(secrets_store.unprotect(v), SECRET)
            # файл настроек: только password_enc, без открытого пароля; load_config расшифровывает
            d = Path(tempfile.mkdtemp())
            raw = st.apply_update({}, {"interval": 60, "servers": [{"name": "srv", "host": "203.0.113.5",
                                                                    "password": SECRET}],
                                       "telegram": {"chat_id": "123", "token": "123456:" + "A" * 30}})
            st.write_raw(d / "servers.yaml", raw)
            text = (d / "servers.yaml").read_text(encoding="utf-8")
            self.assertIn("password_enc: dpapi:", text)
            self.assertNotIn(SECRET, text)
            self.assertNotIn("A" * 30, text)
            cfg = am.load_config(str(d / "servers.yaml"))
            self.assertEqual(cfg.servers[0].password, SECRET)
            self.assertEqual(cfg.telegram_token, "123456:" + "A" * 30)
            self.assertEqual(cfg.telegram_chat_id, "123")
        finally:
            for m in ms:
                m.stop()

    def test_dpapi_value_on_linux_gives_clear_error(self):
        d = Path(tempfile.mkdtemp())
        (d / "s.yaml").write_text("servers:\n  - {name: a, host: 203.0.113.1, password_enc: 'dpapi:AAAA'}\n")
        with mock.patch.object(secrets_store, "IS_WINDOWS", False):
            with self.assertRaises(am.ConfigError):
                am.load_config(str(d / "s.yaml"))


@unittest.skipUnless(sys.platform.startswith("win"), "настоящий DPAPI есть только в Windows")
class RealDPAPI(unittest.TestCase):
    def test_roundtrip(self):
        v = secrets_store.protect(SECRET)
        self.assertTrue(v.startswith("dpapi:"))
        self.assertNotIn(SECRET.encode(), base64.b64decode(v[6:]))
        self.assertEqual(secrets_store.unprotect(v), SECRET)


class ApplyUpdate(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(secrets_store, "IS_WINDOWS", False)
        p.start()
        self.addCleanup(p.stop)
        self.raw = st.apply_update({}, {"interval": 60, "servers": [
            {"name": "a", "host": "203.0.113.1", "password": "pw-a"},
            {"name": "b", "host": "203.0.113.2", "password": "pw-b", "notes": "домен b.example.com"}]})

    def test_blank_keeps_password_and_rename(self):
        new = st.apply_update(self.raw, {"servers": [
            {"orig_name": "a", "name": "a-renamed", "host": "203.0.113.1", "password": ""},
            {"orig_name": "b", "name": "b", "host": "203.0.113.2", "password": "new-b"}]})
        s = {x["name"]: x for x in new["servers"]}
        self.assertEqual(s["a-renamed"]["password"], "pw-a")
        self.assertEqual(s["b"]["password"], "new-b")
        self.assertEqual(new["interval"], 60)

    def test_clear_and_delete(self):
        new = st.apply_update(self.raw, {"servers": [{"orig_name": "a", "name": "a", "host": "203.0.113.1",
                                                      "clear_password": True}]})
        self.assertEqual(len(new["servers"]), 1)
        self.assertNotIn("password", new["servers"][0])

    def test_public_view_has_no_secrets(self):
        v = st.public_view(dict(self.raw, telegram={"token": "123456:" + "B" * 30, "chat_id": "1"}))
        dump = json.dumps(v)
        for secret in ("pw-a", "pw-b", "B" * 30):
            self.assertNotIn(secret, dump)
        self.assertTrue(v["servers"][0]["has_password"])
        self.assertTrue(v["telegram"]["has_token"])
        self.assertEqual(v["servers"][1]["notes"], "домен b.example.com")

    def test_validation(self):
        bad = [{"name": "", "host": "203.0.113.1"}, {"name": "x", "host": "bad host; rm"},
               {"name": "x", "host": "203.0.113.1", "port": 70000}, {"name": "x", "host": "h", "user": "a b"},
               {"name": "settings", "host": "h"}]
        for b in bad:
            with self.assertRaises(st.SettingsError, msg=b):
                st.apply_update(self.raw, {"servers": [b]})
        with self.assertRaises(st.SettingsError):
            st.apply_update(self.raw, {"servers": [{"name": "a", "host": "h"}, {"name": "a", "host": "h2"}]})
        with self.assertRaises(st.SettingsError):
            st.apply_update(self.raw, {"servers": [], "interval": 1})
        with self.assertRaises(st.SettingsError):
            st.apply_update(self.raw, {"servers": [], "telegram": {"token": "garbage"}})


class FakeRunner:
    seen = []

    def __init__(self, srv):
        FakeRunner.seen.append(srv)
        if srv.password != "pw-a":
            raise type("AuthenticationException", (Exception,), {})()

    def run(self, cmd):
        assert am._ALLOWED_RE.match(cmd)
        return 0, "amnezia-awg|Up 2 days|amneziavpn/amnezia-wg\namnezia-dns|Up 2 days|amnezia-dns\n", ""

    def close(self):
        pass


class SettingsHTTP(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(secrets_store, "IS_WINDOWS", False)
        p.start()
        self.addCleanup(p.stop)
        self.dir = Path(tempfile.mkdtemp())
        self.cfgp = self.dir / "servers.yaml"
        st.write_raw(self.cfgp, st.apply_update({}, {"servers": [{"name": "a", "host": "203.0.113.1",
                                                                  "password": "pw-a"}]}))
        self.holder = app_main.ConfigHolder(self.cfgp, self.dir / "m.db")
        self.saved = threading.Event()
        self.ctx = st.SettingsContext(self.cfgp, "TOKEN123", str(self.dir),
                                      on_saved=lambda: (self.holder.reload(force=True), self.saved.set()),
                                      runner_factory=FakeRunner, tcp=lambda h, p, t: 12.5)
        self.httpd = dashboard.make_server(lambda: am.Store(str(self.dir / "m.db")), self.holder.get,
                                           "127.0.0.1", 0, settings=self.ctx)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close()))

    def req(self, method, path, body=None, token="TOKEN123", host=None, origin=None, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {}
        if token:
            h["X-Amnezia-Token"] = token
        if host:
            h["Host"] = host
        if origin:
            h["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = ctype
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read().decode()
        c.close()
        return r.status, raw

    def test_page_embeds_token_only_for_local_host(self):
        code, page = self.req("GET", "/", token=None)
        self.assertEqual(code, 200)
        self.assertIn('"token": "TOKEN123"', page)
        code, page = self.req("GET", "/", token=None, host="evil.example:%d" % self.port)
        self.assertNotIn("TOKEN123", page)                 # DNS-rebinding: токен не выдаётся

    def test_token_origin_host_checks(self):
        self.assertEqual(self.req("GET", "/api/settings", token=None)[0], 403)
        self.assertEqual(self.req("GET", "/api/settings", token="wrong")[0], 403)
        self.assertEqual(self.req("GET", "/api/settings", host="evil.example")[0], 403)
        body = {"servers": []}
        self.assertEqual(self.req("POST", "/api/settings", body, token=None)[0], 403)
        self.assertEqual(self.req("POST", "/api/settings", body, origin="http://evil.example")[0], 403)
        self.assertEqual(self.req("POST", "/api/settings", body, ctype="text/plain")[0], 400)
        self.assertFalse(self.saved.is_set())
        self.assertEqual(len(am.load_config(str(self.cfgp)).servers), 1)     # ничего не изменилось

    def test_get_and_save_never_leak_password(self):
        code, raw = self.req("GET", "/api/settings", origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code, 200)
        self.assertNotIn("pw-a", raw)
        self.assertTrue(json.loads(raw)["servers"][0]["has_password"])
        body = {"interval": 30, "servers": [
            {"orig_name": "a", "name": "a", "host": "203.0.113.1", "password": ""},
            {"name": "new", "host": "203.0.113.9", "port": 2222, "user": "admin", "password": "pw-new",
             "notes": "vpn.example.com"}],
            "telegram": {"chat_id": "42", "token": "123456:" + "C" * 30}}
        code, raw = self.req("POST", "/api/settings", body)
        self.assertEqual(code, 200, raw)
        for secret in ("pw-a", "pw-new", "C" * 30):
            self.assertNotIn(secret, raw)
        self.assertTrue(self.saved.wait(2))
        cfg = self.holder.get()                                        # новые настройки подхвачены
        self.assertEqual([s.name for s in cfg.servers], ["a", "new"])
        self.assertEqual(cfg.servers[0].password, "pw-a")             # пустое поле = прежний пароль
        self.assertEqual(cfg.servers[1].password, "pw-new")
        self.assertEqual(cfg.servers[1].notes, "vpn.example.com")
        self.assertEqual(cfg.interval, 30)
        self.assertEqual(cfg.db_path, str(self.dir / "m.db"))
        if not sys.platform.startswith("win"):
            self.assertEqual(oct(self.cfgp.stat().st_mode & 0o777), "0o600")
        code, raw = self.req("POST", "/api/settings", {"servers": [{"name": "x", "host": "bad host"}]})
        self.assertEqual(code, 400)
        self.assertIn("error", json.loads(raw))

    def test_connection_check_uses_saved_password(self):
        FakeRunner.seen.clear()
        code, raw = self.req("POST", "/api/settings/test",
                             {"server": {"orig_name": "a", "name": "a", "host": "203.0.113.1", "password": ""}})
        res = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"], res)
        self.assertEqual(FakeRunner.seen[-1].password, "pw-a")
        self.assertIn("amnezia-awg", res["steps"][-1]["detail"])
        self.assertNotIn("pw-a", raw)
        code, raw = self.req("POST", "/api/settings/test",
                             {"server": {"name": "a", "host": "203.0.113.1", "password": "wrong"}})
        res = json.loads(raw)
        self.assertFalse(res["ok"])
        self.assertIn("SSH отклонил вход", res["steps"][1]["detail"])
        self.assertNotIn("wrong", raw)

    def test_quit_not_available_without_callback(self):
        self.assertEqual(self.req("POST", "/api/app/quit", {})[0], 404)


class AppMode(unittest.TestCase):
    def test_single_instance_lock(self):
        d = Path(tempfile.mkdtemp())
        a, b = app_main.InstanceLock(d), app_main.InstanceLock(d)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_app_demo_headless_starts_and_stops(self):
        d = tempfile.mkdtemp()
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            rc = am.main(["--demo", "app", "--no-browser", "--no-tray", "--port", "0", "--data-dir", d,
                          "--exit-after", "1"])
        finally:
            for h in root.handlers[:]:
                if h not in before:
                    root.removeHandler(h)
                    h.close()
        self.assertEqual(rc, 0)
        self.assertTrue((Path(d) / "servers.yaml").exists())
        self.assertTrue((Path(d) / "app.port").exists())

    def test_first_run_creates_empty_config(self):
        d = Path(tempfile.mkdtemp())
        app_main.ensure_config(d / "servers.yaml")
        cfg = am.load_config(str(d / "servers.yaml"), allow_empty=True)
        self.assertEqual(cfg.servers, [])
        with self.assertRaises(am.ConfigError):
            am.load_config(str(d / "servers.yaml"))          # CLI по-прежнему требует серверы

    def test_windows_window_policy_and_second_launch(self):
        self.assertTrue(app_window.wants_desktop_window(False, "win32"))
        self.assertFalse(app_window.wants_desktop_window(True, "win32"))
        self.assertFalse(app_window.wants_desktop_window(False, "linux"))

        d = Path(tempfile.mkdtemp())
        lock = app_main.InstanceLock(d)
        self.assertFalse(lock.consume_show_request())
        lock.request_show()
        self.assertTrue(lock.consume_show_request())
        self.assertFalse(lock.consume_show_request())

        class Live:
            def running_url(self):
                return "http://127.0.0.1:9/"

            def __init__(self):
                self.asked = False

            def request_show(self):
                self.asked = True

        live, opened = Live(), []
        rc = app_main.activate_existing(live, no_browser=False, window_mode=True, open_external=opened.append)
        self.assertEqual(rc, 0)
        self.assertTrue(live.asked)
        self.assertEqual(opened, [])
        rc = app_main.activate_existing(live, no_browser=False, window_mode=False, open_external=opened.append)
        self.assertEqual(opened, ["http://127.0.0.1:9/"])
        opened.clear()
        rc = app_main.activate_existing(live, no_browser=True, window_mode=False, open_external=opened.append)
        self.assertEqual(rc, 0)
        self.assertEqual(opened, [])

    def test_close_hides_to_tray_and_quit_allows_close(self):
        calls = []

        class FakeWindow:
            def hide(self):
                calls.append("hide")

            def show(self):
                calls.append("show")

            def restore(self):
                calls.append("restore")

        ui = app_window.DesktopWindow(Path(tempfile.mkdtemp()), presenter=lambda text: calls.append(text))
        ui.window = FakeWindow()
        ui.tray_running = True
        self.assertIs(ui.on_closing(), False)
        time.sleep(0.2)
        self.assertIn("hide", calls)
        ui.show()
        self.assertEqual(calls[-2:], ["show", "restore"])
        ui.quitting = True
        self.assertIsNone(ui.on_closing())

    def test_missing_webview2_does_not_open_a_browser(self):
        notes = []
        ui = app_window.DesktopWindow(Path(tempfile.mkdtemp()), presenter=notes.append)
        with mock.patch.object(app_window, "webview2_runtime_installed", return_value=False):
            self.assertFalse(ui.run("http://127.0.0.1:9/"))
        self.assertEqual(ui.failure, "webview2-missing")
        self.assertIn("WebView2", notes[0])
        self.assertIn("не открывается", notes[0])

    def test_tray_icon_image(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow не установлен")
        img = app_main.make_icon_image()
        self.assertEqual(img.size, (64, 64))


if __name__ == "__main__":
    unittest.main(verbosity=2)
