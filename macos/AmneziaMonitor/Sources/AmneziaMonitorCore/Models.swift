import Foundation

/// Одна сохранённая панель. Токен — секрет чтения, не пароль SSH.
public struct Endpoint: Equatable, Sendable {
    public var id: String
    public var label: String
    public var baseUrl: String
    public var token: String

    public init(id: String, label: String = "", baseUrl: String, token: String = "") {
        self.id = id
        self.label = label
        self.baseUrl = baseUrl
        self.token = token
    }
}

/// Сервер из резервной копии или vpn:// AmneziaVPN.
/// Пароль SSH и ключ живут только в Связке ключей. Ключи VPN сюда не копируются.
public struct ImportedServer: Equatable, Sendable {
    public var id: String
    public var name: String
    public var host: String
    public var port: Int
    public var user: String
    public var password: String
    public var privateKey: String

    public init(
        id: String,
        name: String,
        host: String,
        port: Int = 22,
        user: String = "root",
        password: String = "",
        privateKey: String = ""
    ) {
        self.id = id
        self.name = name
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.privateKey = privateKey
    }

    public var hasSshAuth: Bool {
        !password.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !privateKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

public struct AppSettings: Equatable, Sendable {
    public var endpoints: [Endpoint]
    public var activeId: String?
    public var intervalSec: Int
    public var imported: [ImportedServer]

    public init(
        endpoints: [Endpoint] = [],
        activeId: String? = nil,
        intervalSec: Int = 30,
        imported: [ImportedServer] = []
    ) {
        self.endpoints = endpoints
        self.activeId = activeId
        self.intervalSec = intervalSec
        self.imported = imported
    }

    public var active: Endpoint? {
        if let activeId = activeId, let found = endpoints.first(where: { $0.id == activeId }) {
            return found
        }
        return endpoints.first
    }

    public func normalized() -> AppSettings {
        let list = Array(endpoints.prefix(20))
        let activeOk = list.first(where: { $0.id == activeId })?.id ?? list.first?.id
        return AppSettings(
            endpoints: list,
            activeId: activeOk,
            intervalSec: min(300, max(15, intervalSec)),
            imported: Array(imported.prefix(32))
        )
    }
}

public struct ServerSummary: Equatable, Identifiable, Sendable {
    public var name: String
    public var host: String?
    public var status: String
    public var ageSec: Double?
    public var tcpMs: Double?
    public var cpu: Double?
    public var mem: Double?
    public var peersOnline: Int?
    public var peersTotal: Int?
    public var rxBps: Double?
    public var txBps: Double?
    public var error: String?
    public var localId: String?

    public var id: String { localId ?? "dash:\(name)" }

    public init(
        name: String,
        host: String?,
        status: String,
        ageSec: Double?,
        tcpMs: Double?,
        cpu: Double?,
        mem: Double?,
        peersOnline: Int?,
        peersTotal: Int?,
        rxBps: Double?,
        txBps: Double?,
        error: String?,
        localId: String? = nil
    ) {
        self.name = name
        self.host = host
        self.status = status
        self.ageSec = ageSec
        self.tcpMs = tcpMs
        self.cpu = cpu
        self.mem = mem
        self.peersOnline = peersOnline
        self.peersTotal = peersTotal
        self.rxBps = rxBps
        self.txBps = txBps
        self.error = error
        self.localId = localId
    }
}

public struct Current: Equatable, Sendable {
    public var status: String
    public var error: String?
    public var ageSec: Double?
    public var tcpMs: Double?
    public var pingMs: Double?
    public var availability: Double?
    public var latencyAvg: Double?
    public var uptimeSec: Double?
    public var load1: Double?
    public var nproc: Int?
    public var cpu: Double?
    public var mem: Double?
    public var memUsed: Double?
    public var memTotal: Double?
    public var rxBps: Double?
    public var txBps: Double?
    public var stale: Bool

    public init(
        status: String,
        error: String?,
        ageSec: Double? = nil,
        tcpMs: Double? = nil,
        pingMs: Double? = nil,
        availability: Double? = nil,
        latencyAvg: Double? = nil,
        uptimeSec: Double? = nil,
        load1: Double? = nil,
        nproc: Int? = nil,
        cpu: Double? = nil,
        mem: Double? = nil,
        memUsed: Double? = nil,
        memTotal: Double? = nil,
        rxBps: Double? = nil,
        txBps: Double? = nil,
        stale: Bool = false
    ) {
        self.status = status
        self.error = error
        self.ageSec = ageSec
        self.tcpMs = tcpMs
        self.pingMs = pingMs
        self.availability = availability
        self.latencyAvg = latencyAvg
        self.uptimeSec = uptimeSec
        self.load1 = load1
        self.nproc = nproc
        self.cpu = cpu
        self.mem = mem
        self.memUsed = memUsed
        self.memTotal = memTotal
        self.rxBps = rxBps
        self.txBps = txBps
        self.stale = stale
    }
}

public struct Series: Equatable, Sendable {
    public var cpu: [Double?]
    public var mem: [Double?]
    public var online: [Double?]
    public var downBytes: [Double?]
    public var upBytes: [Double?]

    public init(cpu: [Double?], mem: [Double?], online: [Double?], downBytes: [Double?], upBytes: [Double?]) {
        self.cpu = cpu
        self.mem = mem
        self.online = online
        self.downBytes = downBytes
        self.upBytes = upBytes
    }
}

public struct ContainerRow: Equatable, Sendable {
    public var name: String
    public var type: String?
    public var up: Bool
    public var status: String?

    public init(name: String, type: String?, up: Bool, status: String?) {
        self.name = name
        self.type = type
        self.up = up
        self.status = status
    }
}

public struct UserRow: Equatable, Sendable {
    public var client: String
    public var pubkey: String?
    public var container: String?
    public var online: Bool
    public var handshakeAge: Double?
    public var endpoint: String?
    public var rateDown: Double?
    public var rateUp: Double?
    public var spark: [Double]
    public var down24h: Double
    public var up24h: Double
    public var downTotal: Double?
    public var upTotal: Double?

    public init(
        client: String,
        pubkey: String?,
        container: String?,
        online: Bool,
        handshakeAge: Double?,
        endpoint: String?,
        rateDown: Double?,
        rateUp: Double?,
        spark: [Double],
        down24h: Double,
        up24h: Double,
        downTotal: Double?,
        upTotal: Double?
    ) {
        self.client = client
        self.pubkey = pubkey
        self.container = container
        self.online = online
        self.handshakeAge = handshakeAge
        self.endpoint = endpoint
        self.rateDown = rateDown
        self.rateUp = rateUp
        self.spark = spark
        self.down24h = down24h
        self.up24h = up24h
        self.downTotal = downTotal
        self.upTotal = upTotal
    }
}

public struct DnsSite: Equatable, Sendable {
    public var site: String
    public var count: Int
    public init(site: String, count: Int) {
        self.site = site
        self.count = count
    }
}

public struct DnsUser: Equatable, Sendable {
    public var client: String
    public var total: Int
    public var top: [DnsSite]
    public init(client: String, total: Int, top: [DnsSite]) {
        self.client = client
        self.total = total
        self.top = top
    }
}

public struct DnsInfo: Equatable, Sendable {
    public var status: String
    public var note: String?
    public var queries: Int?
    public var top: [DnsSite]
    public var perUser: [DnsUser]

    public init(status: String, note: String?, queries: Int?, top: [DnsSite], perUser: [DnsUser]) {
        self.status = status
        self.note = note
        self.queries = queries
        self.top = top
        self.perUser = perUser
    }
}

public struct EventRow: Equatable, Sendable {
    public var ts: Double
    public var kind: String?
    public var message: String
    public init(ts: Double, kind: String?, message: String) {
        self.ts = ts
        self.kind = kind
        self.message = message
    }
}

public struct ServerDetail: Equatable, Sendable {
    public var name: String
    public var host: String?
    public var current: Current
    public var containers: [ContainerRow]
    public var series: Series?
    public var users: [UserRow]
    public var down24h: Double?
    public var up24h: Double?
    public var dns: DnsInfo
    public var events: [EventRow]

    public init(
        name: String,
        host: String?,
        current: Current,
        containers: [ContainerRow],
        series: Series?,
        users: [UserRow],
        down24h: Double?,
        up24h: Double?,
        dns: DnsInfo,
        events: [EventRow]
    ) {
        self.name = name
        self.host = host
        self.current = current
        self.containers = containers
        self.series = series
        self.users = users
        self.down24h = down24h
        self.up24h = up24h
        self.dns = dns
        self.events = events
    }
}

public struct Health: Equatable, Sendable {
    public var ok: Bool
    public var app: String?
    public init(ok: Bool, app: String?) {
        self.ok = ok
        self.app = app
    }
}

public struct ApiException: Error, Equatable, Sendable {
    public let code: Int
    public let message: String
    public init(code: Int, message: String) {
        self.code = code
        self.message = message
    }
}

public struct UserError: Error, Equatable, Sendable {
    public let message: String
    public init(_ message: String) {
        self.message = message
    }
}

public struct WgPeer: Equatable, Sendable {
    public var pubkey: String
    public var endpoint: String?
    public var allowedIps: String
    public var handshake: Int64
    public var handshakeAge: Double?
    public var rx: Int64
    public var tx: Int64
    public var online: Bool

    public init(
        pubkey: String,
        endpoint: String?,
        allowedIps: String,
        handshake: Int64,
        handshakeAge: Double?,
        rx: Int64,
        tx: Int64,
        online: Bool
    ) {
        self.pubkey = pubkey
        self.endpoint = endpoint
        self.allowedIps = allowedIps
        self.handshake = handshake
        self.handshakeAge = handshakeAge
        self.rx = rx
        self.tx = tx
        self.online = online
    }
}
