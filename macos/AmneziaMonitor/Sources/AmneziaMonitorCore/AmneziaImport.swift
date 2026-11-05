import CZlibUncompress
import CoreFoundation
import Foundation

/// Разбор того, что пользователь может вынести из AmneziaVPN без чтения её закрытого хранилища:
/// резервная копия (JSON), ссылка vpn:// и клиентский .conf.
/// Из объекта сервера берутся только имя, адрес, SSH-порт, пользователь и секрет входа.
/// Контейнеры, ключи WireGuard/AmneziaWG и last_config отбрасываются.
public enum AmneziaImport {
    private static let maxPlain = 2_000_000
    private static let hostPattern = "^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$"
    private static let userPattern = "^[A-Za-z0-9._-]{1,32}$"

    public static func parse(_ raw: String) throws -> [ImportedServer] {
        let text = try unwrap(raw)
        var trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.hasPrefix("\u{FEFF}") { trimmed.removeFirst() }
        if trimmed.isEmpty { throw UserError("Пустой файл") }
        let fromJson = (try? serversFromJson(trimmed)) ?? []
        let found: [ImportedServer]
        if !fromJson.isEmpty {
            found = fromJson
        } else if looksLikeConf(trimmed) {
            if let one = fromConf(trimmed) {
                found = [one]
            } else {
                found = []
            }
        } else {
            throw UserError("Это не резервная копия Amnezia, не ссылка vpn:// и не конфиг WireGuard")
        }
        if found.isEmpty { throw UserError("В файле нет серверов с адресом") }
        return found
    }

    public static func merge(
        current: [ImportedServer],
        incoming: [ImportedServer],
        newId: () -> String
    ) -> [ImportedServer] {
        var out = current
        for inc in incoming {
            if let idx = out.firstIndex(where: {
                $0.host.caseInsensitiveCompare(inc.host) == .orderedSame && $0.port == inc.port
            }) {
                let old = out[idx]
                out[idx] = ImportedServer(
                    id: old.id,
                    name: inc.name.isEmpty ? old.name : inc.name,
                    host: old.host,
                    port: old.port,
                    user: inc.user.isEmpty ? old.user : inc.user,
                    password: inc.password.isEmpty ? old.password : inc.password,
                    privateKey: inc.privateKey.isEmpty ? old.privateKey : inc.privateKey
                )
            } else {
                out.append(ImportedServer(
                    id: newId(),
                    name: inc.name,
                    host: inc.host,
                    port: inc.port,
                    user: inc.user,
                    password: inc.password,
                    privateKey: inc.privateKey
                ))
            }
        }
        return Array(out.prefix(32))
    }

    private static func unwrap(_ raw: String) throws -> String {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.hasPrefix("\u{FEFF}") { text.removeFirst() }
        guard let at = text.range(of: "vpn://") else { return text }
        let token = text[at.upperBound...].prefix { !$0.isWhitespace }
        return try decodeVpnToken(String(token))
    }

    private static func decodeVpnToken(_ token: String) throws -> String {
        let bytes: Data
        do {
            bytes = try b64Url(token)
        } catch {
            throw UserError("Ссылка vpn:// повреждена")
        }
        let plain = qUncompress(bytes) ?? bytes
        if plain.count > maxPlain { throw UserError("Конфиг слишком большой") }
        guard let text = String(data: plain, encoding: .utf8) else {
            throw UserError("В ссылке vpn:// нет описания сервера")
        }
        if !text.contains("hostName") && !text.contains("Endpoint") && !text.contains("serversList") {
            throw UserError("В ссылке vpn:// нет описания сервера")
        }
        return text
    }

    private static func serversFromJson(_ text: String) throws -> [ImportedServer] {
        let el = try JSONSerialization.jsonObject(with: Data(text.utf8))
        let objects: [[String: Any]]
        if let arr = el as? [Any] {
            objects = arr.compactMap { $0 as? [String: Any] }
        } else if let obj = el as? [String: Any] {
            objects = serversInObject(obj)
        } else {
            objects = []
        }
        return objects.compactMap(toServer)
    }

    private static func serversInObject(_ o: [String: Any]) -> [[String: Any]] {
        let keys = Array(o.keys)
        let listKey = keys.first(where: { $0 == "Servers/serversList" })
            ?? keys.first(where: { $0.hasSuffix("serversList") })
        if let listKey = listKey {
            return elementsOf(o[listKey]).compactMap { $0 as? [String: Any] }
        }
        if o["hostName"] != nil || o["hostname"] != nil {
            return [o]
        }
        if let nested = o["servers"] {
            return elementsOf(nested).compactMap { $0 as? [String: Any] }
        }
        return []
    }

    private static func elementsOf(_ el: Any?) -> [Any] {
        if let arr = el as? [Any] { return arr }
        guard let s = el as? String else { return [] }
        if let parsed = try? JSONSerialization.jsonObject(with: Data(s.utf8)) {
            if let arr = parsed as? [Any] { return arr }
            if let obj = parsed as? [String: Any] { return [obj] }
        }
        if let decoded = try? b64Url(s), let text = String(data: decoded, encoding: .utf8),
           let again = try? JSONSerialization.jsonObject(with: Data(text.utf8)) {
            if let arr = again as? [Any] { return arr }
            if let obj = again as? [String: Any] { return [obj] }
        }
        return []
    }

