package org.amnezia.monitor

import org.amnezia.monitor.data.ApiException
import org.amnezia.monitor.data.ApiJson
import org.amnezia.monitor.data.cleanToken
import org.amnezia.monitor.data.fmtBytes
import org.amnezia.monitor.data.fmtRate
import org.amnezia.monitor.data.friendlyError
import org.amnezia.monitor.data.normalizeBaseUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ApiParseTest {
    @Test
    fun parsesServerListAndNodata() {
        val raw = """
            [
              {"name":"test1","host":"203.0.113.11","status":"up","age":12,"tcp_ms":20,
               "cpu":15.5,"mem":40,"peers_online":2,"peers_total":5,"rx_bps":1000,"tx_bps":2000,"error":null},
              {"name":"пусто","host":"203.0.113.14","status":"nodata"}
            ]
        """.trimIndent()
        val list = ApiJson.parseServers(raw)
        assertEquals(2, list.size)
        assertEquals("up", list[0].status)
        assertEquals(2, list[0].peersOnline)
        assertEquals(2000.0, list[0].txBps!!, 0.01)
        assertEquals("nodata", list[1].status)
        assertNull(list[1].cpu)
        assertNull(list[1].error)
    }

    @Test
    fun parsesDetailSeriesUsersAndDns() {
        val raw = """
            {
              "name":"test1","host":"203.0.113.11",
              "current":{"status":"down","error":"SSH timeout","age":9,"tcp_ms":4,"ping_ms":3.2,
                "availability":99.1,"latency_avg":8,"uptime_s":3600,"load1":0.4,"nproc":2,
                "cpu":10,"mem":40,"mem_used":100,"mem_total":200,"rx_bps":1,"tx_bps":2,"stale":true},
              "containers":[{"name":"amnezia-awg","type":"awg","up":true,"status":"Up"}],
              "series":{"start":1,"end":2,"buckets":2,"cpu":[1,null],"mem":[2,3],"online":[1,0],
                "down_bytes":[10,0],"up_bytes":[0,5],"avail":[1,null]},
              "users":[{"client":"Дмитрий — телефон","pubkey":"abc…","container":"amnezia-awg",
                "online":false,"handshake_age":500,"endpoint":"198.51.100.9","rate_down":null,"rate_up":null,
                "spark":[1,2],"down_24h":100,"up_24h":50,"down_total":1000,"up_total":400}],
              "totals":{"down_24h":100,"up_24h":50},
              "dns":{"status":"ok","note":null,"queries":3,"top":[{"site":"youtube.com","n":2}],
                "per_user":[{"client":"Дмитрий — телефон","total":2,"top":[{"site":"youtube.com","n":2}]}]},
              "events":[{"ts":1800000000,"kind":"peer","message":"подключился"}]
            }
        """.trimIndent()
        val d = ApiJson.parseDetail(raw)
        assertEquals("down", d.current.status)
        assertTrue(d.current.stale)
        assertEquals("SSH timeout", d.current.error)
        assertNull(d.series!!.cpu[1])
        assertEquals(10.0, d.series.downBytes[0]!!, 0.01)
        assertEquals("Дмитрий — телефон", d.users[0].client)
        assertFalse(d.users[0].online)
        assertNull(d.users[0].rateDown)
        assertEquals("youtube.com", d.dns.top[0].site)
        assertEquals(2, d.dns.perUser[0].total)
        assertEquals("подключился", d.events[0].message)
        assertTrue(d.containers[0].up)
    }

    @Test
    fun nodataDetailAndErrorObject() {
        val nodata = """{"name":"x","status":"nodata","users":[],"containers":[],"events":[],"series":null,
            "dns":{"status":"nodata","note":"Данных пока нет"}}"""
        val d = ApiJson.parseDetail(nodata)
        assertEquals("nodata", d.current.status)
        assertNull(d.series)
        assertEquals("Данных пока нет", d.dns.note)
        try {
            ApiJson.parseServers("""{"error":"bad token"}""")
            throw AssertionError("expected")
        } catch (e: ApiException) {
            assertEquals("bad token", e.message)
        }
    }

    @Test
    fun urlAndTokenRules() {
        assertEquals("http://192.168.1.104:8765", normalizeBaseUrl("192.168.1.104:8765"))
        assertEquals("http://192.168.1.104:8765", normalizeBaseUrl("http://192.168.1.104:8765/"))
        assertEquals("https://mon.example", normalizeBaseUrl("https://mon.example"))
        assertFails { normalizeBaseUrl("http://user:secret@192.168.1.104:8765") }
        assertFails { normalizeBaseUrl("http://192.168.1.104:8765?token=abc") }
        assertFails { normalizeBaseUrl("ftp://192.168.1.104") }
        assertEquals("", cleanToken("  "))
        assertFails { cleanToken("a\nb") }
    }

    @Test
    fun formatsMatchDashboardStyle() {
        assertEquals("1.0 КБ", fmtBytes(1024.0))
        assertEquals("16 Кбит/с", fmtRate(2000.0))
        assertEquals("—", fmtRate(null))
        assertTrue(friendlyError(ApiException(403, "bad token")).contains("токен"))
    }

    private fun assertFails(block: () -> Unit) {
        try {
            block()
            throw AssertionError("expected failure")
        } catch (_: IllegalArgumentException) {
        }
    }
}
