package org.amnezia.monitor.data

import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * Только чтение JSON панели. Никаких POST: настройки и SSH остаются на компьютере.
 * Редиректы выключены, чтобы токен не уехал на другой хост.
 */
class MonitorApi(
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .followRedirects(false)
        .followSslRedirects(false)
        .build(),
) {
    fun health(ep: Endpoint): Health = ApiJson.parseHealth(get(ep, "/api/health"))

    fun servers(ep: Endpoint): List<ServerSummary> = ApiJson.parseServers(get(ep, "/api/servers"))

    fun server(ep: Endpoint, name: String, hours: Int): ServerDetail {
        val q = URLEncoder.encode(name, Charsets.UTF_8.name())
        val h = hours.coerceIn(1, 168)
        return ApiJson.parseDetail(get(ep, "/api/server?name=$q&hours=$h"))
    }

    private fun get(ep: Endpoint, path: String): String {
        val url = ep.baseUrl.trimEnd('/') + path
        val req = Request.Builder().url(url).header("Accept", "application/json")
        val token = ep.token.trim()
        if (token.isNotEmpty()) req.header("X-Amnezia-Token", token)
        client.newCall(req.build()).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (resp.code in 300..399) {
                throw ApiException(resp.code, "Панель перенаправила запрос. Укажите конечный адрес.")
            }
            if (!resp.isSuccessful) {
                val fromBody = runCatching { ApiJson.errorMessage(text) }.getOrNull()
                val message = when (resp.code) {
                    401, 403 -> fromBody ?: "Неверный токен"
                    404 -> fromBody ?: "Не найдено"
                    else -> fromBody ?: "Ошибка ${resp.code}"
                }
                throw ApiException(resp.code, message)
            }
            return text
        }
    }
}
