package org.amnezia.monitor.data

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import java.nio.charset.StandardCharsets
import java.util.Base64
import java.util.zip.Inflater

/**
 * Разбор того, что пользователь может вынести из AmneziaVPN без root:
 * резервная копия (JSON), ссылка vpn:// и клиентский .conf.
 * Из объекта сервера берутся только имя, адрес, SSH-порт, пользователь и секрет входа.
 * Контейнеры, ключи WireGuard/AmneziaWG и last_config отбрасываются.
 */
object AmneziaImport {
    private val json = Json { ignoreUnknownKeys = true }
    private const val MAX_PLAIN = 2_000_000
    private val HOST = Regex("^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$")
    private val USER = Regex("^[A-Za-z0-9._-]{1,32}$")

    fun parse(raw: String): List<ImportedServer> {
        val text = unwrap(raw)
        val trimmed = text.trim().removePrefix("\uFEFF")
        if (trimmed.isEmpty()) throw IllegalArgumentException("Пустой файл")
        val fromJson = runCatching { serversFromJson(trimmed) }.getOrNull()
        val found = when {
            !fromJson.isNullOrEmpty() -> fromJson
            looksLikeConf(trimmed) -> listOfNotNull(fromConf(trimmed))
            else -> throw IllegalArgumentException(
                "Это не резервная копия Amnezia, не ссылка vpn:// и не конфиг WireGuard",
            )
        }
        if (found.isEmpty()) throw IllegalArgumentException("В файле нет серверов с адресом")
        return found
    }

    fun merge(current: List<ImportedServer>, incoming: List<ImportedServer>, newId: () -> String): List<ImportedServer> {
        val out = current.toMutableList()
        for (inc in incoming) {
            val idx = out.indexOfFirst { it.host.equals(inc.host, ignoreCase = true) && it.port == inc.port }
            if (idx < 0) {
                out.add(inc.copy(id = newId()))
            } else {
                val old = out[idx]
                out[idx] = old.copy(
                    name = inc.name.ifBlank { old.name },
                    user = inc.user.ifBlank { old.user },
                    password = inc.password.ifBlank { old.password },
                    privateKey = inc.privateKey.ifBlank { old.privateKey },
                )
            }
        }
        return out.take(32)
    }

    private fun unwrap(raw: String): String {
        val text = raw.trim().removePrefix("\uFEFF")
        val at = text.indexOf("vpn://")
        if (at >= 0) {
            val token = buildString {
                for (ch in text.substring(at + 6)) {
                    if (ch.isWhitespace()) break
                    append(ch)
                }
            }
            return decodeVpnToken(token)
        }
        return text
    }

    private fun decodeVpnToken(token: String): String {
        val bytes = try {
            b64Url(token)
        } catch (_: Exception) {
            throw IllegalArgumentException("Ссылка vpn:// повреждена")
        }
        val plain = qUncompress(bytes) ?: bytes
        if (plain.size > MAX_PLAIN) throw IllegalArgumentException("Конфиг слишком большой")
        val text = String(plain, StandardCharsets.UTF_8)
        if (!text.contains("hostName") && !text.contains("Endpoint") && !text.contains("serversList")) {
            throw IllegalArgumentException("В ссылке vpn:// нет описания сервера")
        }
        return text
    }

    private fun serversFromJson(text: String): List<ImportedServer> {
        val el = json.parseToJsonElement(text)
        val objects = when (el) {
            is JsonArray -> el.mapNotNull { it as? JsonObject }
            is JsonObject -> serversInObject(el)
            else -> emptyList()
        }
        return objects.mapNotNull { toServer(it) }
    }

    private fun serversInObject(o: JsonObject): List<JsonObject> {
        val listKey = o.keys.firstOrNull { it == "Servers/serversList" || it.endsWith("serversList") }
        if (listKey != null) {
            return elementsOf(o[listKey]).mapNotNull { it as? JsonObject }
        }
        if (o["hostName"] != null || o["hostname"] != null) return listOf(o)
        val nested = o["servers"]
        if (nested != null) return elementsOf(nested).mapNotNull { it as? JsonObject }
        return emptyList()
    }

