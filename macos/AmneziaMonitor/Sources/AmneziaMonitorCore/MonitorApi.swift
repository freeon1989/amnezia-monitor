import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

private final class RedirectBlocker: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

/// Только чтение JSON панели. Никаких POST: настройки и SSH остаются на компьютере.
/// Редиректы выключены, чтобы токен не уехал на другой хост.
public final class MonitorApi {
    private let session: URLSession
    private let redirects: RedirectBlocker

    public init() {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 20
        cfg.timeoutIntervalForResource = 25
        cfg.httpCookieAcceptPolicy = .never
        cfg.httpShouldSetCookies = false
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        let blocker = RedirectBlocker()
        redirects = blocker
        session = URLSession(configuration: cfg, delegate: blocker, delegateQueue: nil)
    }

    public func health(_ ep: Endpoint) async throws -> Health {
        try ApiJson.parseHealth(await get(ep, "/api/health"))
    }

    public func servers(_ ep: Endpoint) async throws -> [ServerSummary] {
        try ApiJson.parseServers(await get(ep, "/api/servers"))
    }

    public func server(_ ep: Endpoint, name: String, hours: Int) async throws -> ServerDetail {
        var parts = URLComponents()
        parts.queryItems = [
            URLQueryItem(name: "name", value: name),
            URLQueryItem(name: "hours", value: String(min(168, max(1, hours)))),
        ]
        let query = parts.percentEncodedQuery ?? ""
        return try ApiJson.parseDetail(await get(ep, "/api/server?\(query)"))
    }

    private func get(_ ep: Endpoint, _ path: String) async throws -> String {
        let urlString = ep.baseUrl.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + path
        guard let url = URL(string: urlString) else {
            throw UserError("Не удалось разобрать адрес")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.timeoutInterval = 20
        let token = ep.token.trimmingCharacters(in: .whitespacesAndNewlines)
        if !token.isEmpty {
            req.setValue(token, forHTTPHeaderField: "X-Amnezia-Token")
        }
        let (data, response) = try await session.data(for: req)
        let text = String(data: data, encoding: .utf8) ?? ""
        guard let http = response as? HTTPURLResponse else {
            throw ApiException(code: 0, message: "Неожиданный ответ панели")
        }
        if (300...399).contains(http.statusCode) {
            throw ApiException(code: http.statusCode, message: "Панель перенаправила запрос. Укажите конечный адрес.")
        }
        if !(200...299).contains(http.statusCode) {
            let fromBody = ApiJson.errorMessage(text)
            let message: String
            switch http.statusCode {
            case 401, 403: message = fromBody ?? "Неверный токен"
            case 404: message = fromBody ?? "Не найдено"
            default: message = fromBody ?? "Ошибка \(http.statusCode)"
            }
            throw ApiException(code: http.statusCode, message: message)
        }
        return text
    }
}
