# -*- coding: utf-8 -*-
import re
import sys
import tempfile
import threading
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import amnezia_monitor as am  # noqa: E402
import app_main  # noqa: E402
import app_settings as st  # noqa: E402
import dashboard  # noqa: E402
import dns_setup  # noqa: E402
import secrets_store  # noqa: E402

SAMPLE_UNBOUND = """
server:
    directory: "/opt/unbound/etc/unbound"
    # Do not print one line per query to the log
    log-queries: no
    # logfile: /opt/unbound/etc/unbound/unbound.log
    logfile: /dev/null
    verbosity: 0
    # other important settings
    interface: 0.0.0.0
forward-zone:
    name: "."
    forward-addr: 1.1.1.1
"""

SAMPLE_ADGUARD = """
bind_host: 0.0.0.0
querylog:
  ignored: []
  interval: 2160h
  size_memory: 1000
  enabled: false
  file_enabled: false
users:
  - name: admin
    password: HASHPLACEHOLDER
"""

QUERY_LINE = "[1800000000] unbound[1:0] info: 10.8.1.2 www.example.com. A IN\n"


class FakeRemote(dns_setup.Remote):
    def __init__(self, kind="unbound"):
        self.kind = kind
        self.container_files = {}
        if kind == "unbound":
            self.container_files[dns_setup.UNBOUND_CONF] = SAMPLE_UNBOUND
            self.container_files[dns_setup.UNBOUND_LOG] = ""
        else:
            self.container_files[dns_setup.ADGUARD_YAML_CANDIDATES[0]] = SAMPLE_ADGUARD
            self.container_files[dns_setup.ADGUARD_QUERYLOG] = ""
        self.host_files = {}
        self.restarted = []
        self.timer_active = False
        self.timer_enabled = False
        self.dns_status = "running healthy"
        self.cascade = "inactive"
        self.commands = []
        self.containers = "amnezia-awg2|Up 6 days|amnezia-awg2\namnezia-dns|Up 2 days (healthy)|amnezia-dns\n"

    def run(self, cmd, timeout=60):
        self.commands.append(cmd)
        if re.search(r"\b(restart|kill|rm|stop)\b.*\b(cascade|xray|awg|openvpn)\b", cmd, re.I):
            raise AssertionError("forbidden command: " + cmd)
        if cmd.startswith("systemctl "):
            return self._systemctl(cmd[len("systemctl "):])
        if "ps -a --format" in cmd:
            return 0, self.containers
        m = re.search(r"exec (\S+) sh -c '(.*)'$", cmd)
        if m:
            return self._exec(m.group(1), m.group(2))
        m = re.search(r"\bcp (/tmp/\S+) (\S+):(\S+)$", cmd)
        if m:
            src, container, dest = m.group(1), m.group(2), m.group(3)
            dns_setup.assert_safe_dns_name(container)
            if src not in self.host_files:
                return 1, "no such tmp file"
            self.container_files[dest] = self.host_files[src].decode("utf-8") if isinstance(self.host_files[src], bytes) else self.host_files[src]
            return 0, ""
        m = re.search(r"\brestart (\S+)$", cmd)
        if m:
            name = m.group(1)
            dns_setup.assert_safe_dns_name(name)
            self.restarted.append(name)
            return 0, name
        if "inspect -f" in cmd:
            return 0, self.dns_status
        if cmd.startswith("rm -f "):
            for path in cmd.split()[2:]:
                self.host_files.pop(path, None)
            return 0, ""
        if cmd == dns_setup.CLEANUP_SCRIPT_REMOTE:
            # imitates first run: create empty log
            self.container_files.setdefault(dns_setup.UNBOUND_LOG, "")
            return 0, "active_bytes=0\n"
        return 1, "unexpected: " + cmd[:200]

    def _systemctl(self, args):
        if args not in dns_setup._SYSTEMCTL_OK:
            return 1, "unexpected systemctl " + args
        if args == "daemon-reload":
            return 0, ""
        if args == "enable --now amnezia-dns-log-cleanup.timer":
            self.timer_active = self.timer_enabled = True
            return 0, ""
        if args == "disable --now amnezia-dns-log-cleanup.timer":
            self.timer_active = self.timer_enabled = False
            return 0, ""
        if args == "is-active amnezia-dns-log-cleanup.timer":
            return (0, "active\n") if self.timer_active else (3, "inactive\n")
        if args == "is-enabled amnezia-dns-log-cleanup.timer":
            return (0, "enabled\n") if self.timer_enabled else (1, "disabled\n")
        if args == "is-active amnezia-dns-log-cleanup.service":
            return 3, "inactive\n"
        if args == "is-active cascade-xray.service":
            return (0, "active\n") if self.cascade == "active" else (3, "inactive\n")
        return 1, "unhandled"

    def _exec(self, container, script):
        dns_setup.assert_safe_dns_name(container)
        # backup
        if "bak-before-querylog" in script and "cp " in script:
            if dns_setup.UNBOUND_BAK not in self.container_files and dns_setup.UNBOUND_CONF in self.container_files:
                self.container_files[dns_setup.UNBOUND_BAK] = self.container_files[dns_setup.UNBOUND_CONF]
            return 0, "BACKED\n"
        # cat a path
        m = re.search(r"cat (\S+)(?: 2>/dev/null)?", script)
        if m and script.strip().startswith("cat "):
            path = m.group(1)
            data = self.container_files.get(path)
            return (0, data) if data is not None else (1, "")
        # combined tail/cat logs
        if "tail -c" in script or "unbound.log" in script or "querylog.json" in script:
            chunks = []
            for path, data in self.container_files.items():
                if path.endswith("unbound.log") or path.endswith("querylog.json") or "/log-archive/" in path:
                    chunks.append(data)
            return 0, "".join(chunks)
        return 1, "unhandled exec: " + script[:200]

    def put(self, data, path, mode=0o644):
        self.host_files[path] = data


