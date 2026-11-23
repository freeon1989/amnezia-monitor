package org.amnezia.monitor.data

import com.jcraft.jsch.ChannelExec
import com.jcraft.jsch.HostKey
import com.jcraft.jsch.HostKeyRepository
import com.jcraft.jsch.JSch
import com.jcraft.jsch.Logger
import com.jcraft.jsch.Session
import com.jcraft.jsch.UserInfo
import java.io.ByteArrayOutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.charset.StandardCharsets
import java.util.Base64

data class ProbePrev(
    val tsMs: Long = 0,
    val cpu: Pair<Long, Long>? = null,
    val net: Map<String, Pair<Long, Long>> = emptyMap(),
    val peers: Map<String, Pair<Long, Long>> = emptyMap(),
)

data class LocalPoll(
    val summary: ServerSummary,
    val detail: ServerDetail,
    val prev: ProbePrev,
    val hostKey: HostKeyMismatch? = null,
)

/**
 * Опрос одного сервера. Команды только из белого списка чтения — те же, что у настольного агента.
 * Конфигурация сервера не меняется.
 */
object SshProbe {
    private const val TIMEOUT_MS = 8_000
    private const val DNS_LINES = 400
    private const val DNS_UNBOUND_LINES = 1500
    private const val MAX_OUT = 400_000

    fun poll(
        server: ImportedServer,
        prev: ProbePrev?,
        knownKeys: MutableMap<String, String>,
        nowMs: Long = System.currentTimeMillis(),
    ): LocalPoll {
        val tcp = tcpLatency(server.host, server.port, 4_000)
        if (!server.hasSshAuth) {
            return bare(server, tcp, nowMs, prev, if (tcp == null) "SSH-порт недоступен" else
                "Адрес импортирован без пароля SSH. Для CPU, памяти и пиров нужна полная резервная копия Amnezia или ссылка vpn:// с доступом к серверу.")
        }
        if (tcp == null) return bare(server, null, nowMs, prev, "SSH-порт недоступен")
        val repo = BookHostKeys(knownKeys, server.port)
        var session: Session? = null
        return try {
            session = connect(server, repo)
            readServer(server, session, tcp, prev, nowMs)
        } catch (e: Exception) {
            val mm = repo.mismatch
            if (mm != null) {
                val text = "Ключ SSH сервера изменился — опрос остановлен до входа, пароль не отправлялся. " +
                    "Было: " + mm.old.joinToString { "${it.type} ${it.fingerprint}" } +
                    ". Сейчас: ${mm.new.type} ${mm.new.fingerprint}."
                val b = bare(server, tcp, nowMs, prev, text)
                b.copy(summary = b.summary.copy(hostKey = mm), hostKey = mm)
            } else {
                bare(server, tcp, nowMs, prev, sshError(e))
            }
        } finally {
            try {
                session?.disconnect()
            } catch (_: Exception) {
            }
            knownKeys.clear()
            knownKeys.putAll(repo.keys)
        }
    }

    fun tcpLatency(host: String, port: Int, timeoutMs: Int): Double? {
        val t0 = System.nanoTime()
        return try {
            Socket().use { s -> s.connect(InetSocketAddress(host, port), timeoutMs) }
            (System.nanoTime() - t0) / 1_000_000.0
        } catch (_: Exception) {
            null
        }
    }

    /**
     * ICMP-пинг системной утилитой ping (есть на Android без root). Как у панели ПК: средний RTT, мс.
     * null — утилиты нет, ICMP закрыт или адрес странный.
     */
    fun icmpPing(host: String, count: Int = 2): Double? {
        if (!HOST_RE.matches(host)) return null
        return try {
            val p = ProcessBuilder("ping", "-c", count.toString(), "-W", "2", host)
                .redirectErrorStream(true).start()
            val out = StringBuilder()
            val reader = Thread { runCatching { out.append(p.inputStream.bufferedReader().readText()) } }
            reader.start()
            if (!p.waitFor(8, java.util.concurrent.TimeUnit.SECONDS)) { p.destroyForcibly(); return null }
            reader.join(1_000)
            parsePing(out.toString())
        } catch (_: Exception) {
            null
        }
    }

