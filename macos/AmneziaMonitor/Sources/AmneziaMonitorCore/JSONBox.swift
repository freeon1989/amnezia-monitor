import CoreFoundation
import Foundation

/// Разбор JSON без строгой схемы: лишние поля игнорируются, null не ломает экран.
enum JSONBox {
    static func object(_ text: String) throws -> [String: Any] {
        let any = try parse(text)
        guard let obj = any as? [String: Any] else {
            throw ApiException(code: 0, message: "Неожиданный ответ панели")
        }
        return obj
    }

    static func parse(_ text: String) throws -> Any {
        guard let data = text.data(using: .utf8) else {
            throw ApiException(code: 0, message: "Неожиданный ответ панели")
        }
        do {
            return try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        } catch {
            throw ApiException(code: 0, message: "Неожиданный ответ панели")
        }
    }

    static func str(_ o: [String: Any], _ key: String) -> String? {
        guard let v = o[key], !(v is NSNull) else { return nil }
        if let s = v as? String { return s }
        return nil
    }

    static func dbl(_ o: [String: Any], _ key: String) -> Double? {
        guard let v = o[key], !(v is NSNull) else { return nil }
        if let s = v as? String { return Double(s) }
        if isBoolean(v) { return nil }
        if let n = v as? NSNumber { return n.doubleValue }
        if let i = v as? Int { return Double(i) }
        if let d = v as? Double { return d }
        return nil
    }

    static func int(_ o: [String: Any], _ key: String) -> Int? {
        dbl(o, key).map { Int($0) }
    }

    static func bool(_ o: [String: Any], _ key: String) -> Bool {
        guard let v = o[key] else { return false }
        if let b = v as? Bool { return b }
        if isBoolean(v), let n = v as? NSNumber { return n.boolValue }
        return false
    }

    static func obj(_ o: [String: Any], _ key: String) -> [String: Any]? {
        o[key] as? [String: Any]
    }

    static func arr(_ o: [String: Any], _ key: String) -> [Any]? {
        o[key] as? [Any]
    }

    static func nums(_ o: [String: Any], _ key: String) -> [Double?] {
        guard let arr = o[key] as? [Any] else { return [] }
        return arr.map { el in
            if el is NSNull { return nil }
            if let s = el as? String { return Double(s) }
            if isBoolean(el) { return nil }
            if let n = el as? NSNumber { return n.doubleValue }
            if let i = el as? Int { return Double(i) }
            if let d = el as? Double { return d }
            return nil
        }
    }

    static func isBoolean(_ value: Any) -> Bool {
        guard let n = value as? NSNumber else {
            return value is Bool
        }
        if CFGetTypeID(n) == CFBooleanGetTypeID() {
            return true
        }
        // На Linux Foundation булево иногда приходит как NSNumber с objCType "c"/"B".
        let t = String(cString: n.objCType)
        if t == "c" || t == "B" {
            let d = n.doubleValue
            if d != 0 && d != 1 { return false }
            // Целые 0/1 тоже имеют тип, похожий на char. Отличить их надёжно нельзя,
            // поэтому булево признаём только через CFBoolean или Swift.Bool.
            return false
        }
        return false
    }
}
