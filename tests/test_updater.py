# -*- coding: utf-8 -*-
"""Обновление с GitHub: поддельный локальный сервер релизов, подмена хеша, защита хостов, подмена файлов с откатом."""
import hashlib
import http.client
import http.server
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import amnezia_monitor as am  # noqa: E402
import app_main  # noqa: E402
import app_settings as st  # noqa: E402
import dashboard  # noqa: E402
import secrets_store  # noqa: E402
import updater  # noqa: E402

REPO = "freeon1989/amnezia-monitor"
EXE_BYTES = b"MZ" + b"\x90" * 4000


def make_zip(exe=EXE_BYTES, name="AmneziaMonitor-win-x64-v9.9.9/AmneziaMonitor.exe", extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, exe)
        z.writestr("AmneziaMonitor-win-x64-v9.9.9/START.txt", "hi")
        for n, d in (extra or {}).items():
            z.writestr(n, d)
    return buf.getvalue()


class FakeGitHub:
    """Локальный «GitHub»: api + файлы релиза. Считает запросы к файлам, чтобы доказать «без нажатия — не качаем»."""

    def __init__(self, version="1.9.0", zip_bytes=None, sha_override=None, notes="Что нового: тест", assets=True,
                 redirect_to=None, prerelease=False):
        self.version, self.notes, self.assets, self.redirect_to = version, notes, assets, redirect_to
        self.zip_bytes = zip_bytes if zip_bytes is not None else make_zip()
        self.sha = sha_override or hashlib.sha256(self.zip_bytes).hexdigest()
        self.prerelease = prerelease
        self.asset_hits = 0
        self.api_hits = 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/octet-stream", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                base = "http://127.0.0.1:%d" % outer.port
                if self.path == "/repos/%s/releases/latest" % REPO:
                    outer.api_hits += 1
                    tag = "v" + outer.version
                    assets = []
                    if outer.assets:
                        for n in (updater.WIN_ASSET, updater.WIN_SHA_ASSET):
                            assets.append({"name": n, "size": len(outer.zip_bytes),
                                           "browser_download_url": "%s/dl/%s/%s" % (base, tag, n)})
                    body = json.dumps({"tag_name": tag, "draft": False, "prerelease": outer.prerelease,
                                       "body": outer.notes, "html_url": "%s/%s/releases/tag/%s" % (base, REPO, tag),
                                       "assets": assets}).encode()
                    return self._send(200, body, "application/json")
                if self.path.startswith("/dl/"):
                    outer.asset_hits += 1
                    if outer.redirect_to:
                        return self._send(302, b"", headers={"Location": outer.redirect_to})
                    if self.path.endswith(".sha256"):
                        return self._send(200, ("%s  %s\n" % (outer.sha, updater.WIN_ASSET)).encode())
                    return self._send(200, outer.zip_bytes)
                self._send(404, b"nope")

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def source(self, **kw):
        return updater.Source(api_base="http://127.0.0.1:%d" % self.port, allowed_hosts=frozenset({"127.0.0.1"}),
                              allowed_schemes=("http",), min_exe=100, timeout=5, **kw)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Versions(unittest.TestCase):
    def test_semver_compare(self):
        self.assertTrue(updater.is_newer("v1.10.0", "1.9.9"))
        self.assertTrue(updater.is_newer("1.6.0", "1.4.2"))
        self.assertFalse(updater.is_newer("1.6.0", "1.6.0"))
        self.assertFalse(updater.is_newer("1.4.9", "1.6.0"))
        for bad in ("", "latest", "1.5", "1.6.0-beta", "v1.2.3.4", "1.2.x"):
            with self.assertRaises(updater.UpdateError):
                updater.parse_version(bad)

    def test_app_version_consistent(self):
        from version import __version__
        self.assertEqual(__version__, "1.6.0")
        import blockcheck
        self.assertEqual(blockcheck.APP_VERSION, __version__)

    def test_sha_file_parsing(self):
        h = "ab" * 32
        self.assertEqual(updater.parse_sha256_file("%s  AmneziaMonitor-win-x64.zip\n" % h.upper(), updater.WIN_ASSET), h)
        self.assertEqual(updater.parse_sha256_file(h + "\n", updater.WIN_ASSET), h)
        with self.assertRaises(updater.UpdateError):
            updater.parse_sha256_file("%s  other.zip\n%s  x.zip\n" % (h, h), updater.WIN_ASSET)
        with self.assertRaises(updater.UpdateError):
            updater.parse_sha256_file("не хеш", updater.WIN_ASSET)


class FetchAndDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_check_finds_newer_and_changelog_without_downloading(self):
        gh = FakeGitHub(version="1.9.0", notes="• Исправлено А\n• Добавлено Б")
        self.addCleanup(gh.close)
        m = updater.UpdateManager(self.tmp, src=gh.source(), current="1.6.0", installable=True)
        s = m.check(manual=True)
        self.assertTrue(s["available"])
        self.assertEqual(s["release"]["version"], "1.9.0")
        self.assertIn("Исправлено А", s["release"]["notes"])
        self.assertEqual(gh.asset_hits, 0, "проверка версии не должна скачивать файлы")

    def test_same_or_older_version_is_not_offered(self):
        gh = FakeGitHub(version="1.6.0")
        self.addCleanup(gh.close)
        m = updater.UpdateManager(self.tmp, src=gh.source(), current="1.6.0", installable=True)
        self.assertFalse(m.check(manual=True)["available"])
        with self.assertRaises(updater.UpdateError):
            m.start_download()

    def test_prerelease_ignored(self):
        gh = FakeGitHub(version="2.0.0", prerelease=True)
        self.addCleanup(gh.close)
        m = updater.UpdateManager(self.tmp, src=gh.source(), current="1.6.0", installable=True)
        s = m.check(manual=True)
        self.assertFalse(s["available"])
        self.assertTrue(s["error"])

    def test_download_verifies_hash_and_stages_exe(self):
        gh = FakeGitHub()
        self.addCleanup(gh.close)
        rel = updater.fetch_latest(gh.source())
        seen = []
        exe = updater.download_and_stage(rel, self.tmp / "stage", gh.source(), lambda d, t: seen.append((d, t)))
        self.assertEqual(exe.read_bytes(), EXE_BYTES)
        self.assertTrue(seen and seen[-1][0] == len(gh.zip_bytes))
        self.assertFalse((self.tmp / "stage" / "download.part").exists())

    def test_tampered_hash_is_rejected_and_nothing_staged(self):
        gh = FakeGitHub(sha_override="0" * 64)
        self.addCleanup(gh.close)
        rel = updater.fetch_latest(gh.source())
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download_and_stage(rel, self.tmp / "stage", gh.source())
        self.assertIn("SHA-256", str(cm.exception))
        self.assertFalse((self.tmp / "stage").exists())

    def test_tampered_archive_is_rejected(self):
        gh = FakeGitHub()
        self.addCleanup(gh.close)
        gh.zip_bytes = make_zip(exe=b"MZ" + b"EVIL" * 1000)        # хеш в .sha256 остался от настоящего архива
        rel = updater.fetch_latest(gh.source())
        with self.assertRaises(updater.UpdateError):
            updater.download_and_stage(rel, self.tmp / "stage", gh.source())

    def test_manager_tampered_flow_ends_in_error_state(self):
        gh = FakeGitHub(sha_override="f" * 64)
        self.addCleanup(gh.close)
        m = updater.UpdateManager(self.tmp, src=gh.source(), current="1.6.0", installable=True)
        m.check(manual=True)
        m.start_download()
        for _ in range(100):
            if m.status()["phase"] != "downloading":
                break
            time.sleep(0.05)
        s = m.status()
        self.assertEqual(s["phase"], "error")
        self.assertIn("SHA-256", s["error"])
        with self.assertRaises(updater.UpdateError):
            m.restart_now()

    def test_redirect_to_non_github_host_is_blocked(self):
        gh = FakeGitHub(redirect_to="http://evil.example.com/AmneziaMonitor-win-x64.zip")
        self.addCleanup(gh.close)
        rel = updater.fetch_latest(gh.source())
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download_and_stage(rel, self.tmp / "stage", gh.source())
        self.assertIn("не с GitHub", str(cm.exception))

    def test_release_page_check(self):
        src = updater.Source()
        self.assertTrue(updater.release_page_ok("https://github.com/freeon1989/amnezia-monitor/releases/tag/v1.6.0", src))
        for bad in ("https://github.com/other/repo/releases/tag/v1", "http://github.com/freeon1989/amnezia-monitor/releases",
                    "https://evil.com/freeon1989/amnezia-monitor/releases", "javascript:alert(1)", ""):
            self.assertFalse(updater.release_page_ok(bad, src), bad)

    def test_default_source_requires_https_and_github_hosts(self):
        src = updater.Source()
        for url in ("http://github.com/x", "https://evil.example.com/x", "https://github.com.evil.com/x",
                    "ftp://github.com/x", "https://127.0.0.1/x"):
            with self.assertRaises(updater.UpdateError, msg=url):
                updater._check_url(url, src)
        for url in ("https://github.com/freeon1989/amnezia-monitor/releases/download/v1/a.zip",
                    "https://api.github.com/repos/x", "https://objects.githubusercontent.com/x",
                    "https://release-assets.githubusercontent.com/x"):
            self.assertEqual(updater._check_url(url, src), url)

    def test_zip_path_traversal_and_bad_layout_rejected(self):
        for z in (make_zip(name="../AmneziaMonitor.exe"), make_zip(name="/AmneziaMonitor.exe"),
                  make_zip(name="a/b/c/AmneziaMonitor.exe"),
                  make_zip(extra={"x/AmneziaMonitor.exe": EXE_BYTES}),     # два exe
                  make_zip(exe=b"not a pe file" * 100)):
            gh = FakeGitHub(zip_bytes=z)
            self.addCleanup(gh.close)
            rel = updater.fetch_latest(gh.source())
            with self.assertRaises(updater.UpdateError):
                updater.download_and_stage(rel, self.tmp / "stage", gh.source())

    def test_release_without_assets_cannot_auto_install(self):
        gh = FakeGitHub(assets=False)
        self.addCleanup(gh.close)
        m = updater.UpdateManager(self.tmp, src=gh.source(), current="1.6.0", installable=True)
        s = m.check(manual=True)
        self.assertTrue(s["available"])
        self.assertFalse(s["release"]["has_asset"])
        m.start_download()
        for _ in range(100):
            if m.status()["phase"] != "downloading":
                break
            time.sleep(0.05)
        self.assertEqual(m.status()["phase"], "error")

    def test_non_windows_build_only_notifies(self):
        gh = FakeGitHub()
        self.addCleanup(gh.close)
        m = updater.UpdateManager(self.tmp, src=gh.source(), current="1.6.0", installable=False)
        s = m.check(manual=True)
        self.assertTrue(s["available"] and not s["can_install"])
        with self.assertRaises(updater.UpdateError):
            m.start_download()
        self.assertEqual(gh.asset_hits, 0)

    def test_network_failure_is_a_readable_error(self):
        src = updater.Source(api_base="http://127.0.0.1:1", allowed_hosts=frozenset({"127.0.0.1"}),
                             allowed_schemes=("http",), timeout=2)
        m = updater.UpdateManager(self.tmp, src=src, current="1.6.0", installable=True)
        s = m.check(manual=True)
        self.assertIn("Нет связи", s["error"])
        s = m.check(manual=False)                   # фоновая проверка молчит
        self.assertEqual(s["phase"], "idle")

    def test_skip_version_and_auto_check_persist(self):
        gh = FakeGitHub(version="1.9.0")
        self.addCleanup(gh.close)
        m = updater.UpdateManager(self.tmp, src=gh.source(), current="1.6.0", installable=True)
        m.check(manual=True)
        m.skip("1.9.0")
        self.assertTrue(m.status()["skipped"])
        m.set_auto_check(False)
        m2 = updater.UpdateManager(self.tmp, src=gh.source(), current="1.6.0", installable=True)   # «перезапуск»
        self.assertFalse(m2.auto_check)
        self.assertFalse(m2.auto_check_due())
        m2.check(manual=True)
        self.assertTrue(m2.status()["skipped"])
        gh.version = "1.9.1"                         # вышла следующая версия — снова предлагаем
        self.assertFalse(m2.check(manual=True)["skipped"])
        m2.unskip()
        with self.assertRaises(updater.UpdateError):
            m2.skip("latest")

    def test_full_flow_download_then_restart_only_on_click(self):
        gh = FakeGitHub()
        self.addCleanup(gh.close)
        launched, exited = [], threading.Event()
        m = updater.UpdateManager(self.tmp, src=gh.source(), current="1.6.0", installable=True,
                                  target=self.tmp / "AmneziaMonitor.exe",
                                  launcher=lambda t, n, v: launched.append((t, n, v)), on_exit=exited.set)
        m.check(manual=True)
        self.assertEqual(gh.asset_hits, 0)
        self.assertEqual(m.status()["phase"], "idle")
        m.start_download()
        for _ in range(100):
            if m.status()["phase"] == "ready":
                break
            time.sleep(0.05)
        self.assertEqual(m.status()["phase"], "ready")
        self.assertEqual(launched, [], "перезапуск без нажатия невозможен")
        m.restart_now()
        self.assertTrue(exited.wait(3))
        self.assertEqual(launched[0][2], "1.9.0")
        self.assertTrue(launched[0][1].is_file())