class PatchTests(unittest.TestCase):
    def test_unbound_patch_is_precise_and_idempotent(self):
        enabled = dns_setup.patch_unbound_conf(SAMPLE_UNBOUND, True)
        self.assertIn("log-queries: yes", enabled)
        self.assertIn("logfile: unbound.log", enabled)
        self.assertIn("# logfile: /opt/unbound/etc/unbound/unbound.log", enabled)
        self.assertIn("forward-addr: 1.1.1.1", enabled)
        self.assertIn("interface: 0.0.0.0", enabled)
        self.assertTrue(dns_setup.unbound_enabled(enabled))
        self.assertFalse(dns_setup.unbound_enabled(SAMPLE_UNBOUND))
        again = dns_setup.patch_unbound_conf(enabled, True)
        self.assertEqual(enabled, again)
        disabled = dns_setup.patch_unbound_conf(enabled, False)
        self.assertIn("log-queries: no", disabled)
        self.assertIn("logfile: /dev/null", disabled)
        self.assertFalse(dns_setup.unbound_enabled(disabled))

    def test_adguard_patch_keeps_other_keys(self):
        enabled = dns_setup.patch_adguard_yaml(SAMPLE_ADGUARD, True)
        self.assertIn("password: HASHPLACEHOLDER", enabled)
        self.assertIn("enabled: true", enabled)
        self.assertIn("file_enabled: true", enabled)
        self.assertIn("interval: 24h", enabled)
        self.assertTrue(dns_setup.adguard_enabled(enabled))
        self.assertFalse(dns_setup.adguard_enabled(SAMPLE_ADGUARD))

    def test_refuse_non_dns_names(self):
        with self.assertRaises(ValueError):
            dns_setup.assert_safe_dns_name("amnezia-awg2")
        with self.assertRaises(ValueError):
            dns_setup.assert_safe_dns_name("cascade-xray")
        with self.assertRaises(ValueError):
            dns_setup.assert_safe_dns_name("amnezia-dns; rm -rf /")
        self.assertEqual(dns_setup.assert_safe_dns_name("amnezia-dns"), "amnezia-dns")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        # skip real 2s sleeps while waiting for health
        self.sleep = mock.patch.object(dns_setup.time, "sleep", lambda s: None)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_enable_check_disable_unbound(self):
        r = FakeRemote("unbound")
        r.cascade = "active"
        before = dns_setup.apply(r, "docker", "check")
        self.assertEqual(before["status"], "no_log")
        self.assertEqual(before["mode"], "unbound")
        self.assertFalse(before["enabled"])

        res = dns_setup.apply(r, "docker", "enable")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["enabled"])
        self.assertTrue(res["timer"])
        self.assertEqual(r.restarted, ["amnezia-dns"])
        self.assertIn("log-queries: yes", r.container_files[dns_setup.UNBOUND_CONF])
        self.assertIn("interface: 0.0.0.0", r.container_files[dns_setup.UNBOUND_CONF])
        self.assertIn(dns_setup.UNBOUND_BAK, r.container_files)
        self.assertIn(dns_setup.CLEANUP_SCRIPT_REMOTE, r.host_files)
        self.assertEqual(res["cascade_xray"], "active")
        # conf did not mention passwords, response must not invent secrets
        blob = json.dumps(res)
        self.assertNotIn("password", blob)

        # idempotent: second enable does not restart again
        r.restarted.clear()
        res2 = dns_setup.apply(r, "docker", "enable")
        self.assertTrue(res2["ok"], res2)
        self.assertEqual(r.restarted, [])

        # records become visible
        r.container_files[dns_setup.UNBOUND_LOG] = QUERY_LINE
        chk = dns_setup.apply(r, "docker", "check")
        self.assertEqual(chk["status"], "ok")
        self.assertEqual(chk["parseable"], 1)
        self.assertNotIn("example.com", json.dumps(chk))

        off = dns_setup.apply(r, "docker", "disable")
        self.assertTrue(off["ok"], off)
        self.assertFalse(off["enabled"])
        self.assertFalse(off["timer"])
        self.assertIn("log-queries: no", r.container_files[dns_setup.UNBOUND_CONF])
        self.assertIn("amnezia-dns", r.restarted)

    def test_enable_adguard(self):
        r = FakeRemote("adguard")
        res = dns_setup.apply(r, "docker", "enable")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["mode"], "adguard")
        yaml = r.container_files[dns_setup.ADGUARD_YAML_CANDIDATES[0]]
        self.assertIn("enabled: true", yaml)
        self.assertIn("password: HASHPLACEHOLDER", yaml)
        self.assertEqual(r.restarted, ["amnezia-dns"])

    def test_no_container(self):
        r = FakeRemote()
        r.containers = "amnezia-awg2|Up 6 days|amnezia-awg2\n"
        res = dns_setup.apply(r, "docker", "enable")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "no_container")
        self.assertEqual(r.restarted, [])


