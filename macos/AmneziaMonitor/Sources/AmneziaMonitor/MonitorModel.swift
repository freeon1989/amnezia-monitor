import AmneziaMonitorCore
import Foundation

struct HomeUi {
    var servers: [ServerSummary] = []
    var loading = false
    var refreshing = false
    var error: String?
    var updatedAt: Int64?
    var hasSource = false
    var label = ""
}

@MainActor
final class MonitorModel: ObservableObject {
    @Published var settings: AppSettings
    @Published var home = HomeUi()
    @Published var detail: ServerDetail?
    @Published var detailError: String?
    @Published var detailLoading = false
    @Published var hours = 24
    @Published var selection: String?
    @Published var importMessage: String?
    @Published var showSettings = false

    private let api = MonitorApi()
    private let repo = SettingsRepository()
    private var loop: Task<Void, Never>?
    private var gate = false
    private var prevs: [String: ProbePrev] = [:]
    private var localDetails: [String: ServerDetail] = [:]

    init() {
        settings = repo.load()
        startLoop()
    }

    var menuTitle: String {
        let list = home.servers
        if list.isEmpty { return "" }
        let up = list.filter { $0.status == "up" }.count
        return "\(up)/\(list.count)"
    }

    var menuSymbol: String {
        if home.servers.isEmpty { return "shield.lefthalf.filled" }
        if home.servers.contains(where: { $0.status != "up" }) { return "shield.slash" }
        return "shield.lefthalf.filled"
    }

    var selectedSummary: ServerSummary? {
        home.servers.first { $0.id == selection }
    }

