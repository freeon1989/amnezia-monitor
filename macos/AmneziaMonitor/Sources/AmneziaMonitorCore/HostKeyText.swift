import Foundation

/// Отпечаток ключа хоста, как его печатает `ssh-keygen -l`.
public struct HostKeyFingerprint: Equatable, Sendable {
    public var type: String
    public var fingerprint: String

    public init(type: String, fingerprint: String) {
        self.type = type
        self.fingerprint = fingerprint
    }
}

/// Ключ хоста не совпал с сохранённым. Показывается пользователю до любой замены.
public struct HostKeyChange: Equatable, Sendable {
    public var host: String
    public var port: Int
    public var old: [HostKeyFingerprint]
    public var new: HostKeyFingerprint?

    public init(host: String, port: Int, old: [HostKeyFingerprint], new: HostKeyFingerprint?) {
        self.host = host
        self.port = port
        self.old = old
        self.new = new
    }
}

/// Разбор вывода OpenSSH для проверки и замены ключа хоста (без запуска процессов — проверяется тестами).
public enum HostKeyText {
    /// Как OpenSSH пишет хост в known_hosts: `host` для порта 22, иначе `[host]:port`.
    public static func entryName(host: String, port: Int) -> String {
        port == 22 ? host : "[\(host)]:\(port)"
    }

    /// Есть ли в выводе ssh признак смены ключа.
    public static func isChangedKey(_ stderr: String) -> Bool {
        let low = stderr.lowercased()
        return low.contains("remote host identification has changed")
            || low.contains("host key verification failed")
            || low.contains("identification has changed")
    }

    /// Новый ключ из предупреждения ssh: «The fingerprint for the ED25519 key sent by the remote host is SHA256:…».
    public static func parseChangedKeyMessage(_ stderr: String) -> HostKeyFingerprint? {
        let flat = stderr.replacingOccurrences(of: "\r", with: " ").replacingOccurrences(of: "\n", with: " ")
        guard let range = flat.range(
            of: #"fingerprint for the ([A-Za-z0-9-]+) key sent by the remote host is\s+(SHA256:[A-Za-z0-9+/]+)"#,
            options: .regularExpression
        ) else { return nil }
        let part = String(flat[range])
        let words = part.split(whereSeparator: { $0 == " " }).map(String.init)
        guard let idx = words.firstIndex(of: "the"), idx + 1 < words.count,
              let fp = words.last(where: { $0.hasPrefix("SHA256:") }) else { return nil }
        return HostKeyFingerprint(type: words[idx + 1].uppercased(), fingerprint: fp)
    }

    /// Строки `ssh-keygen -l -F host -f file` («host ED25519 SHA256:…») и `ssh-keygen -lf` («256 SHA256:… host (ED25519)»).
    public static func parseKeygenList(_ out: String) -> [HostKeyFingerprint] {
        var res: [HostKeyFingerprint] = []
        for raw in out.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            let parts = line.split(whereSeparator: { $0 == " " || $0 == "\t" }).map(String.init)
            guard let fpIdx = parts.firstIndex(where: { $0.hasPrefix("SHA256:") }) else { continue }
            let fp = parts[fpIdx]
            var type = ""
            if let last = parts.last, last.hasPrefix("("), last.hasSuffix(")") {
                type = String(last.dropFirst().dropLast())
            } else if fpIdx >= 1 {
                type = parts[fpIdx - 1]
            }
            res.append(HostKeyFingerprint(type: type.uppercased(), fingerprint: fp))
        }
        return res
    }

    /// Строки ключей из `ssh-keyscan` (без комментариев).
    public static func keyscanLines(_ out: String) -> [String] {
        out.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty && !$0.hasPrefix("#") && $0.split(separator: " ").count >= 3 }
    }

    /// known_hosts без записей этого хоста и с новой строкой ключа в конце.
    public static func replacingHost(_ text: String, host: String, port: Int, newLine: String) -> String {
        let markers: Set<String> = [entryName(host: host, port: port)]
        var kept = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init).filter { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { return true }
            let token = trimmed.split(whereSeparator: \.isWhitespace).first.map(String.init) ?? ""
            let names = token.split(separator: ",").map(String.init)
            return !names.contains(where: { markers.contains($0) })
        }
        while let last = kept.last, last.trimmingCharacters(in: .whitespaces).isEmpty { kept.removeLast() }
        kept.append(newLine.trimmingCharacters(in: .whitespacesAndNewlines))
        return kept.joined(separator: "\n") + "\n"
    }
}
