import Foundation

/// Разбор вывода тех же read-only команд, что и у настольного агента.
/// Приватный ключ интерфейса из первой строки `wg show dump` не читается: у строки пира ровно 9 полей.
public enum ProcParse {
    private static let namePattern = "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
    /// Как re.match в агенте: совпадение с начала строки, не обязательно до конца (кроме веток с `$`).
    private static let allowedPattern =
        "^(cat /proc/|head -1 /proc/stat$|nproc$|(sudo -n )?docker (ps|exec \\S+ sh -c '(awg|wg|cat|tail) )" +
        "|(sudo -n )?docker logs --tail \\d+ [\\w.-]+$)"
    private static let unboundPattern = "(?:^\\[(\\d+)\\].*?)?info: ([0-9a-fA-F.:]+) (\\S+?)\\.? (\\w+) IN\\b"
    private static let skipSuffix = [".in-addr.arpa", ".ip6.arpa", ".local", ".lan", ".internal"]

    public static func allowed(_ cmd: String) -> Bool {
        guard let re = try? NSRegularExpression(pattern: allowedPattern) else { return false }
        let range = NSRange(cmd.startIndex..., in: cmd)
        guard let m = re.firstMatch(in: cmd, range: range) else { return false }
        return m.range.location == 0
    }

    public static func safeName(_ name: String) -> String? {
        wholeMatch(namePattern, name) ? name : nil
    }

