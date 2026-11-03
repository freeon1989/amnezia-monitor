package org.amnezia.monitor.data

/** Разбор вывода тех же read-only команд, что и у настольного агента. Приватный ключ интерфейса не читается. */
object ProcParse {
    private val NAME = Regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    private val ALLOWED = Regex(
        "^(cat /proc/|head -1 /proc/stat$|nproc$|(sudo -n )?docker (ps|exec \\S+ sh -c '(awg|wg|cat|tail) )" +
            "|(sudo -n )?docker logs --tail \\d+ [\\w.-]+$)",
    )
    private val UNBOUND = Regex("""(?:^\[(\d+)\].*?)?info: ([0-9a-fA-F.:]+) (\S+?)\.? (\w+) IN\b""")
    private val SKIP_SUFFIX = listOf(".in-addr.arpa", ".ip6.arpa", ".local", ".lan", ".internal")

    /** Как re.match в агенте: совпадение с начала строки, не обязательно до конца (кроме веток с `$`). */
    fun allowed(cmd: String): Boolean = ALLOWED.find(cmd)?.range?.first == 0

    fun safeName(name: String): String? = name.takeIf { NAME.matches(it) }

    fun uptimeSeconds(text: String): Double? = text.trim().split(" ").firstOrNull()?.toDoubleOrNull()

    fun load1(text: String): Double? = text.trim().split(" ").firstOrNull()?.toDoubleOrNull()

    fun nproc(text: String): Int? = text.trim().toIntOrNull()

    fun mem(text: String): Triple<Double, Double, Double>? {
        val vals = mutableMapOf<String, Long>()
        for (line in text.lineSequence()) {
            val m = Regex("""(\w+):\s+(\d+)""").find(line) ?: continue
            vals[m.groupValues[1]] = m.groupValues[2].toLong() * 1024
        }
        val total = vals["MemTotal"] ?: return null
        val avail = vals["MemAvailable"] ?: vals["MemFree"] ?: return null
        if (total <= 0) return null
        val used = total - avail
        val pct = (used * 1000.0 / total).toLong() / 10.0
        return Triple(pct, used.toDouble(), total.toDouble())
    }

    fun cpuJiffies(text: String): Pair<Long, Long>? {
        val line = text.lineSequence().firstOrNull { it.startsWith("cpu ") } ?: return null
        val nums = line.split(Regex("\\s+")).drop(1).mapNotNull { it.toLongOrNull() }
        if (nums.size < 4) return null
        val idle = nums[3] + (nums.getOrNull(4) ?: 0L)
        val total = nums.take(8).sum()
        return (total - idle) to total
    }

    fun cpuPercent(prev: Pair<Long, Long>?, cur: Pair<Long, Long>?): Double? {
        if (prev == null || cur == null) return null
        val dt = cur.second - prev.second
        if (dt <= 0) return null
        return ((cur.first - prev.first).toDouble() * 100.0 / dt).coerceIn(0.0, 100.0).let { (it * 10).toLong() / 10.0 }
    }

    fun netDev(text: String): Map<String, Pair<Long, Long>> {
        val res = linkedMapOf<String, Pair<Long, Long>>()
        for (line in text.lineSequence()) {
            val idx = line.indexOf(':')
            if (idx < 0) continue
            val iface = line.substring(0, idx).trim()
            if (iface == "lo" || iface.startsWith("veth")) continue
            val f = line.substring(idx + 1).trim().split(Regex("\\s+"))
            if (f.size >= 9) {
                val rx = f[0].toLongOrNull() ?: continue
                val tx = f[8].toLongOrNull() ?: continue
                res[iface] = rx to tx
            }
        }
        return res
    }

    fun netRates(prev: Map<String, Pair<Long, Long>>?, cur: Map<String, Pair<Long, Long>>, dtSec: Double): Pair<Double, Double>? {
        if (prev == null || dtSec <= 0) return null
        var rx = 0L
        var tx = 0L
        for ((iface, now) in cur) {
            val was = prev[iface] ?: continue
            rx += (now.first - was.first).coerceAtLeast(0)
            tx += (now.second - was.second).coerceAtLeast(0)
        }
        return rx / dtSec to tx / dtSec
    }

    fun dockerPs(text: String): List<ContainerRow> {
        val out = mutableListOf<ContainerRow>()
        for (line in text.lineSequence()) {
            val parts = line.trim().split("|")
            if (parts.size < 2 || parts[0].isBlank()) continue
            val name = parts[0]
            val status = parts[1]
            val image = parts.getOrElse(2) { "" }
            if (!name.startsWith("amnezia") && "amnezia" !in image) continue
            if (safeName(name) == null) continue
            out.add(ContainerRow(name = name, type = containerType(name), up = status.lowercase().startsWith("up"), status = status))
        }
        return out
    }

    fun containerType(name: String): String {
        val n = name.lowercase()
        for (t in listOf("awg", "wireguard", "xray", "openvpn", "cloak", "shadowsocks", "ipsec", "dns", "sftp", "tor")) {
            if (t in n) return t
        }
        return "other"
    }