def script(path: Path, body: str):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@unittest.skipIf(sys.platform.startswith("win"), "тестовые «exe» — shell-скрипты")
class ApplyUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = self.tmp / "app"
        self.app.mkdir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "servers.yaml").write_text("servers: []\n")
        (self.data / "amnezia_monitor.db").write_bytes(b"SQLITE-DATA")
        self.target = self.app / "AmneziaMonitor.exe"
        script(self.target, "echo old\n")
        self.new = self.tmp / "AmneziaMonitor.exe.new"
        self.spawned = []

    def good_new(self):
        script(self.new, 'if [ "$1" = "--update-selftest" ]; then echo \'{"version": "9.9.9"}\' > "$2"; exit 0; fi\necho new\n')

    def spawn(self, rc=None):
        def f(exe, args):
            self.spawned.append((Path(exe).read_text(), list(args)))
            p = mock.Mock()
            if rc is None:
                p.wait.side_effect = subprocess.TimeoutExpired("x", 1)
            else:
                p.wait.return_value = rc
            return p
        return f

    def result(self):
        return json.loads((self.data / "update_result.json").read_text())

    def test_swap_keeps_backup_and_data_and_restarts(self):
        self.good_new()
        waiter = subprocess.Popen(["sleep", "1"])
        t0 = time.time()
        ok = updater.apply_update(self.target, self.new, [waiter.pid], self.data, "9.9.9",
                                  health_seconds=0.2, restart_args=["--x"], spawn=self.spawn())
        waiter.wait()
        self.assertTrue(ok)
        self.assertGreater(time.time() - t0, 0.8, "помощник обязан дождаться выхода программы")
        self.assertIn("new", self.target.read_text())
        self.assertEqual((self.app / "AmneziaMonitor.exe.bak").read_text(), "#!/bin/sh\necho old\n")
        self.assertEqual((self.data / "servers.yaml").read_text(), "servers: []\n")
        self.assertEqual((self.data / "amnezia_monitor.db").read_bytes(), b"SQLITE-DATA")
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("new", self.spawned[0][0])
        self.assertEqual(self.spawned[0][1], ["--x"])
        self.assertTrue(self.result()["ok"])
        self.assertFalse((self.app / "AmneziaMonitor.exe.new").exists())

    def test_failed_selftest_changes_nothing_and_relaunches_old(self):
        script(self.new, "exit 3\n")
        ok = updater.apply_update(self.target, self.new, [], self.data, "9.9.9", health_seconds=0.2, spawn=self.spawn())
        self.assertFalse(ok)
        self.assertIn("old", self.target.read_text())
        self.assertFalse((self.app / "AmneziaMonitor.exe.bak").exists())
        self.assertIn("old", self.spawned[0][0])
        self.assertFalse(self.result()["ok"])

    def test_wrong_version_in_selftest_is_rejected(self):
        script(self.new, 'echo \'{"version": "1.0.0"}\' > "$2"\n')
        self.assertFalse(updater.apply_update(self.target, self.new, [], self.data, "9.9.9", spawn=self.spawn()))
        self.assertIn("old", self.target.read_text())

    def test_crash_on_first_start_rolls_back(self):
        self.good_new()
        ok = updater.apply_update(self.target, self.new, [], self.data, "9.9.9", health_seconds=0.2, spawn=self.spawn(rc=1))
        self.assertFalse(ok)
        self.assertIn("old", self.target.read_text())
        self.assertEqual(len(self.spawned), 2)
        self.assertIn("new", self.spawned[0][0])
        self.assertIn("old", self.spawned[1][0], "после отката запускается прежняя версия")
        self.assertIn("Возвращена прежняя версия", self.result()["message"])
        self.assertEqual((self.data / "amnezia_monitor.db").read_bytes(), b"SQLITE-DATA")

    def test_readonly_app_folder_fails_cleanly(self):
        if os.geteuid() == 0:
            self.skipTest("root обходит права")
        self.good_new()
        self.app.chmod(0o555)
        self.addCleanup(lambda: self.app.chmod(0o755))
        ok = updater.apply_update(self.target, self.new, [], self.data, "9.9.9", health_seconds=0.2, spawn=self.spawn())
        self.assertFalse(ok)
        self.assertIn("old", self.target.read_text())
        self.assertIn("Нет права записи", self.result()["message"])

    def test_timeout_waiting_for_app_exit(self):
        self.good_new()
        sleeper = subprocess.Popen(["sleep", "5"])
        self.addCleanup(sleeper.kill)
        ok = updater.apply_update(self.target, self.new, [sleeper.pid], self.data, "9.9.9", wait_timeout=0.6,
                                  spawn=self.spawn())
        self.assertFalse(ok)
        self.assertIn("old", self.target.read_text())
        self.assertEqual(self.spawned, [])

    def test_helper_main_cli(self):
        self.good_new()
        argv = ["--apply-update", "--target", str(self.target), "--new", str(self.new), "--data-dir", str(self.data),
                "--version", "9.9.9", "--pid", "0"]
        orig = updater.apply_update
        with mock.patch.object(updater, "_spawn", self.spawn()), \
                mock.patch.object(updater, "apply_update", lambda *a, **k: orig(*a, health_seconds=0.2, **k)):
            self.assertEqual(updater.helper_main(argv), 0)
        self.assertIn("new", self.target.read_text())

    def test_selftest_main_writes_version(self):
        out = self.tmp / "st.json"
        self.assertEqual(updater.selftest_main(str(out)), 0)
        self.assertEqual(json.loads(out.read_text())["version"], "1.6.0")
        self.assertEqual(app_main.main(["--update-selftest", str(out)]), 0)


