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
)

/**
 * Опрос одного сервера. Команды только из белого списка чтения — те же, что у настольного агента.
 * Конфигурация сервера не меняется.
 */
object SshProbe {
    private const val TIMEOUT_MS = 8_000
    private const val DNS_LINES = 400
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
        val repo = MemoryHostKeys(knownKeys)
        var session: Session? = null
        return try {
            session = connect(server, repo)
            readServer(server, session, tcp, prev, nowMs)
        } catch (e: Exception) {
            bare(server, tcp, nowMs, prev, sshError(e))
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

    private fun connect(server: ImportedServer, repo: MemoryHostKeys): Session {
        JSch.setLogger(QuietLog)
        val jsch = JSch()
        if (server.privateKey.isNotBlank()) {
            jsch.addIdentity("imported", server.privateKey.toByteArray(StandardCharsets.UTF_8), null, null)
        }
        jsch.hostKeyRepository = repo
        val session = jsch.getSession(server.user, server.host, server.port)
        if (server.password.isNotBlank()) session.setPassword(server.password)
        session.setConfig("StrictHostKeyChecking", "yes")
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
        val cpu = ProcParse.cpuJiffies(exec("head -1 /proc/stat"))
        val net = ProcParse.netDev(exec("cat /proc/net/dev"))
        val dt = if (prev == null) 0.0 else (nowMs - prev.tsMs) / 1000.0
        val cpuPct = ProcParse.cpuPercent(prev?.cpu, cpu)
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

    private fun dnsLogCmd(docker: String, name: String) =
        "$docker exec $name sh -c 'tail -n $DNS_LINES /opt/adguardhome/work/data/querylog.json 2>/dev/null || tail -n $DNS_LINES /var/log/unbound/unbound.log 2>/dev/null || tail -n $DNS_LINES /var/log/unbound.log 2>/dev/null'"

    private fun dnsDockerLogs(docker: String, name: String) = "$docker logs --tail $DNS_LINES $name"

    private fun sshError(e: Exception): String {
        val msg = e.message.orEmpty()
        return when {
            msg.contains("Auth fail", true) || msg.contains("authentication", true) ||
                e.javaClass.simpleName.contains("Auth") ->
                "SSH отклонил вход. В клиентском конфиге пароля нет — нужна полная резервная копия."
            msg.contains("HostKey", true) || msg.contains("reject HostKey", true) ->
                "Ключ хоста изменился, подключение остановлено"
            msg.contains("timeout", true) || e.javaClass.simpleName.contains("Timeout") -> "Таймаут SSH"
            msg.contains("белого списка") -> "Внутренняя ошибка: команда не из списка чтения"
            else -> "SSH: ${e.javaClass.simpleName}"
        }
    }

    private object QuietLog : Logger {
        override fun isEnabled(level: Int) = false
        override fun log(level: Int, message: String?) = Unit
    }

    private class MemoryHostKeys(seed: Map<String, String>) : HostKeyRepository {
        val keys: MutableMap<String, String> = seed.toMutableMap()

        override fun check(host: String?, key: ByteArray?): Int {
            if (host == null || key == null) return HostKeyRepository.NOT_INCLUDED
            val now = Base64.getEncoder().encodeToString(key)
            val known = keys[host]
            if (known == null) {
                keys[host] = now
                return HostKeyRepository.OK
            }
            return if (known == now) HostKeyRepository.OK else HostKeyRepository.CHANGED
        }

        override fun add(hostkey: HostKey?, ui: UserInfo?) {
            // Новый ключ уже записан в check(): там приходит сырой байтовый отпечаток.
        }

        override fun remove(host: String?, type: String?) {
            if (host != null) keys.remove(host)
        }

        override fun remove(host: String?, type: String?, key: ByteArray?) {
            if (host != null) keys.remove(host)
        }

        override fun getKnownHostsRepositoryID(): String = "memory"
        override fun getHostKey(): Array<HostKey> = emptyArray()
        override fun getHostKey(host: String?, type: String?): Array<HostKey> = emptyArray()
    }
}