    private val HOST_RE = Regex("^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")

    /** «rtt min/avg/max/mdev = 30.1/32.4/34.7/2.3 ms» → 32.4; иначе среднее по «time=…». */
    fun parsePing(out: String): Double? {
        Regex("= [\\d.]+/([\\d.]+)/").find(out)?.let { return it.groupValues[1].toDoubleOrNull() }
        val times = Regex("time[=<]([\\d.]+)").findAll(out).mapNotNull { it.groupValues[1].toDoubleOrNull() }.toList()
        return if (times.isEmpty()) null else times.average()
    }

    /**
     * Только обмен ключами, без входа: какой ключ хоста сервер показывает сейчас.
     * Нужен для «Доверять новому ключу» — сверить с отпечатком, который видел пользователь.
     */
    fun fetchHostKey(host: String, port: Int, algo: String? = null): ByteArray? {
        JSch.setLogger(QuietLog)
        val jsch = JSch()
        val grab = GrabHostKey()
        jsch.hostKeyRepository = grab
        val session = jsch.getSession("hostkey-probe", host, port)
        session.setConfig("StrictHostKeyChecking", "yes")
        if (algo != null) session.setConfig("server_host_key", algo)
        session.timeout = TIMEOUT_MS
        try {
            session.connect(TIMEOUT_MS)
        } catch (_: Exception) {
            // ожидаемо: GrabHostKey отклоняет ключ, вход не начинается
        } finally {
            try {
                session.disconnect()
            } catch (_: Exception) {
            }
        }
        return grab.seen
    }

    /** Ключ сервера, совпадающий с подтверждённым отпечатком, или null. */
    fun fetchMatchingKey(host: String, port: Int, fingerprint: String): ByteArray? {
        fetchHostKey(host, port)?.let { if (HostKeyBook.fingerprint(it) == fingerprint) return it }
        val algos = (JSch.getConfig("server_host_key") ?: "").split(',').filter { it.isNotBlank() }
        for (a in algos) {
            val k = fetchHostKey(host, port, a) ?: continue
            if (HostKeyBook.fingerprint(k) == fingerprint) return k
        }
        return null
    }

    private fun connect(server: ImportedServer, repo: BookHostKeys): Session {
        JSch.setLogger(QuietLog)
        val jsch = JSch()
        if (server.privateKey.isNotBlank()) {
            jsch.addIdentity("imported", server.privateKey.toByteArray(StandardCharsets.UTF_8), null, null)
        }
        jsch.hostKeyRepository = repo
        val session = jsch.getSession(server.user, server.host, server.port)
        if (server.password.isNotBlank()) session.setPassword(server.password)
        session.setConfig("StrictHostKeyChecking", "yes")
        // Сначала типы ключей, уже известные для этого хоста — без ложной «смены ключа» из-за типа.
        val defaults = (JSch.getConfig("server_host_key") ?: "").split(',').filter { it.isNotBlank() }
        val host = HostKeyBook.entryName(server.host, server.port)
        if (defaults.isNotEmpty()) {
            session.setConfig("server_host_key", HostKeyBook.preferredAlgorithms(repo.keys, host, defaults).joinToString(","))
        }
        session.setConfig("PreferredAuthentications", if (server.privateKey.isNotBlank()) "publickey,password" else "password")
        session.timeout = TIMEOUT_MS
        session.connect(TIMEOUT_MS)
        return session
    }