class UpdateHTTP(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(secrets_store, "IS_WINDOWS", False)
        p.start()
        self.addCleanup(p.stop)
        self.dir = Path(tempfile.mkdtemp())
        self.cfgp = self.dir / "servers.yaml"
        st.write_raw(self.cfgp, {"interval": 60, "servers": []})
        self.holder = app_main.ConfigHolder(self.cfgp, self.dir / "m.db")
        self.gh = FakeGitHub(version="1.9.0")
        self.addCleanup(self.gh.close)
        self.opened = []
        self.launched = []
        self.mgr = updater.UpdateManager(self.dir, src=self.gh.source(), current="1.6.0", installable=True,
                                         launcher=lambda t, n, v: self.launched.append(v), on_exit=lambda: None)
        self.ctx = st.SettingsContext(self.cfgp, "TOKEN123", str(self.dir), updates=self.mgr,
                                      open_url=self.opened.append)
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
        raw = r.read().decode()
        c.close()
        return r.status, (json.loads(raw) if raw.startswith("{") else raw)

    def test_token_required(self):
        for m, p in (("GET", "/api/update/status"), ("POST", "/api/update/check"), ("POST", "/api/update/download"),
                     ("POST", "/api/update/restart")):
            code, _ = self.req(m, p, {} if m == "POST" else None, token=None)
            self.assertEqual(code, 403, p)
            code, _ = self.req(m, p, {} if m == "POST" else None, token="bad")
            self.assertEqual(code, 403, p)
        self.assertEqual(self.gh.api_hits + self.gh.asset_hits, 0)

    def test_check_download_restart_via_api(self):
        code, s = self.req("GET", "/api/update/status")
        self.assertEqual((code, s["available"], s["current"]), (200, False, "1.6.0"))
        code, s = self.req("POST", "/api/update/check", {})
        self.assertTrue(s["available"])
        self.assertEqual(self.gh.asset_hits, 0)
        code, s = self.req("POST", "/api/update/restart", {})
        self.assertEqual(code, 400)                         # ещё не скачано
        self.assertEqual(self.launched, [])
        self.req("POST", "/api/update/download", {})
        for _ in range(100):
            if self.req("GET", "/api/update/status")[1]["phase"] == "ready":
                break
            time.sleep(0.05)
        self.assertEqual(self.launched, [])
        code, s = self.req("POST", "/api/update/restart", {})
        self.assertEqual(code, 200)
        self.assertEqual(self.launched, ["1.9.0"])

    def test_tampered_hash_via_api_reports_error(self):
        self.gh.sha = "1" * 64
        self.req("POST", "/api/update/check", {})
        self.req("POST", "/api/update/download", {})
        for _ in range(100):
            s = self.req("GET", "/api/update/status")[1]
            if s["phase"] != "downloading":
                break
            time.sleep(0.05)
        self.assertEqual(s["phase"], "error")
        self.assertIn("SHA-256", s["error"])
        self.assertEqual(self.launched, [])

    def test_settings_skip_open_ack(self):
        self.req("POST", "/api/update/check", {})
        code, s = self.req("POST", "/api/update/settings", {"auto_check": False})
        self.assertFalse(s["auto_check"])
        code, _ = self.req("POST", "/api/update/settings", {"auto_check": "yes"})
        self.assertEqual(code, 400)
        code, s = self.req("POST", "/api/update/skip", {"version": "1.9.0"})
        self.assertTrue(s["skipped"])
        code, s = self.req("POST", "/api/update/skip", {"version": ""})
        self.assertFalse(s["skipped"])
        code, _ = self.req("POST", "/api/update/skip", {"version": "<script>"})
        self.assertEqual(code, 400)
        code, _ = self.req("POST", "/api/update/open", {})
        self.assertEqual(code, 200)
        self.assertEqual(len(self.opened), 1)
        self.assertTrue(self.opened[0].startswith("http://127.0.0.1"))   # тестовый «GitHub»

    def test_open_refuses_foreign_url(self):
        self.req("POST", "/api/update/check", {})
        self.mgr.release.page_url = "https://evil.example.com/x"
        code, _ = self.req("POST", "/api/update/open", {})
        self.assertEqual(code, 400)
        self.assertEqual(self.opened, [])

    def test_last_result_shown_once_then_acked(self):
        updater.write_result(self.dir, True, "1.9.0", "Обновлено")
        s = self.req("GET", "/api/update/status")[1]
        self.assertTrue(s["last_result"]["ok"])
        self.req("POST", "/api/update/ack", {})
        self.assertIsNone(self.req("GET", "/api/update/status")[1]["last_result"])

    def test_page_has_update_ui_and_settings_report_version(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/")
        page = c.getresponse().read().decode()
        self.assertIn("updBanner", page)
        self.assertIn("Проверить обновления", page)
        code, s = self.req("GET", "/api/settings")
        self.assertEqual(s["version"], "1.6.0")

    def test_check_loop_runs_and_stops(self):
        stop = threading.Event()
        t = threading.Thread(target=app_main.update_check_loop, args=(self.mgr, stop, 0.05, 60), daemon=True)
        t.start()
        for _ in range(60):
            if self.gh.api_hits:
                break
            time.sleep(0.05)
        self.assertEqual(self.gh.api_hits, 1)
        self.assertEqual(self.gh.asset_hits, 0)
        stop.set()
        t.join(2)
        self.assertFalse(t.is_alive())

    def test_auto_check_off_means_no_background_request(self):
        self.mgr.set_auto_check(False)
        stop = threading.Event()
        t = threading.Thread(target=app_main.update_check_loop, args=(self.mgr, stop, 0.05, 60), daemon=True)
        t.start()
        time.sleep(0.4)
        stop.set()
        t.join(2)
        self.assertEqual(self.gh.api_hits, 0)


if __name__ == "__main__":
    unittest.main()
