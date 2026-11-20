# -*- coding: utf-8 -*-
"""Ключи хостов: свой known_hosts, смена ключа с отпечатками, «Доверять новому ключу», типы ключей.

Интеграционные тесты поднимают локальный SSH-сервер paramiko на 127.0.0.1 (без сети).
"""
import logging
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.getLogger("paramiko").setLevel(logging.CRITICAL + 1)

import paramiko  # noqa: E402

import amnezia_monitor as am  # noqa: E402
import app_settings as st  # noqa: E402
import hostkeys  # noqa: E402

PW = "test-only-password"


class _Server(paramiko.ServerInterface):
    def __init__(self, seen):
        self.seen = seen

    def check_auth_password(self, username, password):
        self.seen.append(("auth", username))
        return paramiko.AUTH_SUCCESSFUL if password == PW else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class LocalSSH:
    """Минимальный SSH-сервер: только обмен ключами и вход по паролю."""

    def __init__(self, keys):
        self.keys = keys
        self.seen = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)       # accept() не должен пережить close() и «украсть» чужой сокет
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                c, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c.settimeout(None)
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        t = paramiko.Transport(c)
        for k in self.keys:
            t.add_server_key(k)
        try:
            t.start_server(server=_Server(self.seen))
            t.accept(5)
        except Exception:
            pass

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.sock.close()




def gen_keys():
    # Ed25519 paramiko генерировать не умеет — берём ECDSA и RSA двух поколений.
    return {
        "ecdsa1": paramiko.ECDSAKey.generate(),
        "ecdsa2": paramiko.ECDSAKey.generate(),
        "rsa1": paramiko.RSAKey.generate(2048),
    }


KEYS = gen_keys()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".ssh").mkdir(parents=True)
        self.app_file = Path(self.tmp.name) / "data" / "known_hosts"
        hostkeys.set_store_path(self.app_file)
        self.patch = mock.patch.object(hostkeys, "user_known_hosts", lambda: self.home / ".ssh" / "known_hosts")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        hostkeys.set_store_path(None)
        self.tmp.cleanup()

    def srv(self, port):
        return am.ServerCfg(name="t", host="127.0.0.1", port=port, user="root", password=PW, timeout=5)

    def write_user_kh(self, port, key):
        hk = paramiko.HostKeys()
        hk.add(hostkeys.host_entry_name("127.0.0.1", port), key.get_name(), key)
        hk.save(str(self.home / ".ssh" / "known_hosts"))


class TestHostKeysOffline(Base):
    def test_entry_name_and_fingerprint(self):
        self.assertEqual(hostkeys.host_entry_name("203.0.113.5", 22), "203.0.113.5")
        self.assertEqual(hostkeys.host_entry_name("203.0.113.5", 2222), "[203.0.113.5]:2222")
        fp = hostkeys.fingerprint(KEYS["ecdsa1"])
        self.assertTrue(fp.startswith("SHA256:") and len(fp) == 50)

    def test_disabled_algorithms_keep_known_types(self):
        d = hostkeys.disabled_algorithms_for({"ssh-rsa": KEYS["rsa1"]})["keys"]
        for a in ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"):
            self.assertNotIn(a, d)
        self.assertIn("ssh-ed25519", d)
        self.assertIsNone(hostkeys.disabled_algorithms_for({}))

    def test_trust_requires_confirmation_and_matching_fingerprint(self):
        new = KEYS["ecdsa2"]
        fetch = lambda h, p, **kw: new  # noqa: E731
        with self.assertRaises(hostkeys.TrustError):
            hostkeys.trust_new_key("127.0.0.1", 22, hostkeys.fingerprint(new), confirmed=False, fetch=fetch)
        with self.assertRaises(hostkeys.TrustError):
            hostkeys.trust_new_key("127.0.0.1", 22, hostkeys.fingerprint(KEYS["ecdsa1"]), confirmed=True,
                                   fetch=fetch)
        self.assertFalse(self.app_file.exists())
        r = hostkeys.trust_new_key("127.0.0.1", 22, hostkeys.fingerprint(new), confirmed=True, fetch=fetch)
        self.assertTrue(r["ok"])
        known, source = hostkeys.lookup("127.0.0.1", 22)
        self.assertEqual(source, "app")
        self.assertEqual(list(known.values()), [new])

    def test_trust_replaces_only_that_host(self):
        hostkeys.remember_first_key("198.51.100.7", 22, KEYS["rsa1"])
        hostkeys.remember_first_key("127.0.0.1", 22, KEYS["ecdsa1"])
        hostkeys.trust_new_key("127.0.0.1", 22, hostkeys.fingerprint(KEYS["ecdsa2"]), True,
                               fetch=lambda h, p, **kw: KEYS["ecdsa2"])
        self.assertEqual(list(hostkeys.lookup("198.51.100.7", 22)[0].values()), [KEYS["rsa1"]])
        self.assertEqual(list(hostkeys.lookup("127.0.0.1", 22)[0].values()), [KEYS["ecdsa2"]])

    def test_settings_trust_endpoint_validates(self):
        ctx = st.SettingsContext(Path(self.tmp.name) / "servers.yaml", "tok",
                                 hostkey_fetch=lambda h, p, **kw: KEYS["ecdsa2"])
        with self.assertRaises(st.SettingsError):
            ctx.trust_hostkey({"host": "127.0.0.1", "port": 22, "fingerprint": hostkeys.fingerprint(KEYS["ecdsa2"])})
        with self.assertRaises(st.SettingsError):
            ctx.trust_hostkey({"host": "bad host;rm", "port": 22, "fingerprint": "SHA256:x", "confirm": True})
        r = ctx.trust_hostkey({"host": "127.0.0.1", "port": 22, "confirm": True,
                               "fingerprint": hostkeys.fingerprint(KEYS["ecdsa2"])})
        self.assertTrue(r["ok"])


