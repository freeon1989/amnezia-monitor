package org.amnezia.monitor.data

import android.content.Context
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * GeoIP для оформления (по желанию, выключено по умолчанию): страна по IP сервера через ip-api.com.
 * Отправляется только IP-адрес (без паролей и ключей), ответ хранится 30 дней.
 */
class GeoIp(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("geoip_cache", Context.MODE_PRIVATE)
    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .followRedirects(false)
        .build()

    /** Из кэша: код страны, "" (не определилась) или null (нет в кэше / устарело). */
    fun cached(ip: String, now: Long = System.currentTimeMillis()): String? {
        val raw = prefs.getString(ip, null) ?: return null
        val (cc, t) = raw.split("|", limit = 2).let { it[0] to (it.getOrNull(1)?.toLongOrNull() ?: 0L) }
        return if (now - t < TTL_MS) cc else null
    }

    /** Сетевой запрос (вызывать в IO). Ошибки сети — тихо, просто без страны. */
    fun lookup(ip: String): String? {
        if (!CountryDetect.isPublicIpv4(ip)) return null
        cached(ip)?.let { return it }
        return try {
            val req = Request.Builder().url("http://ip-api.com/json/$ip?fields=status,countryCode").build()
            client.newCall(req).execute().use { resp ->
                val o = JSONObject(resp.body?.string().orEmpty())
                val cc = if (o.optString("status") == "success") CountryDetect.normalize(o.optString("countryCode")) else ""
                prefs.edit().putString(ip, "$cc|${System.currentTimeMillis()}").apply()
                cc
            }
        } catch (_: Exception) {
            null
        }
    }

    companion object {
        const val TTL_MS = 30L * 24 * 3600 * 1000
    }
}
