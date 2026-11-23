package org.amnezia.monitor

import org.amnezia.monitor.data.LocalHistory
import org.amnezia.monitor.data.LocalHistory.Sample
import org.amnezia.monitor.data.SshProbe
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** v1.4.0 и раньше: у прямых серверов на телефоне не было истории — графики и доступность пустые. */
class LocalHistoryTest {
    @get:Rule val tmp = TemporaryFolder()

    private val t0 = 1_800_000_000L

    private fun poll(h: LocalHistory, i: Int, up: Boolean = true) = h.record(
        "srv1",
        Sample(t0 + i * 60L, up, tcpMs = 30.0 + i % 3, pingMs = 28.0, cpu = if (up) 10.0 + i else null,
            mem = if (up) 40.0 else null, online = if (up) 2 else null, downBytes = 6e6, upBytes = 1e6),
    )

    @Test
    fun seriesAndStatsAppearAfterPolls() {
        val h = LocalHistory(null)
        val now = t0 + 10 * 60
        assertNull("до опросов истории нет", h.series("srv1", 24, now))
        for (i in 0..9) poll(h, i, up = i != 4)
        val s = h.series("srv1", 24, now)
        assertNotNull(s)
        s!!
        assertEquals(48, s.cpu.size)
        assertTrue(s.cpu.count { it != null } >= 1)
        // последние 10 минут попадают в последнюю корзину (30 мин): среднее CPU удачных опросов
        val expected = (0..9).filter { it != 4 }.map { 10.0 + it }.average()
        assertEquals(expected, s.cpu.last()!!, 0.01)
        assertEquals(2.0, s.online.last()!!, 0.01)
        assertEquals(6e7, s.downBytes.last()!!, 1.0)
        val (avail, lat) = h.stats("srv1", 24, now)
        assertEquals(90.0, avail!!, 0.01)
        assertEquals((0..9).map { 30.0 + it % 3 }.average(), lat!!, 0.06)
        // период 1 ч: те же данные, корзины по 75 с
        assertTrue(h.series("srv1", 1, now)!!.cpu.count { it != null } >= 6)
        assertNull(h.series("other", 24, now))
    }

    @Test
    fun manualRefreshesMergeAndOldSamplesExpire() {
        val h = LocalHistory(null, keepSec = 3600, minGapSec = 45)
        h.record("a", Sample(t0, true, cpu = 5.0, downBytes = 100.0))
        h.record("a", Sample(t0 + 10, true, cpu = 7.0, downBytes = 50.0))
        assertEquals(1, h.samples("a").size)
        assertEquals(150.0, h.samples("a")[0].downBytes!!, 0.01)
        assertEquals(7.0, h.samples("a")[0].cpu!!, 0.01)
        h.record("a", Sample(t0 + 7200, true, cpu = 9.0))
        assertEquals(listOf(t0 + 7200), h.samples("a").map { it.ts })
        h.forget("a")
        assertTrue(h.samples("a").isEmpty())
    }

    @Test
    fun survivesRestartAndStartsCleanAfterUpdate() {
        val f = tmp.root.resolve("local_history.v1")
        // обновление с v1.3.0: файла истории ещё нет — пусто, без ошибок, дальше копится
        val first = LocalHistory(f)
        assertTrue(first.samples("srv1").isEmpty())
        for (i in 0..5) poll(first, i)
        first.save(force = true)
        assertTrue(f.exists())
        assertTrue("в файле только id и числа", f.readText().lines().drop(1).all { it.isEmpty() || it == "#srv1" || it.matches(Regex("[0-9.,-]*")) })
        val second = LocalHistory(f)
        assertEquals(first.samples("srv1"), second.samples("srv1"))
        assertNotNull(second.series("srv1", 24, t0 + 400))
        // испорченный файл не роняет приложение
        f.writeText("garbage")
        assertTrue(LocalHistory(f).samples("srv1").isEmpty())
    }

    @Test
    fun parsesPingOutput() {
        assertEquals(32.4, SshProbe.parsePing("2 packets transmitted, 2 received\nrtt min/avg/max/mdev = 30.1/32.4/34.7/2.3 ms")!!, 0.001)
        assertEquals(21.5, SshProbe.parsePing("64 bytes from 192.0.2.1: icmp_seq=1 ttl=50 time=20 ms\n64 bytes: time=23 ms")!!, 0.001)
        assertNull(SshProbe.parsePing("1 packets transmitted, 0 received, 100% packet loss"))
        assertNull(SshProbe.icmpPing("-c 100 192.0.2.1"))
    }
}
