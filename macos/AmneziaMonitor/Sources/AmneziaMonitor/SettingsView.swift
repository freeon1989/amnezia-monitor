import SwiftUI
import UniformTypeIdentifiers

struct SettingsView: View {
    @ObservedObject var model: MonitorModel
    @State private var showImporter = false
    @State private var paste = ""
    @State private var editing: Endpoint?
    @State private var creating = false
    @State private var formError: String?
    @State private var testMsg: String?
    @State private var testing = false
    @State private var pendingDelete: Endpoint?
    @State private var pendingImportDelete: ImportedServer?

    private let intervals = [15, 30, 60, 120]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                importCard
                intervalCard
                panelsCard
                helpCard
            }
            .padding(20)
            .frame(maxWidth: 760, alignment: .leading)
        }
        .background(Theme.bg)
        .fileImporter(isPresented: $showImporter, allowedContentTypes: [.json, .plainText, .text, .data], allowsMultipleSelection: false) { result in
            if case .success(let urls) = result, let url = urls.first {
                model.importFile(url)
            } else if case .failure = result {
                model.importMessage = "Не удалось открыть файл"
            }
        }
        .confirmationDialog("Удалить панель?", isPresented: Binding(
            get: { pendingDelete != nil },
            set: { if !$0 { pendingDelete = nil } }
        ), presenting: pendingDelete) { ep in
            Button("Удалить", role: .destructive) {
                model.deleteEndpoint(ep.id)
                if editing?.id == ep.id { editing = nil }
                pendingDelete = nil
            }
            Button("Отмена", role: .cancel) { pendingDelete = nil }
        } message: { ep in
            Text(ep.baseUrl)
        }
        .confirmationDialog("Убрать сервер из мониторинга?", isPresented: Binding(
            get: { pendingImportDelete != nil },
            set: { if !$0 { pendingImportDelete = nil } }
        ), presenting: pendingImportDelete) { srv in
            Button("Удалить", role: .destructive) {
                model.deleteImported(srv.id)
                pendingImportDelete = nil
            }
            Button("Отмена", role: .cancel) { pendingImportDelete = nil }
        } message: { srv in
            Text("\(srv.name)\n\(srv.host)")
        }
    }

    private var importCard: some View {
        card {
            Text("Импорт из AmneziaVPN").font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
            Text("Список серверов AmneziaVPN лежит в закрытой памяти того приложения и снаружи не читается. Сделайте резервную копию (Настройки → резервная копия) и выберите файл здесь, либо вставьте ссылку vpn://.")
                .font(.callout).foregroundStyle(Theme.muted)
            Text("Полная копия содержит пароль SSH: он сохраняется только в Связке ключей этого Mac и используется для команд чтения (нагрузка, пиры, трафик). Ключи WireGuard и AmneziaWG не сохраняются. Клиентский .conf без пароля SSH добавит только адрес. Если этот хост уже есть в панели, цифры берутся оттуда.")
                .font(.callout).foregroundStyle(Theme.muted)
            HStack {
                Button("Выбрать файл") { showImporter = true }
                Button("Импортировать текст") { model.importText(paste) }
                    .disabled(paste.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            TextEditor(text: $paste)
                .font(.body.monospaced())
                .frame(minHeight: 72, maxHeight: 140)
                .scrollContentBackground(.hidden)
                .padding(6)
                .background(Theme.card2, in: RoundedRectangle(cornerRadius: 8))
                .overlay(alignment: .topLeading) {
                    if paste.isEmpty {
                        Text("Или вставьте vpn:// / JSON").foregroundStyle(Theme.muted).padding(10).allowsHitTesting(false)
                    }
                }
            if let message = model.importMessage {
                Text(message).font(.callout).foregroundStyle(Theme.text)
            }
            ForEach(model.settings.imported, id: \.id) { srv in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(srv.name).foregroundStyle(Theme.text)
                        Text("\(srv.user)@\(srv.host):\(srv.port) · \(srv.hasSshAuth ? "секрет SSH в Связке ключей" : "только адрес")")
                            .font(.caption).foregroundStyle(Theme.muted)
                    }
                    Spacer()
                    Button("Удалить") { pendingImportDelete = srv }
                }
            }
        }
    }

    private var intervalCard: some View {
        card {
            Text("Интервал обновления, пока приложение запущено").foregroundStyle(Theme.muted)
            HStack {
                ForEach(intervals, id: \.self) { sec in
                    Button("\(sec) с") { model.setInterval(sec) }
                        .buttonStyle(.bordered)
                        .tint(model.settings.intervalSec == sec ? Theme.indigo : Theme.muted)
                }
            }
        }
    }

    private var panelsCard: some View {
        card {
            HStack {
                Text("Панели").font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
                Spacer()
                Button("Добавить панель") {
                    editing = Endpoint(id: UUID().uuidString, label: "", baseUrl: "", token: "")
                    creating = true
                    formError = nil
                    testMsg = nil
                }
            }
            if model.settings.endpoints.isEmpty {
                Text("Пока ни одного адреса. Добавьте панель с компьютера, где запущен Amnezia Monitor.")
                    .foregroundStyle(Theme.muted)
            }
            ForEach(model.settings.endpoints, id: \.id) { ep in
                HStack(alignment: .center) {
                    Button {
                        model.setActive(ep.id)
                    } label: {
                        Image(systemName: model.settings.active?.id == ep.id ? "largecircle.fill.circle" : "circle")
                    }
                    .buttonStyle(.borderless)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(ep.label.isEmpty ? "Панель" : ep.label).foregroundStyle(Theme.text)
                        Text(ep.baseUrl).font(.caption).foregroundStyle(Theme.muted)
                        Text(ep.token.isEmpty ? "токен не задан" : "токен в Связке ключей").font(.caption2).foregroundStyle(Theme.muted)
                    }
                    Spacer()
                    Button("Изменить") {
                        editing = ep
                        creating = false
                        formError = nil
                        testMsg = nil
                    }
                    Button(role: .destructive) { pendingDelete = ep } label: {
                        Image(systemName: "trash")
                    }
                }
            }
            if let draft = editing {
                endpointForm(draft).id(draft.id)
            }
        }
    }

    private func endpointForm(_ initial: Endpoint) -> some View {
        EndpointForm(
            initial: initial,
            isNew: creating,
            error: formError,
            testMsg: testMsg,
            testing: testing,
            onTest: { ep in
                testing = true
                testMsg = nil
                formError = nil
                Task {
                    let msg = await model.testEndpoint(ep)
                    testMsg = msg
                    testing = false
                }
            },
            onSave: { ep in
                do {
                    let clean = Endpoint(
                        id: ep.id,
                        label: String(ep.label.trimmingCharacters(in: .whitespacesAndNewlines).prefix(40)),
                        baseUrl: try normalizeBaseUrl(ep.baseUrl),
                        token: try cleanToken(ep.token)
                    )
                    model.upsertEndpoint(clean, makeActive: creating || model.settings.active == nil)
                    editing = nil
                    formError = nil
                } catch let error as UserError {
                    formError = error.message
                } catch {
                    formError = "Проверьте адрес"
                }
            },
            onCancel: { editing = nil }
        )
    }

    private var helpCard: some View {
        card {
            Text("Как открыть данные с компьютера").font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
            Text("На компьютере с Amnezia Monitor: Настройки → Телефон → разрешить чтение в локальной сети. Скопируйте адрес вида http://192.168.1.104:8765 и токен. Этот Mac должен видеть тот компьютер по сети или через VPN. Если панель на этом же Mac слушает 127.0.0.1, токен не нужен.")
                .font(.callout).foregroundStyle(Theme.muted)
            Text("Канал HTTP без шифрования: не открывайте порт 8765 в интернет. Токен даёт чтение имён клиентов и доменов DNS. Пароли SSH настольного агента сюда не передаются. Запросы идут с заголовком X-Amnezia-Token, только GET: /api/health, /api/servers, /api/server.")
                .font(.callout).foregroundStyle(Theme.muted)
        }
    }

    private func card<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct EndpointForm: View {
    var initial: Endpoint
    var isNew: Bool
    var error: String?
    var testMsg: String?
    var testing: Bool
    var onTest: (Endpoint) -> Void
    var onSave: (Endpoint) -> Void
    var onCancel: () -> Void

    @State private var label: String
    @State private var url: String
    @State private var token: String
    @State private var showToken = false

    init(
        initial: Endpoint,
        isNew: Bool,
        error: String?,
        testMsg: String?,
        testing: Bool,
        onTest: @escaping (Endpoint) -> Void,
        onSave: @escaping (Endpoint) -> Void,
        onCancel: @escaping () -> Void
    ) {
        self.initial = initial
        self.isNew = isNew
        self.error = error
        self.testMsg = testMsg
        self.testing = testing
        self.onTest = onTest
        self.onSave = onSave
        self.onCancel = onCancel
        _label = State(initialValue: initial.label)
        _url = State(initialValue: initial.baseUrl)
        _token = State(initialValue: initial.token)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(isNew ? "Новая панель" : "Изменить панель").font(.headline).foregroundStyle(Theme.text)
            TextField("Название", text: $label, prompt: Text("Дом"))
            TextField("Адрес панели", text: $url, prompt: Text("http://192.168.1.104:8765"))
            HStack {
                if showToken {
                    TextField("Токен чтения", text: $token, prompt: Text("не нужен для 127.0.0.1"))
                } else {
                    SecureField("Токен чтения", text: $token, prompt: Text("не нужен для 127.0.0.1"))
                }
                Button(showToken ? "Скрыть" : "Показать") { showToken.toggle() }
                    .buttonStyle(.borderless)
            }
            if let error = error {
                Text(error).foregroundStyle(Theme.red).font(.callout)
            }
            if let testMsg = testMsg {
                Text(testMsg).foregroundStyle(Theme.text).font(.callout)
            }
            HStack {
                Button("Сохранить") { onSave(current) }
                Button(testing ? "Проверяю…" : "Проверить") { onTest(current) }
                    .disabled(testing)
                Button("Отмена", action: onCancel)
            }
        }
        .padding(12)
        .background(Theme.card2, in: RoundedRectangle(cornerRadius: 10))
    }

    private var current: Endpoint {
        Endpoint(id: initial.id, label: label, baseUrl: url.trimmingCharacters(in: .whitespacesAndNewlines), token: token)
    }
}
