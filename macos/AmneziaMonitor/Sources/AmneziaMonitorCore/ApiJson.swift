import Foundation

/// Разбор JSON панели. Неизвестный набор полей не ломает экран.
public enum ApiJson {
    public static func errorMessage(_ text: String) -> String? {
        guard let any = try? JSONBox.parse(text), let obj = any as? [String: Any] else { return nil }
        return JSONBox.str(obj, "error")
    }

    public static func parseHealth(_ text: String) throws -> Health {
        let o = try JSONBox.object(text)
        return Health(ok: JSONBox.bool(o, "ok") || JSONBox.str(o, "app") == "amnezia-monitor", app: JSONBox.str(o, "app"))
    }

    public static func parseServers(_ text: String) throws -> [ServerSummary] {
        let el = try JSONBox.parse(text)
        if let obj = el as? [String: Any] {
            throw apiError(obj)
        }
        guard let arr = el as? [Any] else {
            throw ApiException(code: 0, message: "Неожиданный ответ панели")
        }
        return try arr.map { item in
            guard let obj = item as? [String: Any] else {
                throw ApiException(code: 0, message: "Неожиданный ответ панели")
            }
            return parseSummary(obj)
        }
    }

    public static func parseDetail(_ text: String) throws -> ServerDetail {
        let o = try JSONBox.object(text)
        if o["error"] != nil && JSONBox.str(o, "name") == nil {
            throw apiError(o)
        }
        let current: Current
        if let curObj = JSONBox.obj(o, "current") {
            current = parseCurrent(curObj)
        } else {
            current = Current(status: JSONBox.str(o, "status") ?? "nodata", error: JSONBox.str(o, "error"))
        }
        let seriesObj = JSONBox.obj(o, "series")
        let totals = JSONBox.obj(o, "totals")
        let containers: [ContainerRow] = (JSONBox.arr(o, "containers") ?? []).compactMap { item in
            guard let co = item as? [String: Any] else { return nil }
            return ContainerRow(
                name: JSONBox.str(co, "name") ?? "",
                type: JSONBox.str(co, "type"),
                up: JSONBox.bool(co, "up"),
                status: JSONBox.str(co, "status")
            )
        }
        let series = seriesObj.map { box in
            Series(
                cpu: JSONBox.nums(box, "cpu"),
                mem: JSONBox.nums(box, "mem"),
                online: JSONBox.nums(box, "online"),
                downBytes: JSONBox.nums(box, "down_bytes"),
                upBytes: JSONBox.nums(box, "up_bytes")
            )
        }
        let users = (JSONBox.arr(o, "users") ?? []).compactMap { item -> UserRow? in
            guard let uo = item as? [String: Any] else { return nil }
            return parseUser(uo)
        }
        let dns = parseDns(JSONBox.obj(o, "dns"))
        let events: [EventRow] = (JSONBox.arr(o, "events") ?? []).compactMap { item in
            guard let eo = item as? [String: Any] else { return nil }
            return EventRow(ts: JSONBox.dbl(eo, "ts") ?? 0, kind: JSONBox.str(eo, "kind"), message: JSONBox.str(eo, "message") ?? "")
        }
        return ServerDetail(
            name: JSONBox.str(o, "name") ?? "",
            host: JSONBox.str(o, "host"),
            current: current,
            containers: containers,
            series: series,
            users: users,
            down24h: totals.flatMap { JSONBox.dbl($0, "down_24h") },
            up24h: totals.flatMap { JSONBox.dbl($0, "up_24h") },
            dns: dns,
            events: events
        )
    }

    private static func apiError(_ o: [String: Any]) -> ApiException {
        ApiException(code: 0, message: JSONBox.str(o, "error") ?? "Ошибка панели")
    }

