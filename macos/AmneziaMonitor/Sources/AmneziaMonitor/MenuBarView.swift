import AppKit
import SwiftUI

struct MenuBarView: View {
    @ObservedObject var model: MonitorModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Amnezia Monitor").font(.headline).foregroundStyle(Theme.text)
                    Text(subtitle).font(.caption).foregroundStyle(Theme.muted)
                }
                Spacer()
                Button {
                    Task { await model.refresh(manual: true) }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Обновить")
            }
            if let error = model.home.error {
                Text(error).font(.caption).foregroundStyle(Theme.red).lineLimit(3)
            }
            if !model.home.hasSource {
                Text("Импортируйте серверы AmneziaVPN или укажите адрес панели. Приложение только читает состояние и ничего не меняет на серверах.")
                    .font(.caption)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            } else if model.home.loading && model.home.servers.isEmpty {
                ProgressView().controlSize(.small)
            } else if model.home.servers.isEmpty {
                Text("Серверов пока нет.").font(.caption).foregroundStyle(Theme.muted)
            }
            ScrollView {
                VStack(spacing: 8) {
                    ForEach(model.home.servers) { server in
                        serverRow(server)
                    }
                }
            }
            .frame(maxHeight: 320)
            HStack {
                Button("Открыть окно") { showMain(settings: false) }
                Button("Настройки") { showMain(settings: true) }
                Spacer()
                Button("Выйти") { NSApp.terminate(nil) }
            }
            .font(.caption)
        }
        .padding(12)
        .frame(width: 380)
        .background(Theme.bg)
    }

    private var subtitle: String {
        if let ts = model.home.updatedAt { return "обновлено \(fmtClock(ts))" }
        if !model.home.label.isEmpty { return model.home.label }
        return "только чтение"
    }

    private func serverRow(_ server: ServerSummary) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(server.name).font(.callout.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                Spacer()
                StatusChip(status: server.status)
            }
            Text("\(server.host ?? "—") · \(fmtMs(server.tcpMs)) · CPU \(fmtPct(server.cpu)) · память \(fmtPct(server.mem))")
                .font(.caption)
                .foregroundStyle(Theme.muted)
                .lineLimit(1)
            HStack {
                let peers: String = {
                    if server.peersOnline == nil && server.peersTotal == nil { return "—" }
                    return "\(server.peersOnline ?? 0) / \(server.peersTotal ?? 0)"
                }()
                Text("Пиры \(peers)").font(.caption).foregroundStyle(Theme.text)
                Spacer()
                Text("↓ \(fmtRate(server.txBps))  ↑ \(fmtRate(server.rxBps))")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(Theme.muted)
            }
        }
        .padding(8)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 10))
        .contentShape(Rectangle())
        .onTapGesture {
            model.selection = server.id
            showMain(settings: false)
            Task { await model.select(server.id) }
        }
    }

    private func showMain(settings: Bool) {
        model.showSettings = settings
        openWindow(id: "main")
        AppActivate.front()
    }
}