    public static func uptimeSeconds(_ text: String) -> Double? {
        Double(text.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: " ").first.map(String.init) ?? "")
    }

    public static func load1(_ text: String) -> Double? {
        Double(text.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: " ").first.map(String.init) ?? "")
    }

    public static func nproc(_ text: String) -> Int? {
        Int(text.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// Процент, байты занято, байты всего.
    public static func mem(_ text: String) -> (Double, Double, Double)? {
        var vals: [String: Int64] = [:]
        guard let re = try? NSRegularExpression(pattern: "(\\w+):\\s+(\\d+)") else { return nil }
        for line in text.split(whereSeparator: \.isNewline) {
            let s = String(line)
            let range = NSRange(s.startIndex..., in: s)
            guard let m = re.firstMatch(in: s, range: range),
                  let kr = Range(m.range(at: 1), in: s),
                  let vr = Range(m.range(at: 2), in: s),
                  let n = Int64(s[vr]) else { continue }
            vals[String(s[kr])] = n * 1024
        }
        guard let total = vals["MemTotal"], let avail = vals["MemAvailable"] ?? vals["MemFree"], total > 0 else {
            return nil
        }
        let used = total - avail
        let pct = Double(Int64(Double(used) * 1000.0 / Double(total))) / 10.0
        return (pct, Double(used), Double(total))
    }

    public static func cpuJiffies(_ text: String) -> (Int64, Int64)? {
        guard let line = text.split(whereSeparator: \.isNewline).first(where: { $0.hasPrefix("cpu ") }) else { return nil }
        let nums = line.split(whereSeparator: \.isWhitespace).dropFirst().compactMap { Int64($0) }
        if nums.count < 4 { return nil }
        let idle = nums[3] + (nums.count > 4 ? nums[4] : 0)
        let total = nums.prefix(8).reduce(0, +)
        return (total - idle, total)
    }

    public static func cpuPercent(prev: (Int64, Int64)?, cur: (Int64, Int64)?) -> Double? {
        guard let prev = prev, let cur = cur else { return nil }
        let dt = cur.1 - prev.1
        if dt <= 0 { return nil }
        let raw = Double(cur.0 - prev.0) * 100.0 / Double(dt)
        let clamped = min(100, max(0, raw))
        return Double(Int64(clamped * 10)) / 10.0
    }

    public static func netDev(_ text: String) -> [String: (Int64, Int64)] {
        var res: [String: (Int64, Int64)] = [:]
        for lineSub in text.split(whereSeparator: \.isNewline) {
            let line = String(lineSub)
            guard let idx = line.firstIndex(of: ":") else { continue }
            let iface = line[..<idx].trimmingCharacters(in: .whitespaces)
            if iface == "lo" || iface.hasPrefix("veth") { continue }
            let fields = line[line.index(after: idx)...].trimmingCharacters(in: .whitespaces).split(whereSeparator: \.isWhitespace)
            if fields.count >= 9, let rx = Int64(fields[0]), let tx = Int64(fields[8]) {
                res[iface] = (rx, tx)
            }
        }
        return res
    }

    public static func netRates(
        prev: [String: (Int64, Int64)]?,
        cur: [String: (Int64, Int64)],
        dtSec: Double
    ) -> (Double, Double)? {
        guard let prev = prev, dtSec > 0 else { return nil }
        var rx: Int64 = 0
        var tx: Int64 = 0
        for (iface, now) in cur {
            guard let was = prev[iface] else { continue }
            rx += max(0, now.0 - was.0)
            tx += max(0, now.1 - was.1)
        }
        return (Double(rx) / dtSec, Double(tx) / dtSec)
    }

    public static func dockerPs(_ text: String) -> [ContainerRow] {
        var out: [ContainerRow] = []
        for lineSub in text.split(whereSeparator: \.isNewline) {
            let parts = String(lineSub).trimmingCharacters(in: .whitespaces).split(separator: "|", omittingEmptySubsequences: false).map(String.init)
            if parts.count < 2 || parts[0].trimmingCharacters(in: .whitespaces).isEmpty { continue }
            let name = parts[0]
            let status = parts[1]
            let image = parts.count > 2 ? parts[2] : ""
            if !name.hasPrefix("amnezia") && !image.contains("amnezia") { continue }
            if safeName(name) == nil { continue }
            out.append(ContainerRow(
                name: name,
                type: containerType(name),
                up: status.lowercased().hasPrefix("up"),
                status: status
            ))
        }
        return out
    }

    public static func containerType(_ name: String) -> String {
        let n = name.lowercased()
        for t in ["awg", "wireguard", "xray", "openvpn", "cloak", "shadowsocks", "ipsec", "dns", "sftp", "tor"] {
            if n.contains(t) { return t }
        }
        return "other"
    }

    public static func wgPeers(_ text: String, nowSec: Int64, onlineSec: Int64 = 180) -> [WgPeer] {
        var peers: [WgPeer] = []
        for lineSub in text.split(whereSeparator: \.isNewline) {
            let f = lineSub.split(separator: "\t", omittingEmptySubsequences: false).map(String.init)
            if f.count != 9 { continue }
            guard let hs = Int64(f[5]), let rx = Int64(f[6]), let tx = Int64(f[7]) else { continue }
            let age: Double? = hs > 0 ? Double(nowSec - hs) : nil
            let endpoint = (f[3].isEmpty || f[3] == "(none)") ? nil : f[3]
            peers.append(WgPeer(
                pubkey: f[1],
                endpoint: endpoint,
                allowedIps: f[4],
                handshake: hs,
                handshakeAge: age,
                rx: rx,
                tx: tx,
                online: age != nil && age! < Double(onlineSec)
            ))
        }
        return peers
    }

    public static func clientNames(_ text: String) -> [String: String] {
        guard let data = text.data(using: .utf8),
              let el = try? JSONSerialization.jsonObject(with: data),
              let arr = el as? [Any] else { return [:] }
        var res: [String: String] = [:]
        for item in arr {
            guard let o = item as? [String: Any], let id = o["clientId"] as? String else { continue }
            let user = o["userData"] as? [String: Any]
            if let name = user?["clientName"] as? String, !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                res[id] = name
            }
        }
        return res
    }

    public static func dnsTop(_ text: String, peers: [WgPeer]) -> DnsInfo {
        var ipToClient: [String: String] = [:]
        for p in peers {
            let client = String(p.pubkey.prefix(8))
            for raw in p.allowedIps.split(separator: ",") {
                let ip = raw.trimmingCharacters(in: .whitespaces).split(separator: "/").first.map(String.init) ?? ""
                if !ip.isEmpty { ipToClient[ip] = client }
            }
        }
        var counts: [String: Int] = [:]
        var per: [String: [String: Int]] = [:]
        var queries = 0
        for lineSub in text.split(whereSeparator: \.isNewline) {
            guard let row = dnsLine(String(lineSub).trimmingCharacters(in: .whitespaces)) else { continue }
            queries += 1
            let site = rootDomain(row.1)
            counts[site, default: 0] += 1
            let client = ipToClient[row.0] ?? row.0
            var bucket = per[client] ?? [:]
            bucket[site, default: 0] += 1
            per[client] = bucket
        }
        if queries == 0 {
            return DnsInfo(
                status: "no_log",
                note: "Журнал DNS на сервере пуст или не включён. Агент его не включает.",
                queries: nil,
                top: [],
                perUser: []
            )
        }
        let top = counts.sorted { $0.value > $1.value }.prefix(12).map { DnsSite(site: $0.key, count: $0.value) }
        let users = per.map { client, sites -> DnsUser in
            let items = sites.sorted { $0.value > $1.value }.prefix(8).map { DnsSite(site: $0.key, count: $0.value) }
            return DnsUser(client: client, total: sites.values.reduce(0, +), top: items)
        }.sorted { $0.total > $1.total }
        return DnsInfo(status: "ok", note: nil, queries: queries, top: top, perUser: users)
    }

    public static func dockerPsCommand(_ docker: String) -> String {
        guard dockerOk(docker) else { return "rejected" }
        return "\(docker) ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'"
    }

    public static func wgCommand(docker: String, name: String) -> String {
        guard dockerOk(docker), safeName(name) != nil else { return "rejected" }
        return "\(docker) exec \(name) sh -c 'awg show all dump 2>/dev/null || wg show all dump'"
    }

    public static func clientsCommand(docker: String, name: String, type: String) -> String {
        guard dockerOk(docker), safeName(name) != nil, safeType(type) else { return "rejected" }
        return "\(docker) exec \(name) sh -c 'cat /opt/amnezia/\(type)/clientsTable 2>/dev/null || cat /opt/amnezia/*/clientsTable 2>/dev/null'"
    }

    public static func dnsLogCommand(docker: String, name: String, lines: Int = 400) -> String {
        guard dockerOk(docker), safeName(name) != nil, (1...5000).contains(lines) else { return "rejected" }
        return "\(docker) exec \(name) sh -c 'tail -n \(lines) /opt/adguardhome/work/data/querylog.json 2>/dev/null || tail -n \(lines) /var/log/unbound/unbound.log 2>/dev/null || tail -n \(lines) /var/log/unbound.log 2>/dev/null'"
    }

    public static func dnsDockerLogsCommand(docker: String, name: String, lines: Int = 400) -> String {
        guard dockerOk(docker), safeName(name) != nil, (1...5000).contains(lines) else { return "rejected" }
        return "\(docker) logs --tail \(lines) \(name)"
    }

    private static func dockerOk(_ docker: String) -> Bool {
        docker == "docker" || docker == "sudo -n docker"
    }

    private static func safeType(_ type: String) -> Bool {
        ["awg", "wireguard", "xray", "openvpn", "cloak", "shadowsocks", "ipsec", "dns", "sftp", "tor", "other"].contains(type)
    }

    private static func dnsLine(_ line: String) -> (String, String)? {
        if line.isEmpty { return nil }
        if line.hasPrefix("{") {
            guard let data = line.data(using: .utf8),
                  let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let qh = o["QH"] as? String,
                  let ip = o["IP"] as? String else { return nil }
            let dom = qh.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
            if !keepDomain(dom) { return nil }
            return (ip, dom)
        }
        guard let re = try? NSRegularExpression(pattern: unboundPattern, options: []) else { return nil }
        let range = NSRange(line.startIndex..., in: line)
        guard let m = re.firstMatch(in: line, range: range),
              let ipR = Range(m.range(at: 2), in: line),
              let domR = Range(m.range(at: 3), in: line) else { return nil }
        let dom = line[domR].trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
        if !keepDomain(dom) { return nil }
        return (String(line[ipR]), dom)
    }

    private static func keepDomain(_ dom: String) -> Bool {
        if dom.isEmpty || !dom.contains(".") || dom == "localhost" { return false }
        return !skipSuffix.contains(where: { dom.hasSuffix($0) })
    }

    private static func rootDomain(_ domain: String) -> String {
        let parts = domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased().split(separator: ".").map(String.init)
        let sld: Set<String> = ["co", "com", "org", "net", "gov", "edu", "ac"]
        if parts.count >= 3 && parts[parts.count - 1].count == 2 && sld.contains(parts[parts.count - 2]) {
            return parts.suffix(3).joined(separator: ".")
        }
        return parts.suffix(2).joined(separator: ".")
    }

    private static func wholeMatch(_ pattern: String, _ text: String) -> Bool {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return false }
        let range = NSRange(text.startIndex..., in: text)
        guard let m = re.firstMatch(in: text, range: range) else { return false }
        return m.range.location == 0 && m.range.length == range.length
    }
}
