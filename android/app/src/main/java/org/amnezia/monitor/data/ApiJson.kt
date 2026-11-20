package org.amnezia.monitor.data

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull

/** Разбор JSON панели. Неизвестный набор полей не ломает экран. */
object ApiJson {
    private val json = Json { ignoreUnknownKeys = true }

    fun errorMessage(text: String): String? = runCatching {
        val el = json.parseToJsonElement(text)
        if (el is JsonObject) el.str("error") else null
    }.getOrNull()

    fun parseHealth(text: String): Health {
        val o = asObject(text)
        return Health(ok = o.bool("ok") || o.str("app") == "amnezia-monitor", app = o.str("app"))
    }

    fun parseServers(text: String): List<ServerSummary> {
        val el = json.parseToJsonElement(text)
        if (el is JsonObject) throw apiError(el)
        if (el !is JsonArray) throw ApiException(0, "Неожиданный ответ панели")
        return el.map { parseSummary(it.jsonObject) }
    }

    fun parseDetail(text: String): ServerDetail {
        val o = asObject(text)
        if (o.containsKey("error") && o.str("name") == null) throw apiError(o)
        val curObj = o.obj("current")
        val current = if (curObj != null) parseCurrent(curObj) else Current(
            status = o.str("status") ?: "nodata",
            error = o.str("error"),
        )
        val seriesObj = o.obj("series")
        val totals = o.obj("totals")
        return ServerDetail(
            name = o.str("name") ?: "",
            host = o.str("host"),
            current = current,
            containers = o.arr("containers")?.map { c ->
                val co = c.jsonObject
                ContainerRow(
                    name = co.str("name") ?: "",
                    type = co.str("type"),
                    up = co.bool("up"),
                    status = co.str("status"),
                )
            }.orEmpty(),
            series = seriesObj?.let {
                Series(
                    cpu = it.nums("cpu"),
                    mem = it.nums("mem"),
                    online = it.nums("online"),
                    downBytes = it.nums("down_bytes"),
                    upBytes = it.nums("up_bytes"),
                )
            },
            users = o.arr("users")?.map { parseUser(it.jsonObject) }.orEmpty(),
            down24h = totals?.dbl("down_24h"),
            up24h = totals?.dbl("up_24h"),
            dns = parseDns(o.obj("dns")),
            events = o.arr("events")?.map { e ->
                val eo = e.jsonObject
                EventRow(ts = eo.dbl("ts") ?: 0.0, kind = eo.str("kind"), message = eo.str("message") ?: "")
            }.orEmpty(),
        )
    }

    private fun asObject(text: String): JsonObject {
        val el = json.parseToJsonElement(text)
        if (el !is JsonObject) throw ApiException(0, "Неожиданный ответ панели")
        return el
    }

    private fun apiError(o: JsonObject): ApiException =
        ApiException(0, o.str("error") ?: "Ошибка панели")

    private fun parseSummary(o: JsonObject) = ServerSummary(
        name = o.str("name") ?: "",
        host = o.str("host"),
        status = o.str("status") ?: "nodata",
        ageSec = o.dbl("age"),
        tcpMs = o.dbl("tcp_ms"),
        cpu = o.dbl("cpu"),
        mem = o.dbl("mem"),
        peersOnline = o.int("peers_online"),
        peersTotal = o.int("peers_total"),
        rxBps = o.dbl("rx_bps"),
        txBps = o.dbl("tx_bps"),
        error = o.str("error"),
    )

    private fun parseCurrent(o: JsonObject) = Current(
        status = o.str("status") ?: "nodata",
        error = o.str("error"),
        ageSec = o.dbl("age"),
        tcpMs = o.dbl("tcp_ms"),
        pingMs = o.dbl("ping_ms"),
        availability = o.dbl("availability"),
        latencyAvg = o.dbl("latency_avg"),
        uptimeSec = o.dbl("uptime_s"),
        load1 = o.dbl("load1"),
        nproc = o.int("nproc"),
        cpu = o.dbl("cpu"),
        mem = o.dbl("mem"),
        memUsed = o.dbl("mem_used"),
        memTotal = o.dbl("mem_total"),
        rxBps = o.dbl("rx_bps"),
        txBps = o.dbl("tx_bps"),
        stale = o.bool("stale"),
    )

    private fun parseUser(o: JsonObject) = UserRow(
        client = o.str("client") ?: "без имени",
        pubkey = o.str("pubkey"),
        container = o.str("container"),
        online = o.bool("online"),
        handshakeAge = o.dbl("handshake_age"),
        endpoint = o.str("endpoint"),
        rateDown = o.dbl("rate_down"),
        rateUp = o.dbl("rate_up"),
        spark = o.nums("spark").map { it ?: 0.0 },
        down24h = o.dbl("down_24h") ?: 0.0,
        up24h = o.dbl("up_24h") ?: 0.0,
        downTotal = o.dbl("down_total"),
        upTotal = o.dbl("up_total"),
    )

    private fun parseDns(o: JsonObject?): DnsInfo {
        if (o == null) return DnsInfo("nodata", "Данных пока нет", null, emptyList(), emptyList())
        val top = o.arr("top")?.map { siteOf(it) }.orEmpty()
        val per = o.arr("per_user")?.map { u ->
            val uo = u.jsonObject
            DnsUser(
                client = uo.str("client") ?: "",
                total = uo.int("total") ?: 0,
                top = uo.arr("top")?.map { siteOf(it) }.orEmpty(),
            )
        }.orEmpty()
        return DnsInfo(
            status = o.str("status") ?: "nodata",
            note = o.str("note"),
            queries = o.int("queries"),
            top = top,
            perUser = per,
        )
    }

    private fun siteOf(el: JsonElement): DnsSite {
        val o = el.jsonObject
        return DnsSite(site = o.str("site") ?: "", count = o.int("n") ?: 0)
    }
}

private fun JsonObject.obj(key: String): JsonObject? = this[key] as? JsonObject

private fun JsonObject.arr(key: String): JsonArray? = this[key] as? JsonArray

private fun JsonObject.prim(key: String): JsonPrimitive? {
    val v = this[key] ?: return null
    if (v is JsonNull || v !is JsonPrimitive) return null
    return v
}

private fun JsonObject.str(key: String): String? {
    val p = prim(key) ?: return null
    if (!p.isString) return null
    return p.contentOrNull
}

private fun JsonObject.dbl(key: String): Double? {
    val p = prim(key) ?: return null
    if (p.isString) return p.content.toDoubleOrNull()
    return p.doubleOrNull ?: p.longOrNull?.toDouble()
}

private fun JsonObject.int(key: String): Int? = dbl(key)?.toInt()

private fun JsonObject.bool(key: String): Boolean = prim(key)?.booleanOrNull == true

private fun JsonObject.nums(key: String): List<Double?> {
    val arr = this[key] as? JsonArray ?: return emptyList()
    return arr.map { el ->
        when (el) {
            is JsonNull -> null
            is JsonPrimitive -> when {
                el.isString -> el.content.toDoubleOrNull()
                else -> el.doubleOrNull ?: el.longOrNull?.toDouble()
            }
            else -> null
        }
    }
}