    func startLoop() {
        loop?.cancel()
        loop = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh(manual: false)
                let sec = UInt64(self?.settings.intervalSec ?? 30)
                try? await Task.sleep(nanoseconds: sec * 1_000_000_000)
            }
        }
    }

    func refresh(manual: Bool) async {
        if gate { return }
        gate = true
        defer { gate = false }
        let cur = settings
        let ep = cur.active
        if ep == nil && cur.imported.isEmpty {
            home = HomeUi()
            return
        }
        var next = home
        next.hasSource = true
        next.label = ep.map { $0.label.isEmpty ? $0.baseUrl : $0.label } ?? "AmneziaVPN"
        next.loading = home.servers.isEmpty
        next.refreshing = manual && !home.servers.isEmpty
        home = next

        var dash: [ServerSummary] = []
        var dashError: String?
        if let ep = ep {
            do {
                dash = try await api.servers(ep)
            } catch {
                dashError = friendlyError(error)
            }
        }
        let covered: Set<String> = (dashError == nil && ep != nil)
            ? Set(dash.compactMap { $0.host?.lowercased() })
            : []
        var localRows: [ServerSummary] = []
        let known = repo.knownHostsURL
        for srv in cur.imported where !covered.contains(srv.host.lowercased()) {
            let prev = prevs[srv.id]
            let polled = await Task.detached(priority: .userInitiated) {
                SshProbe.poll(server: srv, prev: prev, knownHosts: known)
            }.value
            prevs[srv.id] = polled.prev
            localDetails[srv.id] = polled.detail
            localRows.append(polled.summary)
        }
        var done = home
        done.servers = dash + localRows
        done.error = dashError
        done.updatedAt = Int64(Date().timeIntervalSince1970 * 1000)
        done.loading = false
        done.refreshing = false
        done.hasSource = true
        done.label = next.label
        home = done

        guard let row = selectedSummary else { return }
        if let local = row.localId, let snap = localDetails[local] {
            detail = snap
            detailError = nil
            detailLoading = false
        } else if let ep = ep, dashError == nil {
            await loadDetail(ep: ep, name: row.name)
        } else if dashError != nil, row.localId == nil {
            detailError = dashError
            detailLoading = false
        }
    }

    func select(_ id: String?) async {
        selection = id
        detail = nil
        detailError = nil
        guard let row = selectedSummary else { return }
        if let local = row.localId, let snap = localDetails[local] {
            detail = snap
            return
        }
        guard let ep = settings.active else { return }
        await loadDetail(ep: ep, name: row.name)
    }

    func setHours(_ value: Int) async {
        let h = min(168, max(1, value))
        if h == hours { return }
        hours = h
        guard let row = selectedSummary, row.localId == nil, let ep = settings.active else { return }
        await loadDetail(ep: ep, name: row.name)
    }

    func importText(_ text: String) {
        do {
            let parsed = try AmneziaImport.parse(text)
            let before = Set(settings.imported.map { "\($0.host.lowercased()):\($0.port)" })
            let merged = AmneziaImport.merge(current: settings.imported, incoming: parsed) { UUID().uuidString }
            let added = merged.filter { !before.contains("\($0.host.lowercased()):\($0.port)") }.count
            var next = settings
            next.imported = merged
            try repo.save(next)
            settings = next.normalized()
            let withSsh = parsed.filter(\.hasSshAuth).count
            importMessage = "Серверов в файле: \(parsed.count). Новых адресов: \(added). С паролем SSH: \(withSsh)."
            startLoop()
        } catch let error as UserError {
            importMessage = error.message
        } catch {
            importMessage = "Не удалось прочитать конфиг"
        }
    }

    func importFile(_ url: URL) {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        do {
            let data = try Data(contentsOf: url)
            if data.count > 1_500_000 { throw UserError("Файл слишком большой") }
            guard let text = String(data: data, encoding: .utf8) else {
                throw UserError("Файл не в кодировке UTF-8")
            }
            importText(text)
        } catch let error as UserError {
            importMessage = error.message
        } catch {
            importMessage = "Не удалось открыть файл"
        }
    }

    func deleteImported(_ id: String) {
        localDetails.removeValue(forKey: id)
        prevs.removeValue(forKey: id)
        if selectedSummary?.localId == id { selection = nil }
        var next = settings
        next.imported.removeAll { $0.id == id }
        do {
            try repo.save(next)
            settings = next.normalized()
        } catch let error as UserError {
            importMessage = error.message
        } catch {
            importMessage = "Не удалось удалить сервер"
        }
    }

    func forgetHostKey(for summary: ServerSummary) {
        guard let host = summary.host else { return }
        let port = settings.imported.first { $0.id == summary.localId }?.port ?? 22
        SshProbe.forget(host: host, port: port, file: repo.knownHostsURL)
        importMessage = "Ключ хоста \(host) забыт. Следующий опрос примет новый."
    }

    func upsertEndpoint(_ ep: Endpoint, makeActive: Bool) {
        var next = settings
        if let idx = next.endpoints.firstIndex(where: { $0.id == ep.id }) {
            next.endpoints[idx] = ep
        } else {
            next.endpoints.append(ep)
        }
        if makeActive || next.activeId == nil { next.activeId = ep.id }
        store(next)
    }

    func deleteEndpoint(_ id: String) {
        var next = settings
        next.endpoints.removeAll { $0.id == id }
        if next.activeId == id { next.activeId = next.endpoints.first?.id }
        store(next)
    }

    func setActive(_ id: String) {
        var next = settings
        next.activeId = id
        store(next)
    }

    func setInterval(_ sec: Int) {
        var next = settings
        next.intervalSec = sec
        store(next)
        startLoop()
    }

    func testEndpoint(_ ep: Endpoint) async -> String {
        do {
            let clean = Endpoint(
                id: ep.id,
                label: ep.label,
                baseUrl: try normalizeBaseUrl(ep.baseUrl),
                token: try cleanToken(ep.token)
            )
            let health = try await api.health(clean)
            if let app = health.app, !app.isEmpty, app != "amnezia-monitor" {
                return "Ответ не от Amnezia Monitor (\(app))."
            }
            let n = try await api.servers(clean).count
            return "Связь есть. Серверов в панели: \(n)."
        } catch {
            return friendlyError(error)
        }
    }

    private func store(_ next: AppSettings) {
        do {
            try repo.save(next)
            settings = next.normalized()
        } catch let error as UserError {
            importMessage = error.message
        } catch {
            importMessage = "Не удалось сохранить настройки"
        }
    }

    private func loadDetail(ep: Endpoint, name: String) async {
        let keep = detail?.name == name
        detailLoading = detail == nil || !keep
        if !keep { detailError = nil }
        do {
            let loaded = try await api.server(ep, name: name, hours: hours)
            if selectedSummary?.name != name || selectedSummary?.localId != nil { return }
            detail = loaded
            detailError = nil
            detailLoading = false
        } catch {
            if selectedSummary?.name != name { return }
            detailError = friendlyError(error)
            detailLoading = false
        }
    }
}