    private static func parseSummary(_ o: [String: Any]) -> ServerSummary {
        ServerSummary(
            name: JSONBox.str(o, "name") ?? "",
            host: JSONBox.str(o, "host"),
            status: JSONBox.str(o, "status") ?? "nodata",
            ageSec: JSONBox.dbl(o, "age"),
            tcpMs: JSONBox.dbl(o, "tcp_ms"),
            cpu: JSONBox.dbl(o, "cpu"),
            mem: JSONBox.dbl(o, "mem"),
            peersOnline: JSONBox.int(o, "peers_online"),
            peersTotal: JSONBox.int(o, "peers_total"),
            rxBps: JSONBox.dbl(o, "rx_bps"),
            txBps: JSONBox.dbl(o, "tx_bps"),
            error: JSONBox.str(o, "error")
        )
    }

    private static func parseCurrent(_ o: [String: Any]) -> Current {
        Current(
            status: JSONBox.str(o, "status") ?? "nodata",
            error: JSONBox.str(o, "error"),
            ageSec: JSONBox.dbl(o, "age"),
            tcpMs: JSONBox.dbl(o, "tcp_ms"),
            pingMs: JSONBox.dbl(o, "ping_ms"),
            availability: JSONBox.dbl(o, "availability"),
            latencyAvg: JSONBox.dbl(o, "latency_avg"),
            uptimeSec: JSONBox.dbl(o, "uptime_s"),
            load1: JSONBox.dbl(o, "load1"),
            nproc: JSONBox.int(o, "nproc"),
            cpu: JSONBox.dbl(o, "cpu"),
            mem: JSONBox.dbl(o, "mem"),
            memUsed: JSONBox.dbl(o, "mem_used"),
            memTotal: JSONBox.dbl(o, "mem_total"),
            rxBps: JSONBox.dbl(o, "rx_bps"),
            txBps: JSONBox.dbl(o, "tx_bps"),
            stale: JSONBox.bool(o, "stale")
        )
    }

    private static func parseUser(_ o: [String: Any]) -> UserRow {
        UserRow(
            client: JSONBox.str(o, "client") ?? "без имени",
            pubkey: JSONBox.str(o, "pubkey"),
            container: JSONBox.str(o, "container"),
            online: JSONBox.bool(o, "online"),
            handshakeAge: JSONBox.dbl(o, "handshake_age"),
            endpoint: JSONBox.str(o, "endpoint"),
            rateDown: JSONBox.dbl(o, "rate_down"),
            rateUp: JSONBox.dbl(o, "rate_up"),
            spark: JSONBox.nums(o, "spark").map { $0 ?? 0 },
            down24h: JSONBox.dbl(o, "down_24h") ?? 0,
            up24h: JSONBox.dbl(o, "up_24h") ?? 0,
            downTotal: JSONBox.dbl(o, "down_total"),
            upTotal: JSONBox.dbl(o, "up_total")
        )
    }

    private static func parseDns(_ o: [String: Any]?) -> DnsInfo {
        guard let o = o else {
            return DnsInfo(status: "nodata", note: "Данных пока нет", queries: nil, top: [], perUser: [])
        }
        let top = (JSONBox.arr(o, "top") ?? []).compactMap(siteOf)
        let per = (JSONBox.arr(o, "per_user") ?? []).compactMap { item -> DnsUser? in
            guard let uo = item as? [String: Any] else { return nil }
            return DnsUser(
                client: JSONBox.str(uo, "client") ?? "",
                total: JSONBox.int(uo, "total") ?? 0,
                top: (JSONBox.arr(uo, "top") ?? []).compactMap(siteOf)
            )
        }
        return DnsInfo(
            status: JSONBox.str(o, "status") ?? "nodata",
            note: JSONBox.str(o, "note"),
            queries: JSONBox.int(o, "queries"),
            top: top,
            perUser: per
        )
    }

    private static func siteOf(_ el: Any) -> DnsSite? {
        guard let o = el as? [String: Any] else { return nil }
        return DnsSite(site: JSONBox.str(o, "site") ?? "", count: JSONBox.int(o, "n") ?? 0)
    }
}
