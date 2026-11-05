import Darwin
import Foundation

struct ProbePrev: Sendable {
    var tsMs: Int64 = 0
    var cpuBusy: Int64?
    var cpuTotal: Int64?
    var net: [String: NetSample] = [:]
    var peers: [String: PeerSample] = [:]
}

struct NetSample: Sendable {
    var rx: Int64
    var tx: Int64
}

struct PeerSample: Sendable {
    var rx: Int64
    var tx: Int64
}

struct LocalPoll: Sendable {
    var summary: ServerSummary
    var detail: ServerDetail
    var prev: ProbePrev
}

/// Опрос одного сервера через системный ssh. Команды только из белого списка чтения.
/// Конфигурация сервера не меняется, ключи VPN не читаются и не сохраняются.
enum SshProbe {
    private static let timeout: TimeInterval = 8
    private static let maxOut = 400_000

    static func poll(server: ImportedServer, prev: ProbePrev?, knownHosts: URL, nowMs: Int64 = Int64(Date().timeIntervalSince1970 * 1000)) -> LocalPoll {
        let tcp = tcpLatency(host: server.host, port: server.port, timeoutMs: 4_000)
        if !server.hasSshAuth {
            let message = tcp == nil
                ? "SSH-порт недоступен"
                : "Адрес импортирован без пароля SSH. Для CPU, памяти и пиров нужна полная резервная копия Amnezia или ссылка vpn:// с доступом к серверу."
            return bare(server, tcp: tcp, nowMs: nowMs, prev: prev, error: message)
        }
        if tcp == nil {
            return bare(server, tcp: nil, nowMs: nowMs, prev: prev, error: "SSH-порт недоступен")
        }
        if !validEndpoint(server) {
            return bare(server, tcp: tcp, nowMs: nowMs, prev: prev, error: "Некорректный адрес или пользователь SSH")
        }
        if !FileManager.default.isExecutableFile(atPath: "/usr/bin/ssh") {
            return bare(server, tcp: tcp, nowMs: nowMs, prev: prev, error: "На этом Mac нет /usr/bin/ssh")
        }
        do {
            return try readServer(server, tcp: tcp ?? 0, prev: prev, knownHosts: knownHosts, nowMs: nowMs)
        } catch let error as ProbeError {
            return bare(server, tcp: tcp, nowMs: nowMs, prev: prev, error: error.message)
        } catch {
            return bare(server, tcp: tcp, nowMs: nowMs, prev: prev, error: "SSH: не удалось подключиться")
        }
    }