    private fun elementsOf(el: JsonElement?): List<JsonElement> {
        when (el) {
            is JsonArray -> return el
            is JsonPrimitive -> {
                val s = el.contentOrNull ?: return emptyList()
                val parsed = runCatching { json.parseToJsonElement(s) }.getOrNull()
                if (parsed is JsonArray) return parsed
                if (parsed is JsonObject) return listOf(parsed)
                val decoded = runCatching { String(b64Url(s), StandardCharsets.UTF_8) }.getOrNull()
                if (decoded != null) {
                    val again = runCatching { json.parseToJsonElement(decoded) }.getOrNull()
                    if (again is JsonArray) return again
                    if (again is JsonObject) return listOf(again)
                }
            }
            else -> Unit
        }
        return emptyList()
    }

    private fun toServer(o: JsonObject): ImportedServer? {
        val host = str(o, "hostName", "hostname", "host")?.trim().orEmpty()
        if (!HOST.matches(host) || host.startsWith("127.")) return null
        val description = str(o, "description", "name")?.trim().orEmpty()
        val user = str(o, "userName", "username", "user")?.trim().orEmpty().ifBlank { "root" }
        if (!USER.matches(user)) return null
        val port = int(o, "port") ?: 22
        if (port !in 1..65535) return null
        val secret = str(o, "password", "secretData").orEmpty()
        if (secret.length > 32_000) return null
        val key = secret.contains("PRIVATE KEY") || secret.contains("BEGIN OPENSSH")
        return ImportedServer(
            id = "",
            name = description.ifBlank { host }.take(40),
            host = host,
            port = port,
            user = user,
            password = if (key) "" else secret,
            privateKey = if (key) secret else "",
        )
    }

    private fun looksLikeConf(text: String): Boolean {
        val l = text.lowercase()
        return "[peer]" in l || "[interface]" in l || l.contains("\nremote ") || l.startsWith("remote ")
    }

    private fun fromConf(text: String): ImportedServer? {
        val title = text.lineSequence()
            .map { it.trim() }
            .firstOrNull { it.startsWith("#") && it.length > 1 }
            ?.removePrefix("#")
            ?.trim()
            .orEmpty()
        val endpoint = Regex("(?im)^\\s*Endpoint\\s*=\\s*([^\\s#]+)")
            .find(text)?.groupValues?.get(1)
        val remote = Regex("(?im)^\\s*remote\\s+(\\S+)").find(text)?.groupValues?.get(1)
        val rawHost = endpoint ?: remote ?: return null
        val host = rawHost.substringBeforeLast(':').removePrefix("[").removeSuffix("]")
            .substringBefore("%").trim()
        if (!HOST.matches(host)) return null
        return ImportedServer(
            id = "",
            name = title.ifBlank { host }.take(40),
            host = host,
            port = 22,
            user = "root",
        )
    }

    private fun str(o: JsonObject, vararg keys: String): String? {
        for (k in keys) {
            val v = o[k] as? JsonPrimitive ?: continue
            val s = v.contentOrNull ?: continue
            if (s.isNotBlank()) return s
        }
        return null
    }

    private fun int(o: JsonObject, key: String): Int? {
        val v = o[key] as? JsonPrimitive ?: return null
        return v.content.toIntOrNull() ?: v.content.toDoubleOrNull()?.toInt()
    }

    private fun b64Url(s: String): ByteArray {
        var t = s.trim().replace("\n", "").replace("\r", "")
        val pad = (4 - t.length % 4) % 4
        if (pad != 4) t += "=".repeat(pad)
        return try {
            Base64.getUrlDecoder().decode(t)
        } catch (_: IllegalArgumentException) {
            Base64.getDecoder().decode(t)
        }
    }

    /** Формат Qt qCompress: 4 байта длины (big-endian) и zlib. */
    private fun qUncompress(data: ByteArray): ByteArray? {
        if (data.size < 6) return null
        val declared = ((data[0].toInt() and 0xff) shl 24) or
            ((data[1].toInt() and 0xff) shl 16) or
            ((data[2].toInt() and 0xff) shl 8) or
            (data[3].toInt() and 0xff)
        if (declared <= 0 || declared > MAX_PLAIN) return null
        val inflater = Inflater()
        return try {
            inflater.setInput(data, 4, data.size - 4)
            val out = ByteArray(declared)
            val n = inflater.inflate(out)
            if (n <= 0) null else out.copyOf(n)
        } catch (_: Exception) {
            null
        } finally {
            inflater.end()
        }
    }
}