    private static func toServer(_ o: [String: Any]) -> ImportedServer? {
        let host = (str(o, "hostName", "hostname", "host") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !wholeMatch(hostPattern, host) || host.hasPrefix("127.") { return nil }
        let description = (str(o, "description", "name") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let userRaw = (str(o, "userName", "username", "user") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let user = userRaw.isEmpty ? "root" : userRaw
        if !wholeMatch(userPattern, user) { return nil }
        let port = int(o, "port") ?? 22
        if port < 1 || port > 65535 { return nil }
        let secret = str(o, "password", "secretData") ?? ""
        if secret.count > 32_000 { return nil }
        let key = secret.contains("PRIVATE KEY") || secret.contains("BEGIN OPENSSH")
        let name = description.isEmpty ? host : description
        return ImportedServer(
            id: "",
            name: String(name.prefix(40)),
            host: host,
            port: port,
            user: user,
            password: key ? "" : secret,
            privateKey: key ? secret : ""
        )
    }

    private static func looksLikeConf(_ text: String) -> Bool {
        let l = text.lowercased()
        return l.contains("[peer]") || l.contains("[interface]") || l.contains("\nremote ") || l.hasPrefix("remote ")
    }

    private static func fromConf(_ text: String) -> ImportedServer? {
        let title = text.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .first { $0.hasPrefix("#") && $0.count > 1 }
            .map { String($0.dropFirst()).trimmingCharacters(in: .whitespaces) } ?? ""
        let endpoint = firstGroup("(?im)^\\s*Endpoint\\s*=\\s*([^\\s#]+)", text)
        let remote = firstGroup("(?im)^\\s*remote\\s+(\\S+)", text)
        guard let rawHost = endpoint ?? remote else { return nil }
        let host = stripConfHost(rawHost)
        if !wholeMatch(hostPattern, host) { return nil }
        let name = title.isEmpty ? host : title
        return ImportedServer(
            id: "",
            name: String(name.prefix(40)),
            host: host,
            port: 22,
            user: "root"
        )
    }

    private static func stripConfHost(_ rawHost: String) -> String {
        var host = rawHost
        if let colon = host.lastIndex(of: ":") {
            host = String(host[..<colon])
        }
        if host.hasPrefix("[") { host.removeFirst() }
        if host.hasSuffix("]") { host.removeLast() }
        if let pct = host.firstIndex(of: "%") {
            host = String(host[..<pct])
        }
        return host.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func str(_ o: [String: Any], _ keys: String...) -> String? {
        for k in keys {
            guard let v = o[k], !(v is NSNull) else { continue }
            guard let s = v as? String, !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            return s
        }
        return nil
    }

    private static func int(_ o: [String: Any], _ key: String) -> Int? {
        guard let v = o[key], !(v is NSNull) else { return nil }
        if let i = v as? Int { return i }
        if let d = v as? Double { return Int(d) }
        if let s = v as? String {
            if let i = Int(s) { return i }
            if let d = Double(s) { return Int(d) }
        }
        if let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() {
            return Int(n.doubleValue)
        }
        return nil
    }

    static func b64Url(_ s: String) throws -> Data {
        var t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        t = t.replacingOccurrences(of: "\n", with: "").replacingOccurrences(of: "\r", with: "")
        let pad = (4 - (t.count % 4)) % 4
        if pad != 0 { t += String(repeating: "=", count: pad) }
        let std = t.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        guard let data = Data(base64Encoded: std) else {
            throw UserError("Ссылка vpn:// повреждена")
        }
        return data
    }

    /// Формат Qt qCompress: 4 байта длины (big-endian) и zlib.
    static func qUncompress(_ data: Data) -> Data? {
        guard data.count >= 6 else { return nil }
        let bytes = [UInt8](data.prefix(4))
        let declared = (Int(bytes[0]) << 24) | (Int(bytes[1]) << 16) | (Int(bytes[2]) << 8) | Int(bytes[3])
        if declared <= 0 || declared > maxPlain { return nil }
        var dst = [UInt8](repeating: 0, count: declared)
        var outLen: size_t = 0
        let rc: Int32 = data.withUnsafeBytes { raw in
            guard let base = raw.bindMemory(to: UInt8.self).baseAddress else { return -1 }
            return dst.withUnsafeMutableBytes { outRaw in
                guard let outBase = outRaw.bindMemory(to: UInt8.self).baseAddress else { return -1 }
                return amz_zlib_uncompress(base + 4, data.count - 4, outBase, declared, &outLen)
            }
        }
        if rc != 0 || outLen == 0 { return nil }
        return Data(dst.prefix(Int(outLen)))
    }

    private static func wholeMatch(_ pattern: String, _ text: String) -> Bool {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return false }
        let range = NSRange(text.startIndex..., in: text)
        guard let m = re.firstMatch(in: text, range: range) else { return false }
        return m.range.location == 0 && m.range.length == range.length
    }

    private static func firstGroup(_ pattern: String, _ text: String) -> String? {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return nil }
        let range = NSRange(text.startIndex..., in: text)
        guard let m = re.firstMatch(in: text, range: range), m.numberOfRanges > 1,
              let r = Range(m.range(at: 1), in: text) else { return nil }
        return String(text[r])
    }
}
