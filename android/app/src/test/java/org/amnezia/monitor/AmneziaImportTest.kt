package org.amnezia.monitor

import org.amnezia.monitor.data.AmneziaImport
import org.amnezia.monitor.data.ImportedServer
import org.amnezia.monitor.data.ProcParse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.util.Base64
import java.util.zip.Deflater

class AmneziaImportTest {
    @Test
    fun vpnLinkKeepsSshSecretAndDropsVpnKeys() {
        val json = """
            {"hostName":"203.0.113.11","userName":"root","password":"s3cret-ssh","port":22,
             "description":"Дом","containers":[{"container":"amnezia-awg",
             "awg":{"last_config":"WIREGUARD-PRIVATE-KEY-DATA"}}]}
        """.trimIndent()
        val link = "vpn://" + Base64.getUrlEncoder().withoutPadding().encodeToString(qCompress(json.toByteArray()))
        val servers = AmneziaImport.parse("смотрите ссылку $link спасибо")
        assertEquals(1, servers.size)
        assertEquals("Дом", servers[0].name)
        assertEquals("203.0.113.11", servers[0].host)
        assertEquals(22, servers[0].port)
        assertEquals("s3cret-ssh", servers[0].password)
        assertTrue(servers[0].privateKey.isEmpty())
        val dumped = servers[0].toString()
        assertFalse(dumped.contains("WIREGUARD"))
    }

    @Test
    fun backupJsonArrayAndString() {
        val array = """{"Servers/serversList":[{"hostName":"vpn.example.com","description":"DE","userName":"root","password":"pw"}]}"""
        val a = AmneziaImport.parse(array)
        assertEquals("DE", a[0].name)
        assertEquals("vpn.example.com", a[0].host)
        assertEquals("pw", a[0].password)
        val asString = """{"Servers/serversList":"[{\"hostName\":\"203.0.113.20\",\"userName\":\"monitor\",\"password\":\"\",\"port\":2222,\"description\":\"NL\"}]"}"""
        val b = AmneziaImport.parse(asString)
        assertEquals("NL", b[0].name)
        assertEquals(2222, b[0].port)
        assertEquals("monitor", b[0].user)
        assertFalse(b[0].hasSshAuth)
    }

    @Test
    fun wireguardConfImportsHostOnly() {
        val conf = """
            # Берлин
            [Interface]
            PrivateKey = SHOULD-NOT-STORE
            [Peer]
            Endpoint = 198.51.100.8:51820
        """.trimIndent()
        val s = AmneziaImport.parse(conf).single()
        assertEquals("Берлин", s.name)
        assertEquals("198.51.100.8", s.host)
        assertEquals(22, s.port)
        assertFalse(s.hasSshAuth)
        assertFalse(s.toString().contains("SHOULD-NOT-STORE"))
    }

    @Test
    fun sshKeyGoesToPrivateKeyField() {
        val json = """{"hostName":"203.0.113.5","userName":"root","password":"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n","description":"k"}"""
        val s = AmneziaImport.parse(json).single()
        assertTrue(s.password.isEmpty())
        assertTrue(s.privateKey.contains("OPENSSH"))
    }

    @Test
    fun mergeUpdatesSameHost() {
        val old = listOf(ImportedServer("id1", "old", "203.0.113.11", 22, "root", "oldpw", ""))
        val incoming = listOf(ImportedServer("", "Дом", "203.0.113.11", 22, "root", "newpw", ""))
        val merged = AmneziaImport.merge(old, incoming) { "new" }
        assertEquals(1, merged.size)
        assertEquals("id1", merged[0].id)
        assertEquals("Дом", merged[0].name)
        assertEquals("newpw", merged[0].password)
    }

    @Test
    fun garbageRejected() {
        try {
            AmneziaImport.parse("привет, это не конфиг")
            throw AssertionError("expected")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("не"))
        }
    }

    @Test
    fun parsersIgnorePrivateKeyLineAndWhitelistCommands() {
        val dump = "awg0\tPRIVATE-SERVER-KEY\tpub\t39743\toff\n" +
            "awg0\tPEERPUB\tpreshared\t198.51.100.9:1000\t10.8.1.2/32\t100\t10\t20\t25\n"
        val peers = ProcParse.wgPeers(dump, nowSec = 120)
        assertEquals(1, peers.size)
        assertEquals("PEERPUB", peers[0].pubkey)
        assertTrue(peers[0].online)
        assertFalse(peers.joinToString { it.pubkey }.contains("PRIVATE"))
        val mem = ProcParse.mem("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
        assertEquals(75.0, mem!!.first, 0.01)
        val ps = ProcParse.dockerPs("amnezia-awg|Up 3 days|amnezia-awg\nnginx|Up|nginx\n")
        assertEquals(listOf("amnezia-awg"), ps.map { it.name })
        assertTrue(ProcParse.allowed("cat /proc/meminfo"))
        assertTrue(ProcParse.allowed("docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'"))
        assertTrue(ProcParse.allowed("docker exec amnezia-awg sh -c 'awg show all dump 2>/dev/null || wg show all dump'"))
        assertTrue(ProcParse.allowed("docker exec amnezia-dns sh -c 'tail -n 400 /var/log/unbound.log'"))
        assertTrue(ProcParse.allowed("sudo -n docker logs --tail 400 amnezia-dns"))
        assertFalse(ProcParse.allowed("docker restart amnezia-awg"))
        assertFalse(ProcParse.allowed("docker exec amnezia-awg sh -c 'rm -rf /'"))
        assertNull(ProcParse.safeName("amnezia-awg;rm"))
    }

    private fun qCompress(data: ByteArray): ByteArray {
        val def = Deflater(8)
        def.setInput(data)
        def.finish()
        val z = ByteArrayOutputStream()
        val buf = ByteArray(256)
        while (!def.finished()) {
            val n = def.deflate(buf)
            if (n > 0) z.write(buf, 0, n)
        }
        def.end()
        val body = z.toByteArray()
        val out = ByteArray(4 + body.size)
        val n = data.size
        out[0] = (n ushr 24).toByte()
        out[1] = (n ushr 16).toByte()
        out[2] = (n ushr 8).toByte()
        out[3] = n.toByte()
        System.arraycopy(body, 0, out, 4, body.size)
        return out
    }
}
