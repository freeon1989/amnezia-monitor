import Foundation

/// Настройки без секретов. Токен панели, пароль SSH и SSH-ключ пишутся только в Связку ключей.
public struct PublicEndpoint: Codable, Equatable {
    public var id: String
    public var label: String
    public var baseUrl: String
    public init(id: String, label: String, baseUrl: String) {
        self.id = id
        self.label = label
        self.baseUrl = baseUrl
    }
}

public struct PublicServer: Codable, Equatable {
    public var id: String
    public var name: String
    public var host: String
    public var port: Int
    public var user: String
    public init(id: String, name: String, host: String, port: Int, user: String) {
        self.id = id
        self.name = name
        self.host = host
        self.port = port
        self.user = user
    }
}

public struct PublicSettings: Codable, Equatable {
    public var endpoints: [PublicEndpoint]
    public var activeId: String?
    public var intervalSec: Int
    public var imported: [PublicServer]

    public init(endpoints: [PublicEndpoint], activeId: String?, intervalSec: Int, imported: [PublicServer]) {
        self.endpoints = endpoints
        self.activeId = activeId
        self.intervalSec = intervalSec
        self.imported = imported
    }
}

public enum SettingsCodec {
    public static func encodePublic(_ settings: AppSettings) throws -> Data {
        let n = settings.normalized()
        let pub = PublicSettings(
            endpoints: n.endpoints.map { PublicEndpoint(id: $0.id, label: $0.label, baseUrl: $0.baseUrl) },
            activeId: n.activeId,
            intervalSec: n.intervalSec,
            imported: n.imported.map {
                PublicServer(id: $0.id, name: $0.name, host: $0.host, port: $0.port, user: $0.user)
            }
        )
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        return try enc.encode(pub)
    }

    public static func decodePublic(_ data: Data) throws -> AppSettings {
        let pub = try JSONDecoder().decode(PublicSettings.self, from: data)
        return AppSettings(
            endpoints: pub.endpoints.map { Endpoint(id: $0.id, label: $0.label, baseUrl: $0.baseUrl, token: "") },
            activeId: pub.activeId,
            intervalSec: pub.intervalSec,
            imported: pub.imported.map {
                ImportedServer(id: $0.id, name: $0.name, host: $0.host, port: $0.port, user: $0.user, password: "", privateKey: "")
            }
        ).normalized()
    }
}
