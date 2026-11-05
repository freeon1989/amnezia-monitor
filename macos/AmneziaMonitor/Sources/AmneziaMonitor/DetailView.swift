import SwiftUI

struct DetailView: View {
    @ObservedObject var model: MonitorModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if model.selectedSummary == nil {
                    Text("Выберите сервер слева.")
                        .foregroundStyle(Theme.muted)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(24)
                } else {
                    header
                    if model.selectedSummary?.localId == nil {
                        hourPicker
                    }
                    if model.detailLoading && model.detail == nil {
                        ProgressView("Загрузка…").padding(24)
                    }
                    if let error = model.detailError {
                        ErrorBanner(text: error)
                        if let row = model.selectedSummary, row.localId != nil, error.contains("Ключ хоста") {
                            Button("Забыть ключ хоста") { model.forgetHostKey(for: row) }
                        }
                    }
                    if let detail = model.detail {
                        detailBody(detail)
                    }
                }
            }
            .padding(18)
        }
        .background(Theme.bg)
        .navigationTitle(model.detail?.name ?? model.selectedSummary?.name ?? "Сервер")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(model.detail?.name ?? model.selectedSummary?.name ?? "").font(.title2.weight(.semibold)).foregroundStyle(Theme.text)
            Text(model.detail?.host ?? model.selectedSummary?.host ?? "").font(.callout).foregroundStyle(Theme.muted)
        }
    }

    private var hourPicker: some View {
        HStack(spacing: 8) {
            ForEach([6, 24, 72, 168], id: \.self) { h in
                let label = h == 72 ? "3 д" : (h == 168 ? "7 д" : "\(h) ч")
                Button(label) { Task { await model.setHours(h) } }
                    .buttonStyle(.bordered)
                    .tint(model.hours == h ? Theme.indigo : Theme.muted)
            }
            Spacer()
        }
    }

    @ViewBuilder
    private func detailBody(_ d: ServerDetail) -> some View {
        let c = d.current
        card {
            HStack {
                StatusChip(status: c.status)
                Spacer()
                Text("опрос \(fmtAgo(c.ageSec))").font(.caption).foregroundStyle(Theme.muted)
            }
            if let error = c.error, !error.isEmpty {
                Text(error).foregroundStyle(Theme.red).font(.callout)
            }
            if c.stale {
                Text("Сервер сейчас недоступен. Цифры ниже — с последнего успешного опроса.")
                    .font(.caption).foregroundStyle(Theme.amber)
            }
            Meter(title: "CPU", value: c.cpu)
            Meter(title: "Память", value: c.mem)
            Text("Память \(fmtBytes(c.memUsed)) / \(fmtBytes(c.memTotal))")
                .font(.caption).foregroundStyle(Theme.muted)
        }
        HStack(spacing: 10) {
            kpi("Задержка", fmtMs(c.tcpMs), "ping \(fmtMs(c.pingMs))")
            kpi("Доступность", c.availability.map { "\($0)%" } ?? "—", "средняя \(fmtMs(c.latencyAvg))")
        }
        HStack(spacing: 10) {
            kpi("Аптайм", fmtDuration(c.uptimeSec), "load \(c.load1.map { String($0) } ?? "—") / \(c.nproc.map(String.init) ?? "—")")
            kpi("Трафик", "↓ \(fmtRate(c.txBps))", "↑ \(fmtRate(c.rxBps))")
        }
        if !d.containers.isEmpty {
            SectionTitle(title: "Контейнеры")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(d.containers, id: \.name) { box in
                        Text("\(box.name)\(box.type.map { " · \($0)" } ?? "")")
                            .font(.caption)
                            .foregroundStyle(box.up ? Theme.green : Theme.red)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(Theme.card2, in: RoundedRectangle(cornerRadius: 8))
                    }
                }
            }
        }
        if let series = d.series, !series.cpu.isEmpty || !series.downBytes.isEmpty {
            card {
                SectionTitle(title: "CPU и память", trailing: "CPU \(fmtPct(c.cpu))")
                LineChart(values: series.cpu, color: Theme.indigo, yMax: 100)
                LineChart(values: series.mem, color: Theme.cyan, yMax: 100)
                Text("Индиго — CPU, бирюзовый — память. Пустые промежутки — не было опроса.")
                    .font(.caption2).foregroundStyle(Theme.muted)
                SectionTitle(title: "Трафик интерфейса", trailing: "↓ отдан  ↑ принят")
                TrafficBars(down: series.downBytes, up: series.upBytes)
                Text("За период: ↓ \(fmtBytes(d.down24h))  ↑ \(fmtBytes(d.up24h))")
                    .font(.caption).foregroundStyle(Theme.muted)
            }
        } else if d.series == nil {
            Text("Истории для графиков пока нет — дождитесь нескольких опросов панели. Прямой SSH показывает только текущий срез.")
                .font(.caption).foregroundStyle(Theme.muted)
        }
        SectionTitle(title: "Пользователи", trailing: "\(d.users.filter(\.online).count) онлайн")
        if d.users.isEmpty {
            Text("Пиров WireGuard/AmneziaWG нет в последнем опросе.").foregroundStyle(Theme.muted).font(.callout)
        }
        ForEach(Array(d.users.enumerated()), id: \.offset) { _, user in
            userCard(user)
        }
        dnsBlock(d.dns)
        if !d.events.isEmpty {
            SectionTitle(title: "События")
            card {
                ForEach(Array(d.events.prefix(25).enumerated()), id: \.offset) { _, ev in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(fmtEventTime(ev.ts)).font(.caption2).foregroundStyle(Theme.muted)
                        Text(ev.message).font(.callout).foregroundStyle(Theme.text)
                    }
                }
            }
        }
    }

    private func kpi(_ title: String, _ value: String, _ sub: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(Theme.muted)
            Text(value).font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
            Text(sub).font(.caption2).foregroundStyle(Theme.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 12))
    }

    private func userCard(_ u: UserRow) -> some View {
        card {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(u.client).font(.headline).foregroundStyle(Theme.text)
                    Text([u.container, u.endpoint, u.pubkey].compactMap { $0 }.joined(separator: " · "))
                        .font(.caption2).foregroundStyle(Theme.muted)
                }
                Spacer()
                StatusChip(status: u.online ? "up" : "down", label: u.online ? "онлайн" : "офлайн")
            }
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("↓ \(fmtRate(u.rateDown))   ↑ \(fmtRate(u.rateUp))").font(.callout).foregroundStyle(Theme.text)
                    Text("24 ч ↓ \(fmtBytes(u.down24h))  ↑ \(fmtBytes(u.up24h))").font(.caption).foregroundStyle(Theme.muted)
                    Text("всего ↓ \(fmtBytes(u.downTotal))  ↑ \(fmtBytes(u.upTotal)) · handshake \(fmtDuration(u.handshakeAge))")
                        .font(.caption2).foregroundStyle(Theme.muted)
                }
                Spacer()
                if !u.spark.isEmpty {
                    Sparkline(values: u.spark, color: u.online ? Theme.green : Theme.muted)
                }
            }
        }
    }

    private func dnsBlock(_ dns: DnsInfo) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle(title: "Сайты по DNS", trailing: dns.queries.map { "\($0) запросов" })
            card {
                if dns.status != "ok" {
                    Text(dns.note ?? "DNS-журнал недоступен.").font(.callout).foregroundStyle(Theme.amber)
                }
                if dns.top.isEmpty && dns.status == "ok" {
                    Text("Запросов за период нет.").foregroundStyle(Theme.muted)
                }
                let maxN = max(dns.top.map(\.count).max() ?? 1, 1)
                ForEach(dns.top, id: \.site) { site in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(site.site).font(.callout).foregroundStyle(Theme.text)
                            Spacer()
                            Text("\(site.count)").font(.caption.monospacedDigit()).foregroundStyle(Theme.muted)
                        }
                        GeometryReader { geo in
                            Capsule().fill(Theme.indigo.opacity(0.8))
                                .frame(width: geo.size.width * CGFloat(site.count) / CGFloat(maxN))
                        }
                        .frame(height: 4)
                    }
                }
            }
        }
    }

    private func card<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 12))
    }
}