class TestHostKeysLive(Base):
    def test_first_login_saves_to_app_file_not_user_known_hosts(self):
        s = LocalSSH([KEYS["ecdsa1"]])
        try:
            am.SSHRunner(self.srv(s.port)).close()
        finally:
            s.close()
        self.assertFalse((self.home / ".ssh" / "known_hosts").exists())
        known, source = hostkeys.lookup("127.0.0.1", s.port)
        self.assertEqual(source, "app")
        self.assertEqual(list(known.values()), [KEYS["ecdsa1"]])

    def test_changed_key_stops_before_auth_and_reports_fingerprints(self):
        self.write_user_kh(0, KEYS["ecdsa1"])  # чужой порт — не мешает
        s = LocalSSH([KEYS["ecdsa2"]])
        self.write_user_kh(s.port, KEYS["ecdsa1"])
        try:
            with self.assertRaises(hostkeys.HostKeyMismatch) as cm:
                am.SSHRunner(self.srv(s.port))
            res = st.test_connection(self.srv(s.port))
        finally:
            s.close()
        self.assertNotIn("auth", [x[0] for x in s.seen], "пароль не должен уходить на сменившийся ключ")
        d = cm.exception.as_dict()
        self.assertEqual(d["old"][0]["fingerprint"], hostkeys.fingerprint(KEYS["ecdsa1"]))
        self.assertEqual(d["new"]["fingerprint"], hostkeys.fingerprint(KEYS["ecdsa2"]))
        self.assertFalse(d["type_changed"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["hostkey"]["new"]["fingerprint"], hostkeys.fingerprint(KEYS["ecdsa2"]))
        self.assertIn("не совпадает", res["steps"][-1]["detail"])

    def test_trust_then_connect_works_and_user_file_untouched(self):
        s = LocalSSH([KEYS["ecdsa2"]])
        self.write_user_kh(s.port, KEYS["ecdsa1"])
        before = (self.home / ".ssh" / "known_hosts").read_bytes()
        try:
            hostkeys.trust_new_key("127.0.0.1", s.port, hostkeys.fingerprint(KEYS["ecdsa2"]), True)
            am.SSHRunner(self.srv(s.port)).close()
        finally:
            s.close()
        self.assertEqual((self.home / ".ssh" / "known_hosts").read_bytes(), before)
        self.assertIn(("auth", "root"), s.seen)

    def test_no_false_mismatch_when_server_has_several_key_types(self):
        # Сохранён только RSA, сервер предлагает ECDSA и RSA: должен согласоваться RSA, без ошибки.
        s = LocalSSH([KEYS["ecdsa1"], KEYS["rsa1"]])
        self.write_user_kh(s.port, KEYS["rsa1"])
        try:
            am.SSHRunner(self.srv(s.port)).close()
        finally:
            s.close()
        self.assertIn(("auth", "root"), s.seen)

    def test_type_changed_is_reported_as_mismatch(self):
        # Сохранён RSA, а сервер теперь умеет только ECDSA.
        s = LocalSSH([KEYS["ecdsa2"]])
        self.write_user_kh(s.port, KEYS["rsa1"])
        try:
            with self.assertRaises(hostkeys.HostKeyMismatch) as cm:
                am.SSHRunner(self.srv(s.port))
        finally:
            s.close()
        self.assertTrue(cm.exception.type_changed)
        self.assertEqual(cm.exception.as_dict()["new"]["type"], "ECDSA")
        self.assertNotIn("auth", [x[0] for x in s.seen])

    def test_wrong_trust_fingerprint_refused(self):
        s = LocalSSH([KEYS["ecdsa2"]])
        try:
            with self.assertRaises(hostkeys.TrustError):
                hostkeys.trust_new_key("127.0.0.1", s.port, hostkeys.fingerprint(KEYS["ecdsa1"]), True)
        finally:
            s.close()
        self.assertFalse(self.app_file.exists())


if __name__ == "__main__":
    unittest.main()