class DnsLogHTTP(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(secrets_store, "IS_WINDOWS", False)
        p.start()
        self.addCleanup(p.stop)
        self.sleep = mock.patch.object(dns_setup.time, "sleep", lambda s: None)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.dir = Path(tempfile.mkdtemp())
        self.cfgp = self.dir / "servers.yaml"
        st.write_raw(self.cfgp, st.apply_update({}, {"servers": [{"name": "a", "host": "203.0.113.1",
                                                                  "password": "pw-a"}]}))
        self.remote = FakeRemote("unbound")
        self.ctx = st.SettingsContext(self.cfgp, "TOKEN123", str(self.dir),
                                      dns_remote_factory=lambda srv: self.remote)
        self.httpd = dashboard.make_server(lambda: am.Store(str(self.dir / "m.db")),
                                           lambda: am.load_config(str(self.cfgp)),
                                           "127.0.0.1", 0, settings=self.ctx)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close()))

    def req(self, method, path, body=None, token="TOKEN123"):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"X-Amnezia-Token": token} if token else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read().decode()
        c.close()
        return r.status, raw

    def test_enable_requires_token_and_hides_secrets(self):
        self.assertEqual(self.req("POST", "/api/server/dns-log",
                                  {"name": "a", "action": "enable"}, token=None)[0], 403)
        self.assertEqual(self.req("POST", "/api/server/dns-log",
                                  {"name": "nope", "action": "enable"})[0], 400)
        code, raw = self.req("POST", "/api/server/dns-log", {"name": "a", "action": "enable"})
        self.assertEqual(code, 200, raw)
        res = json.loads(raw)
        self.assertTrue(res["ok"], res)
        self.assertNotIn("pw-a", raw)
        self.assertTrue(self.remote.timer_active)
        self.assertEqual(self.remote.restarted, ["amnezia-dns"])


class ReadCommand(unittest.TestCase):
    def test_new_dns_log_still_whitelisted(self):
        c = am.READ_ONLY_COMMANDS["dns_log"].format(docker="docker", c="amnezia-dns", n=am.DNS_TAIL_LINES)
        self.assertTrue(am._ALLOWED_RE.match(c), c)
        self.assertIn("/opt/unbound/etc/unbound/unbound.log", c)
        self.assertIn("log-archive", c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
