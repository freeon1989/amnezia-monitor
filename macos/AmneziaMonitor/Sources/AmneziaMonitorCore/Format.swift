import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public func statusLabel(_ status: String) -> String {
    switch status {
    case "up": return "в сети"
    case "down": return "нет связи"
    default: return "нет данных"
    }
}

public func fmtPct(_ v: Double?) -> String {
    guard let v = v else { return "—" }
    return "\(Int(v.rounded()))%"
}

public func fmtMs(_ v: Double?) -> String {
    guard let v = v else { return "—" }
    return "\(Int(v.rounded())) мс"
}

public func fmtAgo(_ seconds: Double?) -> String {
    guard let seconds = seconds else { return "—" }
    let s = max(0, Int(seconds))
    if s < 5 { return "только что" }
    if s < 60 { return "\(s) с назад" }
    if s < 3600 { return "\(s / 60) мин назад" }
    if s < 86400 { return "\(s / 3600) ч назад" }
    return "\(s / 86400) д назад"
}

/// Длительность, не «назад». Как fmtD на панели.
public func fmtDuration(_ seconds: Double?) -> String {
    guard let seconds = seconds else { return "—" }
    let s = max(0, Int(seconds))
    if s < 60 { return "\(s) с" }
    let d = s / 86400
    let h = (s % 86400) / 3600
    let m = (s % 3600) / 60
    if d > 0 { return "\(d) д \(h) ч" }
    if h > 0 { return "\(h) ч \(m) мин" }
    return "\(m) мин"
}

public func fmtBytes(_ n: Double?, perSec: Bool = false) -> String {
    guard let n = n else { return "—" }
    let units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    var v = n
    var i = 0
    while v >= 1024 && i < units.count - 1 {
        v /= 1024
        i += 1
    }
    let num: String
    if i == 0 {
        num = String(Int(v.rounded()))
    } else if v < 10 {
        num = oneDecimal(v)
    } else {
        num = String(Int(v.rounded()))
    }
    return num + " " + units[i] + (perSec ? "/с" : "")
}

/// Скорость из байт/с, как fmtR на панели: в бит/с.
public func fmtRate(_ bytesPerSec: Double?) -> String {
    guard let bytesPerSec = bytesPerSec else { return "—" }
    let units = ["бит/с", "Кбит/с", "Мбит/с", "Гбит/с"]
    var v = bytesPerSec * 8
    var i = 0
    while v >= 1000 && i < units.count - 1 {
        v /= 1000
        i += 1
    }
    let num = (v < 10 && i > 0) ? oneDecimal(v) : String(Int(v.rounded()))
    return "\(num) \(units[i])"
}

private func oneDecimal(_ v: Double) -> String {
    let tenths = Int((v * 10).rounded())
    let sign = tenths < 0 ? "-" : ""
    let absTenths = abs(tenths)
    return "\(sign)\(absTenths / 10).\(absTenths % 10)"
}

public func fmtClock(_ epochMs: Int64) -> String {
    let date = Date(timeIntervalSince1970: TimeInterval(epochMs) / 1000)
    return FormatClock.clock.string(from: date)
}

public func fmtEventTime(_ epochSec: Double) -> String {
    let date = Date(timeIntervalSince1970: epochSec)
    return FormatClock.event.string(from: date)
}

private enum FormatClock {
    static let clock: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone.current
        f.dateFormat = "HH:mm:ss"
        return f
    }()
    static let event: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone.current
        f.dateFormat = "dd.MM HH:mm"
        return f
    }()
}

public func cleanToken(_ raw: String) throws -> String {
    let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if t.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7F }) {
        throw UserError("Токен содержит недопустимые символы")
    }
    if t.count > 256 {
        throw UserError("Токен слишком длинный")
    }
    return t
}

/// Адрес панели без логина, пароля и query. Токен хранится отдельно.
public func normalizeBaseUrl(_ raw: String) throws -> String {
    var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if s.isEmpty {
        throw UserError("Укажите адрес панели")
    }
    if !s.contains("://") {
        s = "http://" + s
    }
    guard let uri = URLComponents(string: s) else {
        throw UserError("Не удалось разобрать адрес")
    }
    let scheme = (uri.scheme ?? "").lowercased()
    if scheme != "http" && scheme != "https" {
        throw UserError("Только http или https")
    }
    if uri.user != nil || uri.password != nil || (uri.percentEncodedUser != nil) || (uri.percentEncodedPassword != nil) {
        throw UserError("Не указывайте логин или пароль в адресе")
    }
    if uri.query != nil || uri.fragment != nil {
        throw UserError("Токен вводится отдельно, не в адресе")
    }
    guard let host = uri.host, !host.isEmpty else {
        throw UserError("В адресе нет хоста")
    }
    let hostFmt = host.contains(":") ? "[\(host)]" : host
    let port = uri.port.map { ":\($0)" } ?? ""
    let path = (uri.percentEncodedPath).replacingOccurrences(of: "/+$", with: "", options: .regularExpression)
    return "\(scheme)://\(hostFmt)\(port)\(path)"
}

public func friendlyError(_ error: Error) -> String {
    if let api = error as? ApiException {
        switch api.code {
        case 401, 403:
            return "Неверный токен. Скопируйте его из «Настройки → Телефон» на компьютере."
        case 404:
            return "Такого сервера нет в панели."
        default:
            return api.message.isEmpty ? "Панель вернула ошибку" : api.message
        }
    }
    if let user = error as? UserError {
        return user.message
    }
    let ns = error as NSError
    if ns.domain == NSURLErrorDomain {
        switch ns.code {
        case NSURLErrorCannotFindHost, NSURLErrorDNSLookupFailed:
            return "Адрес панели не найден. Проверьте URL."
        case NSURLErrorTimedOut:
            return "Панель не ответила вовремя."
        case NSURLErrorCannotConnectToHost, NSURLErrorNetworkConnectionLost, NSURLErrorNotConnectedToInternet:
            return "Нет связи с панелью. Amnezia Monitor должен быть запущен, а этот Mac — в той же сети или VPN."
        case NSURLErrorSecureConnectionFailed, NSURLErrorServerCertificateUntrusted,
             NSURLErrorServerCertificateHasBadDate, NSURLErrorServerCertificateHasUnknownRoot:
            return "Ошибка HTTPS. Для панели без сертификата укажите адрес http://."
        default:
            break
        }
    }
    let msg = error.localizedDescription
    let low = msg.lowercased()
    if low.contains("connection refused") || low.contains("could not connect") {
        return "Нет связи с панелью. Проверьте адрес, порт и что Amnezia Monitor слушает сеть."
    }
    return "Не удалось связаться с панелью"
}
