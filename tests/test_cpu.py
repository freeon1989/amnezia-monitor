# -*- coding: utf-8 -*-
"""CPU: два чтения /proc/stat в одной команде и регрессия «CPU — на серверах с DNS-журналом»."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import amnezia_monitor as am  # noqa: E402

NOW = 1_800_000_000.0
# Реальный вид: KVM-VPS на 1 ядро, Ubuntu (числа как на настоящем сервере, адрес не важен)
ONE_CORE = ("cpu  1898 91 2636 74816 81 0 71 0 0 0\n"
            "cpu  1902 91 2643 74905 81 0 71 0 0 0\n")
STEAL = ("cpu  10000 0 5000 80000 100 0 50 2000 0 0\n"
         "cpu0 10000 0 5000 80000 100 0 50 2000 0 0\n"
         "cpu  10020 0 5010 80050 100 0 50 2020 0 0\n")


class ProcStat(unittest.TestCase):
    def test_one_core_two_samples(self):
        # busy 4+7=11 из 11+89=100 jiffies
        self.assertEqual(am.cpu_from_samples(ONE_CORE), 11.0)
        self.assertEqual(am.parse_proc_stat(ONE_CORE), (1902 + 91 + 2643 + 71, 1902 + 91 + 2643 + 74905 + 81 + 71))

    def test_steal_counts_as_busy_and_per_cpu_lines_ignored(self):
        # busy: user 20 + system 10 + steal 20 = 50; total 50 + idle 50 = 100
        self.assertEqual(am.cpu_from_samples(STEAL), 50.0)

    def test_idle_server_gives_zero_not_none(self):
        t = "cpu  100 0 100 5000 0 0 0 0 0 0\ncpu  100 0 100 5100 0 0 0 0 0 0\n"
        self.assertEqual(am.cpu_from_samples(t), 0.0)

    def test_old_kernel_four_fields_and_busybox(self):
        t = "cpu 100 0 50 1000\ncpu 110 0 60 1080\n"
        self.assertEqual(am.cpu_from_samples(t), 20.0)

    def test_single_sample_and_garbage(self):
        self.assertIsNone(am.cpu_from_samples("cpu  1 2 3 4 5 6 7 8 9 10\n"))
        self.assertIsNone(am.cpu_from_samples(""))
        # локализованный мусор (например, вывод top с LANG=ru) не роняет опрос
        ru_top = "%ЦП(ы):  0,0 us,  9,1 sy,  0,0 ni, 90,9 id\nМиБ Mem :   1968,0 total\n"
        self.assertIsNone(am.cpu_from_samples(ru_top))
        self.assertIsNone(am.parse_proc_stat("cpu  x y z w\n"))
        self.assertIsNone(am.cpu_from_samples("cpu  1 2 3 4\ncpu  1 2 3 4\n"))     # пауза без тиков

    def test_command_is_whitelisted_and_reads_twice(self):
        cmd = am.READ_ONLY_COMMANDS["stat"]
        self.assertEqual(cmd.count("/proc/stat"), 2)
        self.assertTrue(am._ALLOWED_RE.match(cmd))
        self.assertFalse(am._ALLOWED_RE.match("head -1 /proc/stat; rm -rf /"))


class FakeRunner(am.Runner):
    """Сервер с DNS-журналом Unbound: именно на таких CPU пропадал."""

    def __init__(self, tick):
        self.tick = tick

    def run(self, cmd):
        t = self.tick
        if cmd.startswith("head -1 /proc/stat"):
            a = 1000 + t * 300
            return 0, f"cpu  {a} 0 100 {5000 + t * 3000} 0 0 0 0 0 0\ncpu  {a + 30} 0 100 {5070 + t * 3000} 0 0 0 0 0 0\n", ""
        if cmd == "cat /proc/meminfo":
            return 0, "MemTotal: 2000000 kB\nMemAvailable: 1560000 kB\n", ""
        if cmd == "cat /proc/net/dev":
            return 0, f"h\nh\n  eth0: {10**6 + t * 60000} 1 0 0 0 0 0 0 {2 * 10**6 + t * 120000} 1 0 0 0 0 0 0\n", ""
        if cmd == "nproc":
            return 0, "1\n", ""
        if " ps -a " in cmd:
            return 0, "amnezia-awg2|Up 1 hour|amneziavpn/amnezia-awg|0.0.0.0:41000->41000/udp\n" \
                      "amnezia-dns|Up 1 hour|amneziavpn/amnezia-dns|\n", ""
        if "show all dump" in cmd:
            return 0, "awg0\tPRIV=\tPUB=\t41000\toff\n", ""
        if "unbound.log" in cmd:
            return 0, f"[{int(NOW) + t * 60}] unbound[1:0] info: 10.8.1.2 example{t}.com. A IN\n", ""
        return 0, "", ""


class DnsServerStateRegression(unittest.TestCase):
    def test_cpu_and_traffic_survive_polls_with_dns_lines(self):
        cfg = am.Config(servers=[am.ServerCfg(name="s", host="203.0.113.7", password="x")])
        store = am.Store(":memory:")
        for t in range(3):
            r = am.collect(cfg.servers[0], lambda s, t=t: FakeRunner(t), NOW + t * 60, {}, 180,
                           probe=lambda s: (10.0, 10.0))
            self.assertEqual(r.dns_status, "ok")
            am.process(store, cfg, r)
        rows = [tuple(x) for x in store.db.execute("SELECT cpu_pct, rx_bps FROM polls ORDER BY ts")]
        # CPU есть уже с первого опроса (30 из 100 jiffies), скорость сети — со второго
        self.assertEqual([x[0] for x in rows], [30.0, 30.0, 30.0])
        self.assertIsNone(rows[0][1])
        self.assertEqual(rows[1][1], 1000.0)
        st = store.get_state("srv:s")
        self.assertTrue(st and st.get("cpu_raw"))                 # состояние под своим ключом
        self.assertEqual(st["ports"][0]["port"], 41000)
        self.assertEqual([r[0] for r in store.db.execute("SELECT key FROM state")], ["srv:s"])

    def test_fallback_to_previous_poll_delta(self):
        cfg = am.Config(servers=[])
        store = am.Store(":memory:")
        for ts, raw in ((NOW, (100, 1000)), (NOW + 60, (150, 2000))):
            am.process(store, cfg, am.PollResult(server="s", ts=ts, ssh_ok=True, reachable=True, tcp_ms=1,
                                                 cpu_raw=raw))
        self.assertEqual([x[0] for x in store.db.execute("SELECT cpu_pct FROM polls ORDER BY ts")], [None, 5.0])

    def test_old_junk_state_keys_are_purged(self):
        import tempfile
        p = str(Path(tempfile.mkdtemp()) / "m.db")
        s = am.Store(p)
        s.set_state("3a836415d53fa5443917a104e4dd5b6ed37c1e01", {"cpu_raw": [1, 2]})
        s.set_state("srv:a", {"up": True})
        s.set_state("daily_sent", "2026-11-23")
        s.commit()
        s.db.close()
        keys = sorted(r[0] for r in am.Store(p).db.execute("SELECT key FROM state"))
        self.assertEqual(keys, ["daily_sent", "srv:a"])


if __name__ == "__main__":
    unittest.main()