    private fun readServer(server: ImportedServer, session: Session, tcp: Double, prev: ProbePrev?, nowMs: Long): LocalPoll {
        fun exec(cmd: String): String {
            if (!ProcParse.allowed(cmd)) throw SecurityException("команда вне белого списка")
            return execLimited(session, cmd).second
        }
        val uptime = ProcParse.uptimeSeconds(exec("cat /proc/uptime"))
        val load = ProcParse.load1(exec("cat /proc/loadavg"))
        val nproc = ProcParse.nproc(exec("nproc"))
        val mem = ProcParse.mem(exec("cat /proc/meminfo"))
        val statText = exec(ProcParse.STAT_CMD)
        val cpu = ProcParse.cpuJiffies(statText)
        val net = ProcParse.netDev(exec("cat /proc/net/dev"))
        val dt = if (prev == null) 0.0 else (nowMs - prev.tsMs) / 1000.0
        val cpuPct = ProcParse.cpuFromSamples(statText) ?: ProcParse.cpuPercent(prev?.cpu, cpu)
        val rates = ProcParse.netRates(prev?.net, net, dt)
        var docker = "docker"
        var psResult = execCode(session, dockerPs(docker))
        var dockerError: String? = null
        if (psResult.first != 0) {
            val alt = execCode(session, dockerPs("sudo -n docker"))
            if (alt.first == 0) {
                docker = "sudo -n docker"
                psResult = alt
            } else {
                dockerError = "docker ps не выполнен (нужны права root или группа docker)"
            }
        }
        val ps = psResult.second
        val containers = ProcParse.dockerPs(ps)
        val names = linkedMapOf<String, String>()
        val peers = mutableListOf<Pair<String, WgPeer>>()
        for (c in containers) {
            if (!c.up || (c.type != "awg" && c.type != "wireguard")) continue
            val cname = ProcParse.safeName(c.name) ?: continue
            names.putAll(ProcParse.clientNames(exec(clientsCmd(docker, cname, c.type))))
            for (p in ProcParse.wgPeers(exec(wgCmd(docker, cname)), nowMs / 1000)) {
                peers.add(c.name to p)
            }
        }
        val dnsC = containers.firstOrNull { it.type == "dns" && it.up }
        val dns = if (dnsC == null) {
            DnsInfo(
                "no_container",
                "На сервере нет контейнера amnezia-dns, поэтому сайты не видны.",
                null, emptyList(), emptyList(),
            )
        } else {
            val cname = ProcParse.safeName(dnsC.name)
            val text = if (cname == null) "" else {
                val log = exec(dnsLogCmd(docker, cname))
                if (log.isNotBlank()) log else exec(dnsDockerLogs(docker, cname))
            }
            ProcParse.dnsTop(text, peers.map { it.second })
        }
        val users = peers.map { (container, p) ->
            val was = prev?.peers?.get(p.pubkey)
            val rateDown = if (was != null && dt > 0) (p.tx - was.second).coerceAtLeast(0) / dt else null
            val rateUp = if (was != null && dt > 0) (p.rx - was.first).coerceAtLeast(0) / dt else null
            UserRow(
                client = names[p.pubkey] ?: (p.pubkey.take(8) + "…"),
                pubkey = p.pubkey.take(10) + "…",
                container = container,
                online = p.online,
                handshakeAge = p.handshakeAge,
                endpoint = p.endpoint?.substringBeforeLast(':'),
                rateDown = rateDown,
                rateUp = rateUp,
                spark = emptyList(),
                down24h = 0.0,
                up24h = 0.0,
                downTotal = p.tx.toDouble(),
                upTotal = p.rx.toDouble(),
            )
        }.sortedWith(compareBy<UserRow> { !it.online }.thenByDescending { (it.downTotal ?: 0.0) + (it.upTotal ?: 0.0) })
        val online = users.count { it.online }
        val age = if (prev == null) 0.0 else dt
        val summary = ServerSummary(
            name = server.name,
            host = server.host,
            status = "up",
            ageSec = age,
            tcpMs = tcp,
            cpu = cpuPct,
            mem = mem?.first,
            peersOnline = online,
            peersTotal = users.size,
            rxBps = rates?.first,
            txBps = rates?.second,
            error = dockerError,
            localId = server.id,
        )
        val detail = ServerDetail(
            name = server.name,
            host = server.host,
            current = Current(
                status = "up",
                error = dockerError,
                ageSec = age,
                tcpMs = tcp,
                uptimeSec = uptime,
                load1 = load,
                nproc = nproc,
                cpu = cpuPct,
                mem = mem?.first,
                memUsed = mem?.second,
                memTotal = mem?.third,
                rxBps = rates?.first,
                txBps = rates?.second,
            ),
            containers = containers,
            series = null,
            users = users,
            down24h = null,
            up24h = null,
            dns = dns,
            events = emptyList(),
        )
        return LocalPoll(
            summary, detail,
            ProbePrev(
                tsMs = nowMs,
                cpu = cpu,
                net = net,
                peers = peers.associate { it.second.pubkey to (it.second.rx to it.second.tx) },
            ),
        )
    }

