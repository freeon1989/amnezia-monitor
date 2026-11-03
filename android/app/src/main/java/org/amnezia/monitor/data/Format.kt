package org.amnezia.monitor.data

import java.net.URI
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.roundToInt

private val clockFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm:ss")
private val eventFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("dd.MM HH:mm")

fun statusLabel(status: String): String = when (status) {
    "up" -> "в сети"
    "down" -> "нет связи"
    else -> "нет данных"
}

fun fmtPct(v: Double?): String = if (v == null) "—" else "${v.roundToInt()}%"

fun fmtMs(v: Double?): String = if (v == null) "—" else "${v.roundToInt()} мс"

fun fmtAgo(seconds: Double?): String {
    if (seconds == null) return "—"
    val s = seconds.toInt().coerceAtLeast(0)
    return when {
        s < 5 -> "только что"
        s < 60 -> "$s с назад"
        s < 3600 -> "${s / 60} мин назад"
        s < 86400 -> "${s / 3600} ч назад"
        else -> "${s / 86400} д назад"
    }
}

/** Как fmtD в панели: длительность, не «назад». */
fun fmtDuration(seconds: Double?): String {
    if (seconds == null) return "—"
    val s = seconds.toInt().coerceAtLeast(0)
    if (s < 60) return "$s с"
    val d = s / 86400
    val h = (s % 86400) / 3600
    val m = (s % 3600) / 60
    return when {
        d > 0 -> "$d д $h ч"
        h > 0 -> "$h ч $m мин"
        else -> "$m мин"
    }
}

fun fmtBytes(n: Double?, perSec: Boolean = false): String {
    if (n == null) return "—"
    val units = arrayOf("Б", "КБ", "МБ", "ГБ", "ТБ")
    var v = n
    var i = 0
    while (v >= 1024 && i < units.lastIndex) {
        v /= 1024.0
        i++
    }
    val num = when {
        i == 0 -> v.roundToInt().toString()
        v < 10 -> String.format(Locale.US, "%.1f", v)
        else -> v.roundToInt().toString()
    }
    return num + " " + units[i] + if (perSec) "/с" else ""
}

/** Скорость из байт/с, как fmtR на панели: в бит/с. */
fun fmtRate(bytesPerSec: Double?): String {
    if (bytesPerSec == null) return "—"
    val units = arrayOf("бит/с", "Кбит/с", "Мбит/с", "Гбит/с")
    var v = bytesPerSec * 8.0
    var i = 0
    while (v >= 1000 && i < units.lastIndex) {
        v /= 1000.0
        i++
    }
    val num = if (v < 10 && i > 0) String.format(Locale.US, "%.1f", v) else v.roundToInt().toString()
    return "$num ${units[i]}"
}

fun fmtClock(epochMs: Long): String =
    Instant.ofEpochMilli(epochMs).atZone(ZoneId.systemDefault()).format(clockFmt)

fun fmtEventTime(epochSec: Double): String =
    Instant.ofEpochSecond(epochSec.toLong()).atZone(ZoneId.systemDefault()).format(eventFmt)

fun cleanToken(raw: String): String {
    val t = raw.trim()
    if (t.any { it.code < 0x20 || it.code == 0x7F }) {
        throw IllegalArgumentException("Токен содержит недопустимые символы")
    }
    if (t.length > 256) throw IllegalArgumentException("Токен слишком длинный")
    return t
}

/** Адрес панели без логина, пароля и query. Токен хранится отдельно. */
fun normalizeBaseUrl(raw: String): String {
    var s = raw.trim()
    if (s.isEmpty()) throw IllegalArgumentException("Укажите адрес панели")
    if (!s.contains("://")) s = "http://$s"
    val uri = try {
        URI(s)
    } catch (_: Exception) {
        throw IllegalArgumentException("Не удалось разобрать адрес")
    }
    val scheme = uri.scheme?.lowercase()
    if (scheme != "http" && scheme != "https") {
        throw IllegalArgumentException("Только http или https")
    }
    if (!uri.userInfo.isNullOrBlank()) {
        throw IllegalArgumentException("Не указывайте логин или пароль в адресе")
    }
    if (!uri.rawQuery.isNullOrBlank() || uri.rawFragment != null) {
        throw IllegalArgumentException("Токен вводится отдельно, не в адресе")
    }
    val host = uri.host?.takeIf { it.isNotBlank() }
        ?: throw IllegalArgumentException("В адресе нет хоста")
    val hostFmt = if (host.contains(':')) "[$host]" else host
    val port = if (uri.port == -1) "" else ":${uri.port}"
    val path = (uri.rawPath ?: "").trimEnd('/')
    return "$scheme://$hostFmt$port$path"
}