    static func tcpLatency(host: String, port: Int, timeoutMs: Int) -> Double? {
        guard port > 0, port < 65536, !host.isEmpty, host.count < 254 else { return nil }
        var hints = addrinfo()
        hints.ai_family = AF_UNSPEC
        hints.ai_socktype = SOCK_STREAM
        var info: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, String(port), &hints, &info) == 0, let first = info else { return nil }
        defer { freeaddrinfo(first) }
        var cursor: UnsafeMutablePointer<addrinfo>? = first
        while let ai = cursor {
            let fd = socket(ai.pointee.ai_family, SOCK_STREAM, 0)
            if fd >= 0 {
                let flags = fcntl(fd, F_GETFL)
                if flags != -1 {
                    _ = fcntl(fd, F_SETFL, flags | Int32(O_NONBLOCK))
                }
                let start = DispatchTime.now().uptimeNanoseconds
                let connected = Darwin.connect(fd, ai.pointee.ai_addr, ai.pointee.ai_addrlen)
                if connected == 0 {
                    close(fd)
                    return Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000
                }
                if errno == EINPROGRESS {
                    var pfd = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
                    let waited = poll(&pfd, nfds_t(1), Int32(timeoutMs))
                    if waited > 0 {
                        var soerr: Int32 = 0
                        var len = socklen_t(MemoryLayout<Int32>.size)
                        let ok: Int32 = withUnsafeMutablePointer(to: &soerr) { ptr in
                            getsockopt(fd, SOL_SOCKET, SO_ERROR, UnsafeMutableRawPointer(ptr), &len)
                        }
                        if ok == 0 && soerr == 0 {
                            let ms = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000
                            close(fd)
                            return ms
                        }
                    }
                }
                close(fd)
            }
            cursor = ai.pointee.ai_next
        }
        return nil
    }

    static func forget(host: String, port: Int, file: URL) {
        guard let text = try? String(contentsOf: file, encoding: .utf8) else { return }
        let markers: Set<String> = [host, "[\(host)]", "[\(host)]:\(port)", "\(host):\(port)"]
        let kept = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init).filter { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { return true }
            let token = trimmed.split(whereSeparator: \.isWhitespace).first.map(String.init) ?? ""
            return !markers.contains(token)
        }
        try? kept.joined(separator: "\n").write(to: file, atomically: true, encoding: .utf8)
    }

    private static func readServer(
        _ server: ImportedServer,
        tcp: Double,
        prev: ProbePrev?,
        knownHosts: URL,
        nowMs: Int64
    ) throws -> LocalPoll {
        let session = try SshSession(server: server, knownHosts: knownHosts)
        defer { session.close() }
        func exec(_ cmd: String) throws -> String {
            if !ProcParse.allowed(cmd) {
                throw ProbeError("Внутренняя ошибка: команда не из списка чтения")
            }
            let result = session.exec(cmd, timeout: timeout)
            return result.text
        }
        func execCode(_ cmd: String) throws -> (Int32, String) {
            if !ProcParse.allowed(cmd) {
                throw ProbeError("Внутренняя ошибка: команда не из списка чтения")
            }
            let result = session.exec(cmd, timeout: timeout)
            return (result.status, result.text)
        }
        let uptime = ProcParse.uptimeSeconds(try exec("cat /proc/uptime"))
        let load = ProcParse.load1(try exec("cat /proc/loadavg"))
        let nproc = ProcParse.nproc(try exec("nproc"))
        let mem = ProcParse.mem(try exec("cat /proc/meminfo"))
        let cpu = ProcParse.cpuJiffies(try exec("head -1 /proc/stat"))
        let net = ProcParse.netDev(try exec("cat /proc/net/dev"))
        let dt = prev == nil ? 0 : Double(nowMs - (prev?.tsMs ?? nowMs)) / 1000
        let cpuPct = ProcParse.cpuPercent(
            prev: cpuPair(prev),
            cur: cpu
        )
        let rates = ProcParse.netRates(prev: netMap(prev), cur: net, dtSec: dt)
        var docker = "docker"
        var psResult = try execCode(ProcParse.dockerPsCommand(docker))
        var dockerError: String?
        if psResult.0 != 0 {
            let alt = try execCode(ProcParse.dockerPsCommand("sudo -n docker"))
            if alt.0 == 0 {
                docker = "sudo -n docker"
                psResult = alt
            } else {
                dockerError = "docker ps не выполнен (нужны права root или группа docker)"
            }
        }
        let containers = ProcParse.dockerPs(psResult.1)
        var names: [String: String] = [:]
        var peers: [(String, WgPeer)] = []
        for c in containers where c.up && (c.type == "awg" || c.type == "wireguard") {
            guard let cname = ProcParse.safeName(c.name), let type = c.type else { continue }
            let table = try exec(ProcParse.clientsCommand(docker: docker, name: cname, type: type))
            for (k, v) in ProcParse.clientNames(table) { names[k] = v }
            let dump = try exec(ProcParse.wgCommand(docker: docker, name: cname))
            for p in ProcParse.wgPeers(dump, nowSec: nowMs / 1000) {
                peers.append((c.name, p))
            }
        }
        let dns: DnsInfo
        if let dnsC = containers.first(where: { $0.type == "dns" && $0.up }), let cname = ProcParse.safeName(dnsC.name) {
            var text = try exec(ProcParse.dnsLogCommand(docker: docker, name: cname))
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                text = try exec(ProcParse.dnsDockerLogsCommand(docker: docker, name: cname))
            }
            dns = ProcParse.dnsTop(text, peers: peers.map(\.1))
        } else {
            dns = DnsInfo(
                status: "no_container",
                note: "На сервере нет контейнера amnezia-dns, поэтому сайты не видны.",
                queries: nil,
                top: [],
                perUser: []
            )
        }
        let users = peers.map { container, p -> UserRow in
            let was = prev?.peers[p.pubkey]
            let rateDown: Double? = (was != nil && dt > 0) ? Double(max(Int64(0), p.tx - (was?.tx ?? 0))) / dt : nil
            let rateUp: Double? = (was != nil && dt > 0) ? Double(max(Int64(0), p.rx - (was?.rx ?? 0))) / dt : nil
            return UserRow(
                client: names[p.pubkey] ?? (String(p.pubkey.prefix(8)) + "…"),
                pubkey: String(p.pubkey.prefix(10)) + "…",
                container: container,
                online: p.online,
                handshakeAge: p.handshakeAge,
                endpoint: endpointHost(p.endpoint),
                rateDown: rateDown,
                rateUp: rateUp,
                spark: [],
                down24h: 0,
                up24h: 0,
                downTotal: Double(p.tx),
                upTotal: Double(p.rx)
            )
        }.sorted { a, b in
            if a.online != b.online { return a.online && !b.online }
            let at = (a.downTotal ?? 0) + (a.upTotal ?? 0)
            let bt = (b.downTotal ?? 0) + (b.upTotal ?? 0)
            return at > bt
        }
        let online = users.filter(\.online).count
        let age = prev == nil ? 0 : dt
        let summary = ServerSummary(
            name: server.name,
            host: server.host,
            status: "up",
            ageSec: age,
            tcpMs: tcp,
            cpu: cpuPct,
            mem: mem?.0,
            peersOnline: online,
            peersTotal: users.count,
            rxBps: rates?.0,
            txBps: rates?.1,
            error: dockerError,
            localId: server.id
        )
        let detail = ServerDetail(
            name: server.name,
            host: server.host,
            current: Current(
                status: "up",
                error: dockerError,
                ageSec: age,
                tcpMs: tcp,
                uptimeSec: uptime,
                load1: load,
                nproc: nproc,
                cpu: cpuPct,
                mem: mem?.0,
                memUsed: mem?.1,
                memTotal: mem?.2,
                rxBps: rates?.0,
                txBps: rates?.1
            ),
            containers: containers,
            series: nil,
            users: users,
            down24h: nil,
            up24h: nil,
            dns: dns,
            events: []
        )
        var next = ProbePrev(tsMs: nowMs)
        if let cpu = cpu {
            next.cpuBusy = cpu.0
            next.cpuTotal = cpu.1
        }
        next.net = Dictionary(uniqueKeysWithValues: net.map { ($0.key, NetSample(rx: $0.value.0, tx: $0.value.1)) })
        for pair in peers {
            next.peers[pair.1.pubkey] = PeerSample(rx: pair.1.rx, tx: pair.1.tx)
        }
        return LocalPoll(summary: summary, detail: detail, prev: next)
    }

    private static func cpuPair(_ prev: ProbePrev?) -> (Int64, Int64)? {
        guard let busy = prev?.cpuBusy, let total = prev?.cpuTotal else { return nil }
        return (busy, total)
    }

    private static func netMap(_ prev: ProbePrev?) -> [String: (Int64, Int64)]? {
        guard let prev = prev else { return nil }
        return prev.net.mapValues { ($0.rx, $0.tx) }
    }

    private static func endpointHost(_ endpoint: String?) -> String? {
        guard let endpoint = endpoint else { return nil }
        guard let idx = endpoint.lastIndex(of: ":") else { return endpoint }
        return String(endpoint[..<idx])
    }

    private static func validEndpoint(_ server: ImportedServer) -> Bool {
        let hostOK = server.host.range(of: "^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$", options: .regularExpression) != nil
        let userOK = server.user.range(of: "^[A-Za-z0-9._-]{1,32}$", options: .regularExpression) != nil
        return hostOK && userOK && (1...65535).contains(server.port) && !server.host.hasPrefix("-") && !server.user.hasPrefix("-")
    }

    private static func bare(_ server: ImportedServer, tcp: Double?, nowMs: Int64, prev: ProbePrev?, error: String) -> LocalPoll {
        let status = (tcp != nil && server.hasSshAuth) ? "down" : (tcp != nil ? "nodata" : "down")
        let age = prev.map { Double(nowMs - $0.tsMs) / 1000 }
        let summary = ServerSummary(
            name: server.name, host: server.host, status: status, ageSec: age, tcpMs: tcp,
            cpu: nil, mem: nil, peersOnline: nil, peersTotal: nil, rxBps: nil, txBps: nil,
            error: error, localId: server.id
        )
        let detail = ServerDetail(
            name: server.name,
            host: server.host,
            current: Current(status: status, error: error, ageSec: age, tcpMs: tcp),
            containers: [],
            series: nil,
            users: [],
            down24h: nil,
            up24h: nil,
            dns: DnsInfo(status: "nodata", note: error, queries: nil, top: [], perUser: []),
            events: []
        )
        return LocalPoll(summary: summary, detail: detail, prev: prev ?? ProbePrev(tsMs: nowMs))
    }
}