    private fun bare(server: ImportedServer, tcp: Double?, nowMs: Long, prev: ProbePrev?, error: String): LocalPoll {
        val status = if (tcp != null && server.hasSshAuth) "down" else if (tcp != null) "nodata" else "down"
        val summary = ServerSummary(
            name = server.name, host = server.host, status = status,
            ageSec = if (prev == null) null else (nowMs - prev.tsMs) / 1000.0,
            tcpMs = tcp, cpu = null, mem = null, peersOnline = null, peersTotal = null,
            rxBps = null, txBps = null, error = error, localId = server.id,
        )
        val detail = ServerDetail(
            name = server.name, host = server.host,
            current = Current(status = status, error = error, tcpMs = tcp, ageSec = summary.ageSec),
            containers = emptyList(), series = null, users = emptyList(),
            down24h = null, up24h = null,
            dns = DnsInfo("nodata", error, null, emptyList(), emptyList()),
            events = emptyList(),
        )
        return LocalPoll(summary, detail, prev ?: ProbePrev(tsMs = nowMs))
    }

    private fun execCode(session: Session, cmd: String): Pair<Int, String> {
        if (!ProcParse.allowed(cmd)) throw SecurityException("команда вне белого списка")
        return execLimited(session, cmd)
    }

    private fun execLimited(session: Session, cmd: String): Pair<Int, String> {
        val channel = session.openChannel("exec") as ChannelExec
        channel.setCommand(cmd)
        channel.setInputStream(null)
        val err = ByteArrayOutputStream()
        channel.setErrStream(err)
        val input = channel.inputStream
        channel.connect(TIMEOUT_MS)
        val out = ByteArrayOutputStream()
        val buf = ByteArray(4096)
        val start = System.currentTimeMillis()
        while (true) {
            while (input.available() > 0) {
                val n = input.read(buf)
                if (n < 0) break
                out.write(buf, 0, n)
                if (out.size() > MAX_OUT) break
            }
            if (channel.isClosed || out.size() > MAX_OUT || System.currentTimeMillis() - start > TIMEOUT_MS) break
            Thread.sleep(30)
        }
        while (input.available() > 0 && out.size() <= MAX_OUT) {
            val n = input.read(buf)
            if (n < 0) break
            out.write(buf, 0, n)
        }
        val rc = channel.exitStatus
        channel.disconnect()
        val text = (out.toString(StandardCharsets.UTF_8.name()) + err.toString(StandardCharsets.UTF_8.name()))
        val clipped = if (text.length > MAX_OUT) text.substring(0, MAX_OUT) else text
        return (if (rc < 0) 1 else rc) to clipped
    }

    private fun dockerPs(docker: String) = "$docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'"

    private fun wgCmd(docker: String, name: String) =
        "$docker exec $name sh -c 'awg show all dump 2>/dev/null || wg show all dump'"

    private fun clientsCmd(docker: String, name: String, type: String) =
        "$docker exec $name sh -c 'cat /opt/amnezia/$type/clientsTable 2>/dev/null || cat /opt/amnezia/*/clientsTable 2>/dev/null'"

