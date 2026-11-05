import AmneziaMonitorCore
import Foundation

/// Адрес панели и список серверов — в UserDefaults. Секреты — только в Связке ключей.
final class SettingsRepository {
    private let defaultsKey = "amnezia.monitor.settings.v1"

    func load() -> AppSettings {
        guard let data = UserDefaults.standard.data(forKey: defaultsKey),
              let pub = try? SettingsCodec.decodePublic(data) else {
            return AppSettings()
        }
        var settings = pub
        settings.endpoints = pub.endpoints.map { ep in
            var copy = ep
            copy.token = KeychainStore.get(account: Self.tokenAccount(ep.id)) ?? ""
            return copy
        }
        settings.imported = pub.imported.map { srv in
            var copy = srv
            copy.password = KeychainStore.get(account: Self.passwordAccount(srv.id)) ?? ""
            copy.privateKey = KeychainStore.get(account: Self.keyAccount(srv.id)) ?? ""
            return copy
        }
        return settings.normalized()
    }

    func save(_ settings: AppSettings) throws {
        let next = settings.normalized()
        let previous = currentPublic()
        for ep in next.endpoints {
            try KeychainStore.set(account: Self.tokenAccount(ep.id), value: ep.token)
        }
        for ep in previous.endpoints where !next.endpoints.contains(where: { $0.id == ep.id }) {
            KeychainStore.delete(account: Self.tokenAccount(ep.id))
        }
        for srv in next.imported {
            try KeychainStore.set(account: Self.passwordAccount(srv.id), value: srv.password)
            try KeychainStore.set(account: Self.keyAccount(srv.id), value: srv.privateKey)
        }
        for srv in previous.imported where !next.imported.contains(where: { $0.id == srv.id }) {
            KeychainStore.delete(account: Self.passwordAccount(srv.id))
            KeychainStore.delete(account: Self.keyAccount(srv.id))
        }
        let data = try SettingsCodec.encodePublic(next)
        UserDefaults.standard.set(data, forKey: defaultsKey)
    }

    var knownHostsURL: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        let dir = base.appendingPathComponent("AmneziaMonitor", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("known_hosts")
        if !FileManager.default.fileExists(atPath: file.path) {
            FileManager.default.createFile(atPath: file.path, contents: Data(), attributes: [.posixPermissions: 0o600])
        }
        return file
    }

    private func currentPublic() -> AppSettings {
        guard let data = UserDefaults.standard.data(forKey: defaultsKey),
              let pub = try? SettingsCodec.decodePublic(data) else {
            return AppSettings()
        }
        return pub
    }

    private static func tokenAccount(_ id: String) -> String { "token.\(id)" }
    private static func passwordAccount(_ id: String) -> String { "ssh-password.\(id)" }
    private static func keyAccount(_ id: String) -> String { "ssh-key.\(id)" }
}