private struct ProbeError: Error {
    var message: String
    init(_ message: String) { self.message = message }
}

private struct CommandOutput {
    var status: Int32
    var text: String
}

private final class LockedData: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    func append(_ chunk: Data, limit: Int) {
        lock.lock()
        defer { lock.unlock() }
        if data.count < limit { data.append(chunk.prefix(limit - data.count)) }
    }
    func text() -> String {
        lock.lock()
        defer { lock.unlock() }
        return String(data: data, encoding: .utf8) ?? ""
    }
}

/// Одно соединение ssh с ControlMaster. Пароль уходит в askpass-файл с правами 0600 и сразу удаляется.
private final class SshSession {
    private let proc = Process()
    private let output = LockedData()
    private let group = DispatchGroup()
    private let sock: String
    private let dir: String
    private let target: String
    private let port: Int
    private var closed = false
    private var started = false
    private var reads: [FileHandle] = []

    init(server: ImportedServer, knownHosts: URL) throws {
        let stamp = String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(10))
        dir = "/tmp/amz\(stamp)"
        sock = dir + "/cm"
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir)
        let hostArg = server.host.contains(":") ? "[\(server.host)]" : server.host
        target = "\(server.user)@\(hostArg)"
        port = server.port

        var args = [
            "-p", String(server.port),
            "-o", "ControlMaster=yes",
            "-o", "ControlPath=\(sock)",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=\(knownHosts.path)",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=8",
            "-o", "ConnectionAttempts=1",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "IdentitiesOnly=yes",
            "-o", "LogLevel=ERROR",
            "-o", "UpdateHostKeys=no",
        ]
        var env = ProcessInfo.processInfo.environment
        let password = server.password
        let key = server.privateKey
        if !key.isEmpty {
            let keyURL = URL(fileURLWithPath: dir).appendingPathComponent("id")
            try Data(key.utf8).write(to: keyURL, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: keyURL.path)
            args += ["-i", keyURL.path, "-o", "PubkeyAuthentication=yes"]
        } else {
            args += ["-o", "PubkeyAuthentication=no", "-o", "IdentityFile=/dev/null"]
        }
        if !password.isEmpty {
            let secret = URL(fileURLWithPath: dir).appendingPathComponent("secret")
            let ask = URL(fileURLWithPath: dir).appendingPathComponent("askpass")
            try Data((password + "\n").utf8).write(to: secret, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: secret.path)
            let script = "#!/bin/sh\ncat '\(secret.path)'\n"
            try Data(script.utf8).write(to: ask, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: ask.path)
            env["SSH_ASKPASS"] = ask.path
            env["SSH_ASKPASS_REQUIRE"] = "force"
            if env["DISPLAY"] == nil { env["DISPLAY"] = ":0" }
            args += ["-o", "PasswordAuthentication=yes", "-o", "PreferredAuthentications=\(key.isEmpty ? "password" : "publickey,password")"]
        } else {
            args += ["-o", "PasswordAuthentication=no", "-o", "PreferredAuthentications=publickey", "-o", "BatchMode=yes"]
        }
        args += ["-N", target]

        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        proc.arguments = args
        proc.environment = env
        proc.standardOutput = outPipe
        proc.standardError = errPipe
        proc.standardInput = FileHandle.nullDevice
        startReader(outPipe.fileHandleForReading)
        startReader(errPipe.fileHandleForReading)
        try proc.run()
        started = true
        if !waitForSocket(timeout: 8) {
            let message = SshSession.mapError(output.text(), password: password, key: key)
            close()
            throw ProbeError(message)
        }
    }

    func exec(_ cmd: String, timeout: TimeInterval) -> CommandOutput {
        let child = Process()
        child.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        child.arguments = [
            "-p", String(port),
            "-o", "ControlPath=\(sock)",
            "-o", "ControlMaster=no",
            "-o", "BatchMode=yes",
            "-o", "LogLevel=ERROR",
            target,
            cmd,
        ]
        let outPipe = Pipe()
        let errPipe = Pipe()
        child.standardOutput = outPipe
        child.standardError = errPipe
        child.standardInput = FileHandle.nullDevice
        let acc = LockedData()
        let readers = DispatchGroup()
        for handle in [outPipe.fileHandleForReading, errPipe.fileHandleForReading] {
            readers.enter()
            DispatchQueue.global(qos: .userInitiated).async {
                defer { readers.leave() }
                while true {
                    let chunk = handle.availableData
                    if chunk.isEmpty { break }
                    acc.append(chunk, limit: 400_000)
                }
            }
        }
        do { try child.run() } catch {
            return CommandOutput(status: 1, text: "")
        }
        let deadline = Date().addingTimeInterval(timeout)
        while child.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.03)
        }
        if child.isRunning {
            child.terminate()
            let killAt = Date().addingTimeInterval(1)
            while child.isRunning && Date() < killAt { Thread.sleep(forTimeInterval: 0.03) }
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
        }
        child.waitUntilExit()
        _ = readers.wait(timeout: .now() + 2)
        let status: Int32 = child.isRunning ? 124 : child.terminationStatus
        return CommandOutput(status: status == 0 || !child.isRunning ? child.terminationStatus : 124, text: acc.text())
    }

    deinit { close() }

    func close() {
        if closed { return }
        closed = true
        if started {
            if proc.isRunning { proc.terminate() }
            let killAt = Date().addingTimeInterval(1)
            while proc.isRunning && Date() < killAt { Thread.sleep(forTimeInterval: 0.03) }
            if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
            proc.waitUntilExit()
        }
        for handle in reads {
            try? handle.close()
        }
        _ = group.wait(timeout: .now() + 2)
        try? FileManager.default.removeItem(atPath: dir)
    }

    private func startReader(_ handle: FileHandle) {
        reads.append(handle)
        group.enter()
        DispatchQueue.global(qos: .userInitiated).async { [output, group] in
            defer { group.leave() }
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                output.append(chunk, limit: 16_000)
            }
        }
    }

    private func waitForSocket(timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if FileManager.default.fileExists(atPath: sock) && proc.isRunning { return true }
            if !proc.isRunning { return false }
            Thread.sleep(forTimeInterval: 0.05)
        }
        return FileManager.default.fileExists(atPath: sock) && proc.isRunning
    }

    private static func mapError(_ text: String, password: String, key: String) -> String {
        if (!password.isEmpty && text.contains(password)) || (!key.isEmpty && text.contains(key)) {
            return "SSH: не удалось подключиться"
        }
        let low = text.lowercased()
        if low.contains("permission denied") || low.contains("authentication") || low.contains("auth fail") {
            return "SSH отклонил вход. В клиентском конфиге пароля нет — нужна полная резервная копия."
        }
        if low.contains("identification has changed") || low.contains("host key verification failed") || low.contains("remote host identification") {
            return "Ключ хоста изменился, подключение остановлено"
        }
        if low.contains("timed out") || low.contains("timeout") {
            return "Таймаут SSH"
        }
        if low.contains("connection refused") {
            return "SSH-порт недоступен"
        }
        return "SSH: не удалось подключиться"
    }
}
