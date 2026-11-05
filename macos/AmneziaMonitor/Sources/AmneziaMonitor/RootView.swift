import AmneziaMonitorCore
import SwiftUI

struct RootView: View {
    @ObservedObject var model: MonitorModel
    @State private var tab = 0

    var body: some View {
        VStack(spacing: 0) {
            header
            if tab == 0 {
                NavigationSplitView {
                    serverList
                        .navigationSplitViewColumnWidth(min: 280, ideal: 340, max: 420)
                } detail: {
                    DetailView(model: model)
                }
            } else {
                SettingsView(model: model)
            }
        }
        .background(Theme.bg)
        .preferredColorScheme(.dark)
        .onChange(of: model.showSettings) { value in
            if value { tab = 1 }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Amnezia Monitor").font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
                Text(subtitle).font(.caption).foregroundStyle(Theme.muted)
            }
            Spacer()
            Picker("Раздел", selection: $tab) {
                Text("Серверы").tag(0)
                Text("Настройки").tag(1)
            }
            .pickerStyle(.segmented)
            .frame(width: 220)
            Button {
                Task { await model.refresh(manual: true) }
            } label: {
                Label("Обновить", systemImage: "arrow.clockwise")
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Theme.bg)
    }

    private var subtitle: String {
        if let ts = model.home.updatedAt { return "обновлено \(fmtClock(ts)) · только чтение" }
        if !model.home.label.isEmpty { return model.home.label }
        return "только чтение"
    }

    private var serverList: some View {
        List(selection: Binding(
            get: { model.selection },
            set: { id in Task { await model.select(id) } }
        )) {
            if !model.home.hasSource {
                Text("Добавьте панель или импортируйте AmneziaVPN в настройках.")
                    .foregroundStyle(Theme.muted)
                    .font(.callout)
            }
            if let error = model.home.error {
                ErrorBanner(text: error)
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
            }
            ForEach(model.home.servers) { server in
                ServerRow(server: server).tag(server.id)
            }
        }
        .listStyle(.sidebar)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
    }
}

private struct ServerRow: View {
    var server: ServerSummary
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(server.name).font(.headline).foregroundStyle(Theme.text).lineLimit(1)
                Spacer()
                StatusChip(status: server.status)
            }
            Text("\(server.host ?? "—") · \(fmtMs(server.tcpMs)) · \(fmtAgo(server.ageSec))")
                .font(.caption)
                .foregroundStyle(Theme.muted)
            Meter(title: "CPU", value: server.cpu)
            Meter(title: "Память", value: server.mem)
            HStack {
                let peers: String = {
                    if server.peersOnline == nil && server.peersTotal == nil { return "—" }
                    return "\(server.peersOnline ?? 0) / \(server.peersTotal ?? 0)"
                }()
                Text("Пиры \(peers)").font(.caption).foregroundStyle(Theme.text)
                Spacer()
                Text("↓ \(fmtRate(server.txBps))   ↑ \(fmtRate(server.rxBps))")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(Theme.muted)
            }
            if let error = server.error, !error.isEmpty, server.status != "up" {
                Text(error).font(.caption).foregroundStyle(Theme.red).lineLimit(3)
            }
            if server.localId != nil {
                Text("Прямой опрос SSH").font(.caption2).foregroundStyle(Theme.muted)
            }
        }
        .padding(.vertical, 6)
    }
}