    /** Журнал Amnezia Unbound (как у настольной программы) и два последних архива; старые пути — запасные. */
    private fun dnsLogCmd(docker: String, name: String) =
        "$docker exec $name sh -c 'tail -n $DNS_UNBOUND_LINES /opt/unbound/etc/unbound/unbound.log 2>/dev/null; " +
            "for f in \$(ls -1 /opt/unbound/etc/unbound/log-archive/unbound-*.log 2>/dev/null | sort | tail -n 2); " +
            "do tail -n $DNS_UNBOUND_LINES \"\$f\"; done; " +
            "tail -n $DNS_LINES /opt/adguardhome/work/data/querylog.json 2>/dev/null || tail -n $DNS_LINES /var/log/unbound/unbound.log 2>/dev/null || tail -n $DNS_LINES /var/log/unbound.log 2>/dev/null'"

    private fun dnsDockerLogs(docker: String, name: String) = "$docker logs --tail $DNS_LINES $name"

    private fun sshError(e: Exception): String {
        val msg = e.message.orEmpty()
        return when {
            msg.contains("Auth fail", true) || msg.contains("authentication", true) ||
                e.javaClass.simpleName.contains("Auth") ->
                "SSH отклонил вход. В клиентском конфиге пароля нет — нужна полная резервная копия."
            msg.contains("HostKey", true) || msg.contains("reject HostKey", true) ->
                "Ключ хоста не принят, подключение остановлено"
            msg.contains("timeout", true) || e.javaClass.simpleName.contains("Timeout") -> "Таймаут SSH"
            msg.contains("белого списка") -> "Внутренняя ошибка: команда не из списка чтения"
            else -> "SSH: ${e.javaClass.simpleName}"
        }
    }

    private object QuietLog : Logger {
        override fun isEnabled(level: Int) = false
        override fun log(level: Int, message: String?) = Unit
    }

    private class BookHostKeys(seed: Map<String, String>, private val port: Int) : HostKeyRepository {
        val keys: MutableMap<String, String> = seed.toMutableMap()
        var mismatch: HostKeyMismatch? = null

        override fun check(host: String?, key: ByteArray?): Int {
            if (host == null || key == null) return HostKeyRepository.NOT_INCLUDED
            return when (HostKeyBook.check(keys, host, key)) {
                HostKeyBook.Result.NEW -> {
                    HostKeyBook.add(keys, host, key)   // первый вход (TOFU)
                    HostKeyRepository.OK
                }
                HostKeyBook.Result.OK -> HostKeyRepository.OK
                else -> {
                    mismatch = HostKeyBook.mismatch(keys, host, port, key)
                    HostKeyRepository.CHANGED
                }
            }
        }

        override fun add(hostkey: HostKey?, ui: UserInfo?) {
            // Новый ключ записывается в check(); замена — только через «Доверять новому ключу».
        }

        override fun remove(host: String?, type: String?) = Unit
        override fun remove(host: String?, type: String?, key: ByteArray?) = Unit
        override fun getKnownHostsRepositoryID(): String = "app"
        override fun getHostKey(): Array<HostKey> = emptyArray()
        override fun getHostKey(host: String?, type: String?): Array<HostKey> {
            if (host == null) return emptyArray()
            return HostKeyBook.known(keys, host)
                .filter { type == null || it.key == type }
                .mapNotNull { runCatching { HostKey(host, it.value) }.getOrNull() }
                .toTypedArray()
        }
    }

    /** Запоминает ключ и отклоняет его: соединение рвётся до входа. */
    private class GrabHostKey : HostKeyRepository {
        var seen: ByteArray? = null
        override fun check(host: String?, key: ByteArray?): Int {
            seen = key
            return HostKeyRepository.NOT_INCLUDED
        }
        override fun add(hostkey: HostKey?, ui: UserInfo?) = Unit
        override fun remove(host: String?, type: String?) = Unit
        override fun remove(host: String?, type: String?, key: ByteArray?) = Unit
        override fun getKnownHostsRepositoryID(): String = "probe"
        override fun getHostKey(): Array<HostKey> = emptyArray()
        override fun getHostKey(host: String?, type: String?): Array<HostKey> = emptyArray()
    }
}