    fun wgPeers(text: String, nowSec: Long, onlineSec: Int = 180): List<WgPeer> {
        val peers = mutableListOf<WgPeer>()
        for (line in text.lineSequence()) {
            val f = line.trimEnd('\n').split('\t')
            if (f.size != 9) continue
            val hs = f[5].toLongOrNull() ?: continue
            val rx = f[6].toLongOrNull() ?: continue
            val tx = f[7].toLongOrNull() ?: continue
            val age = if (hs > 0) (nowSec - hs).toDouble() else null
            peers.add(
                WgPeer(
                    pubkey = f[1],
                    endpoint = f[3].takeUnless { it.isBlank() || it == "(none)" },
                    allowedIps = f[4],
                    handshake = hs,
                    handshakeAge = age,
                    rx = rx,
                    tx = tx,
                    online = age != null && age < onlineSec,
                ),
            )
        }
        return peers
    }

    fun clientNames(text: String): Map<String, String> {
        val el = runCatching {
            kotlinx.serialization.json.Json.parseToJsonElement(text)
        }.getOrNull() ?: return emptyMap()
        val arr = el as? kotlinx.serialization.json.JsonArray ?: return emptyMap()
        val res = linkedMapOf<String, String>()
        for (item in arr) {
            val o = item as? kotlinx.serialization.json.JsonObject ?: continue
            val id = (o["clientId"] as? kotlinx.serialization.json.JsonPrimitive)?.content ?: continue
            val user = o["userData"] as? kotlinx.serialization.json.JsonObject
            val name = (user?.get("clientName") as? kotlinx.serialization.json.JsonPrimitive)?.content
            if (!name.isNullOrBlank()) res[id] = name
        }
        return res
    }

    fun dnsTop(text: String, peers: List<WgPeer>): DnsInfo {
        val ipToClient = linkedMapOf<String, String>()
        for (p in peers) {
            val client = p.pubkey.take(8)
            for (a in p.allowedIps.split(',')) {
                val ip = a.trim().substringBefore('/')
                if (ip.isNotBlank()) ipToClient[ip] = client
            }
        }
        val counts = linkedMapOf<String, Int>()
        val per = linkedMapOf<String, MutableMap<String, Int>>()
        var queries = 0
        for (line in text.lineSequence()) {
            val row = dnsLine(line.trim()) ?: continue
            queries++
            val site = rootDomain(row.second)
            counts[site] = (counts[site] ?: 0) + 1
            val client = ipToClient[row.first] ?: row.first
            val bucket = per.getOrPut(client) { linkedMapOf() }
            bucket[site] = (bucket[site] ?: 0) + 1
        }
        if (queries == 0) {
            return DnsInfo("no_log", "Журнал DNS на сервере пуст или не включён. Агент его не включает.", null, emptyList(), emptyList())
        }
        val top = counts.entries.sortedByDescending { it.value }.take(12).map { DnsSite(it.key, it.value) }
        val users = per.map { (client, sites) ->
            val items = sites.entries.sortedByDescending { it.value }.take(8).map { DnsSite(it.key, it.value) }
            DnsUser(client, sites.values.sum(), items)
        }.sortedByDescending { it.total }
        return DnsInfo("ok", null, queries, top, users)
    }

    private fun dnsLine(line: String): Pair<String, String>? {
        if (line.isEmpty()) return null
        if (line.startsWith("{")) {
            val o = runCatching {
                kotlinx.serialization.json.Json.parseToJsonElement(line) as? kotlinx.serialization.json.JsonObject
            }.getOrNull() ?: return null
            val qh = (o["QH"] as? kotlinx.serialization.json.JsonPrimitive)?.content ?: return null
            val ip = (o["IP"] as? kotlinx.serialization.json.JsonPrimitive)?.content ?: return null
            val dom = qh.trim('.').lowercase()
            if (!keepDomain(dom)) return null
            return ip to dom
        }
        val m = UNBOUND.find(line) ?: return null
        val dom = m.groupValues[3].trim('.').lowercase()
        if (!keepDomain(dom)) return null
        return m.groupValues[2] to dom
    }

    private fun keepDomain(dom: String): Boolean {
        if (dom.isBlank() || "." !in dom || dom == "localhost") return false
        return SKIP_SUFFIX.none { dom.endsWith(it) }
    }

    private fun rootDomain(domain: String): String {
        val parts = domain.trim('.').lowercase().split('.')
        val sld = setOf("co", "com", "org", "net", "gov", "edu", "ac")
        if (parts.size >= 3 && parts.last().length == 2 && parts[parts.size - 2] in sld) {
            return parts.takeLast(3).joinToString(".")
        }
        return parts.takeLast(2).joinToString(".")
    }
}

data class WgPeer(
    val pubkey: String,
    val endpoint: String?,
    val allowedIps: String,
    val handshake: Long,
    val handshakeAge: Double?,
    val rx: Long,
    val tx: Long,
    val online: Boolean,
)
