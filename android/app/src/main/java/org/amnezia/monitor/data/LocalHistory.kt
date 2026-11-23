package org.amnezia.monitor.data

import java.io.File
import java.util.Locale
import kotlin.math.roundToLong

/**
 * История опросов серверов, которые телефон опрашивает сам по SSH (импорт из AmneziaVPN).
 * Для серверов панели ПК историю хранит сама панель (SQLite), здесь — только для прямых.
 *
 * Хранится в файле приложения: id сервера (случайный) и числа — без адресов, имён и паролей.
 * Пишется, пока приложение открыто и опрашивает серверы (фоновой службы нет).
 */
class LocalHistory(
    private val file: File?,
    private val keepSec: Long = 7 * 86_400L,
    /** Чаще одного замера в minGapSec не храним: ручные обновления сливаются с последним. */
    private val minGapSec: Long = 45,
) {
    data class Sample(
        val ts: Long,
        val up: Boolean,
        val tcpMs: Double? = null,
        val pingMs: Double? = null,
        val cpu: Double? = null,
        val mem: Double? = null,
        val online: Int? = null,
        /** Байты к клиентам (tx сервера) и от клиентов (rx) за время с прошлого опроса. */
        val downBytes: Double? = null,
        val upBytes: Double? = null,
    )

    private val data = mutableMapOf<String, MutableList<Sample>>()
    private var loaded = false
    private var dirty = false
    private var savedAt = 0L

    @Synchronized
    fun record(id: String, s: Sample) {
        load()
        val list = data.getOrPut(id) { mutableListOf() }
        val last = list.lastOrNull()
        if (last != null && s.ts < last.ts) return
        if (last != null && s.ts - last.ts < minGapSec) {
            list[list.size - 1] = s.copy(
                downBytes = sum(last.downBytes, s.downBytes),
                upBytes = sum(last.upBytes, s.upBytes),
            )
        } else {
            list.add(s)
        }
        val cut = s.ts - keepSec
        while (list.isNotEmpty() && list[0].ts < cut) list.removeAt(0)
        dirty = true
    }

    @Synchronized
    fun samples(id: String): List<Sample> {
        load()
        return data[id]?.toList().orEmpty()
    }

    @Synchronized
    fun forget(id: String) {
        load()
        if (data.remove(id) != null) dirty = true
    }

    /** Ряды для графиков за hours часов: средние CPU/памяти/онлайна, сумма трафика в корзине. */
    fun series(id: String, hours: Int, nowSec: Long, buckets: Int = 48): Series? {
        val start = nowSec - hours * 3600L
        val list = samples(id).filter { it.ts > start && it.ts <= nowSec }
        if (list.isEmpty()) return null
        val width = (nowSec - start).toDouble() / buckets
        val groups = Array(buckets) { mutableListOf<Sample>() }
        for (s in list) {
            val i = ((s.ts - start) / width).toInt().coerceIn(0, buckets - 1)
            groups[i].add(s)
        }
        fun avg(g: List<Sample>, f: (Sample) -> Double?): Double? {
            val v = g.filter { it.up }.mapNotNull(f)
            return if (v.isEmpty()) null else v.average()
        }
        fun total(g: List<Sample>, f: (Sample) -> Double?): Double? {
            val v = g.mapNotNull(f)
            return if (v.isEmpty()) null else v.sum()
        }
        return Series(
            cpu = groups.map { avg(it) { s -> s.cpu } },
            mem = groups.map { avg(it) { s -> s.mem } },
            online = groups.map { avg(it) { s -> s.online?.toDouble() } },
            downBytes = groups.map { total(it) { s -> s.downBytes } },
            upBytes = groups.map { total(it) { s -> s.upBytes } },
        )
    }

    /** Как панель ПК: доступность — доля опросов с удачным SSH, средняя задержка — по TCP. */
    fun stats(id: String, hours: Int, nowSec: Long): Pair<Double?, Double?> {
        val list = samples(id).filter { it.ts > nowSec - hours * 3600L }
        if (list.isEmpty()) return null to null
        val avail = Math.round(1000.0 * list.count { it.up } / list.size) / 10.0
        val lats = list.mapNotNull { it.tcpMs }
        val lat = if (lats.isEmpty()) null else Math.round(lats.average() * 10) / 10.0
        return avail to lat
    }

    /** Сохранить на диск не чаще раза в минуту (или сразу при force). */
    @Synchronized
    fun save(nowMs: Long = System.currentTimeMillis(), force: Boolean = false) {
        val f = file ?: return
        if (!dirty || (!force && nowMs - savedAt < 60_000)) return
        val sb = StringBuilder("v1\n")
        for ((id, list) in data) {
            if (list.isEmpty()) continue
            sb.append('#').append(id).append('\n')
            for (s in list) {
                sb.append(s.ts).append(',').append(if (s.up) 1 else 0)
                for (v in listOf(s.tcpMs, s.pingMs, s.cpu, s.mem, s.online?.toDouble(), s.downBytes, s.upBytes)) {
                    sb.append(',').append(num(v))
                }
                sb.append('\n')
            }
        }
        runCatching {
            val tmp = File(f.path + ".tmp")
            tmp.writeText(sb.toString())
            if (!tmp.renameTo(f)) { f.delete(); tmp.renameTo(f) }
            dirty = false
            savedAt = nowMs
        }
    }

    private fun load() {
        if (loaded) return
        loaded = true
        val f = file ?: return
        val text = runCatching { if (f.exists()) f.readText() else "" }.getOrDefault("")
        if (!text.startsWith("v1\n")) return
        var cur: MutableList<Sample>? = null
        for (line in text.lineSequence().drop(1)) {
            if (line.isEmpty()) continue
            if (line[0] == '#') { cur = data.getOrPut(line.substring(1)) { mutableListOf() }; continue }
            val p = line.split(',')
            if (p.size != 9 || cur == null) continue
            val ts = p[0].toLongOrNull() ?: continue
            fun d(i: Int) = p[i].toDoubleOrNull()
            cur.add(Sample(ts, p[1] == "1", d(2), d(3), d(4), d(5), d(6)?.toInt(), d(7), d(8)))
        }
    }

    private fun sum(a: Double?, b: Double?) = if (a == null && b == null) null else (a ?: 0.0) + (b ?: 0.0)

    private fun num(v: Double?): String = when {
        v == null || v.isNaN() -> ""
        v == v.roundToLong().toDouble() && kotlin.math.abs(v) < 1e15 -> v.roundToLong().toString()
        else -> String.format(Locale.US, "%.2f", v)
    }
}
