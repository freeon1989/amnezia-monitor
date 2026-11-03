package org.amnezia.monitor.data

import kotlinx.serialization.Serializable

/** Одна сохранённая панель. Токен — секрет чтения, не пароль SSH. */
@Serializable
data class Endpoint(
    val id: String,
    val label: String = "",
    val baseUrl: String,
    val token: String = "",
)

@Serializable
data class AppSettings(
    val endpoints: List<Endpoint> = emptyList(),
    val activeId: String? = null,
    val intervalSec: Int = 30,
) {
    val active: Endpoint?
        get() = endpoints.firstOrNull { it.id == activeId } ?: endpoints.firstOrNull()

    fun normalized(): AppSettings {
        val list = endpoints.take(20)
        val activeOk = list.firstOrNull { it.id == activeId }?.id ?: list.firstOrNull()?.id
        return copy(endpoints = list, activeId = activeOk, intervalSec = intervalSec.coerceIn(15, 300))
    }
}

data class ServerSummary(
    val name: String,
    val host: String?,
    val status: String,
    val ageSec: Double?,
    val tcpMs: Double?,
    val cpu: Double?,
    val mem: Double?,
    val peersOnline: Int?,
    val peersTotal: Int?,
    val rxBps: Double?,
    val txBps: Double?,
    val error: String?,
)

data class Current(
    val status: String,
    val error: String?,
    val ageSec: Double? = null,
    val tcpMs: Double? = null,
    val pingMs: Double? = null,
    val availability: Double? = null,
    val latencyAvg: Double? = null,
    val uptimeSec: Double? = null,
    val load1: Double? = null,
    val nproc: Int? = null,
    val cpu: Double? = null,
    val mem: Double? = null,
    val memUsed: Double? = null,
    val memTotal: Double? = null,
    val rxBps: Double? = null,
    val txBps: Double? = null,
    val stale: Boolean = false,
)

data class Series(
    val cpu: List<Double?>,
    val mem: List<Double?>,
    val online: List<Double?>,
    val downBytes: List<Double?>,
    val upBytes: List<Double?>,
)

data class ContainerRow(
    val name: String,
    val type: String?,
    val up: Boolean,
    val status: String?,
)

data class UserRow(
    val client: String,
    val pubkey: String?,
    val container: String?,
    val online: Boolean,
    val handshakeAge: Double?,
    val endpoint: String?,
    val rateDown: Double?,
    val rateUp: Double?,
    val spark: List<Double>,
    val down24h: Double,
    val up24h: Double,
    val downTotal: Double?,
    val upTotal: Double?,
)

data class DnsSite(val site: String, val count: Int)

data class DnsUser(val client: String, val total: Int, val top: List<DnsSite>)

data class DnsInfo(
    val status: String,
    val note: String?,
    val queries: Int?,
    val top: List<DnsSite>,
    val perUser: List<DnsUser>,
)

data class EventRow(val ts: Double, val kind: String?, val message: String)

data class ServerDetail(
    val name: String,
    val host: String?,
    val current: Current,
    val containers: List<ContainerRow>,
    val series: Series?,
    val users: List<UserRow>,
    val down24h: Double?,
    val up24h: Double?,
    val dns: DnsInfo,
    val events: List<EventRow>,
)

data class Health(val ok: Boolean, val app: String?)

class ApiException(val code: Int, message: String) : Exception(message)
