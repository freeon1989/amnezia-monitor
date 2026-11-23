package org.amnezia.monitor.data

/**
 * Страна сервера для оформления — та же логика, что theme.py на ПК:
 * 1) вручную (поле «Страна»); 2) слова в названии (RU/EN); 3) GeoIP (по желанию, выключен по умолчанию);
 * иначе общая тема «Amnezia». Если сервер пришёл из панели ПК, страна берётся из её ответа.
 */
data class ThemeInfo(
    val country: String,
    val countryName: String,
    val key: String,
    val source: String,
    val accent: Long,
    val accent2: Long,
    val glow: Long,
)

object CountryDetect {
    private val CODE = Regex("^[a-z]{2}$")

    fun normalize(value: String?): String {
        val v = value.orEmpty().trim().lowercase()
        return if (CODE.matches(v)) v else ""
    }

    private fun isWordChar(c: Char) = c.isLetterOrDigit() || c == '_'

    /** Целое слово: соседние символы — не буквы/цифры (одинаково для кириллицы и латиницы на JVM и Android). */
    private fun containsWord(text: String, word: String): Boolean {
        var from = 0
        while (true) {
            val i = text.indexOf(word, from)
            if (i < 0) return false
            val before = i == 0 || !isWordChar(text[i - 1])
            val end = i + word.length
            val after = end >= text.length || !isWordChar(text[end])
            if (before && after) return true
            from = i + 1
        }
    }

    fun byName(name: String?): String? {
        val n = name.orEmpty().lowercase()
        if (n.isBlank()) return null
        for ((cc, words) in ThemeData.KEYWORDS) {
            for (w in words) {
                val hit = if (w.length <= 4) containsWord(n, w) else n.contains(w)
                if (hit) return cc
            }
        }
        return null
    }

    /** -> (код страны или "", источник: manual | name | panel | geoip | none). */
    fun detect(name: String?, manual: String? = null, geoip: String? = null): Pair<String, String> {
        val m = normalize(manual)
        if (m.isNotEmpty()) return m to "manual"
        val byName = byName(name)
        if (byName != null) return byName to "name"
        val g = normalize(geoip)
        if (g.isNotEmpty()) return g to "geoip"
        return "" to "none"
    }

    fun countryName(cc: String): String {
        if (cc.isEmpty()) return ""
        val t = ThemeData.THEMES[cc]
        if (t != null && cc != "default") return t.name
        return ThemeData.COUNTRY_NAMES[cc] ?: cc.uppercase()
    }

    fun theme(country: String, source: String): ThemeInfo {
        val cc = normalize(country)
        val key = if (cc.isNotEmpty() && ThemeData.THEMES.containsKey(cc)) cc else "default"
        val spec = ThemeData.THEMES.getValue(key)
        return ThemeInfo(cc, countryName(cc), key, if (cc.isEmpty()) "none" else source, spec.accent, spec.accent2, spec.glow)
    }

    fun themeFor(name: String?, manual: String? = null, geoip: String? = null): ThemeInfo {
        val (cc, src) = detect(name, manual, geoip)
        return theme(cc, src)
    }

    val DEFAULT: ThemeInfo get() = theme("", "none")

    /** Для «Страна»: сначала страны со своими героинями, потом остальные; по алфавиту. */
    fun choices(): List<Pair<String, String>> {
        val own = ThemeData.THEMES.filterKeys { it != "default" }.map { it.key to it.value.name }.sortedBy { it.second }
        val rest = ThemeData.COUNTRY_NAMES.map { it.key to it.value }.sortedBy { it.second }
        return own + rest
    }

    /** GeoIP только для публичного IPv4-литерала: домены не резолвим, локальные адреса не отправляем. */
    fun isPublicIpv4(host: String?): Boolean {
        val parts = host.orEmpty().trim().split(".")
        if (parts.size != 4) return false
        val b = parts.map { p -> if (p.isEmpty() || p.length > 3 || !p.all(Char::isDigit)) return false else p.toInt() }
        if (b.any { it > 255 }) return false
        val (a, s) = b[0] to b[1]
        return when {
            a == 0 || a == 10 || a == 127 || a >= 224 -> false
            a == 100 && s in 64..127 -> false            // CGNAT
            a == 169 && s == 254 -> false
            a == 172 && s in 16..31 -> false
            a == 192 && s == 168 -> false
            a == 192 && s == 0 && b[2] == 2 -> false      // документация
            a == 198 && (s == 18 || s == 19) -> false
            a == 198 && s == 51 && b[2] == 100 -> false
            a == 203 && s == 0 && b[2] == 113 -> false
            else -> true
        }
    }
}
