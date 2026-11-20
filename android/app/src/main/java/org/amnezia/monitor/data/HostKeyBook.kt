package org.amnezia.monitor.data

import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.Base64

/** Что известно о смене ключа хоста: показывается пользователю до любой замены. */
data class HostKeyMismatch(
    val host: String,
    val port: Int,
    val old: List<HostKeyInfo>,
    val new: HostKeyInfo,
    val typeChanged: Boolean,
)

data class HostKeyInfo(val type: String, val algo: String, val fingerprint: String)

/**
 * Ключи хостов SSH в настройках приложения (без Android API — проверяется unit-тестами).
 *
 * Запись: "host|тип" -> base64 ключа. Хост — как в OpenSSH: `host` для порта 22, иначе `[host]:port`.
 * Старые записи версии 1.1.0 ("host" -> base64, без типа) читаются: тип берётся из самого ключа.
 */
object HostKeyBook {
    enum class Result { OK, NEW, CHANGED, TYPE_CHANGED }

    fun entryName(host: String, port: Int): String = if (port == 22) host else "[$host]:$port"

    /** Тип ключа из SSH-blob: первая строка (uint32 длина + ASCII). */
    fun keyAlgo(blob: ByteArray): String {
        if (blob.size < 4) return "?"
        val n = ByteBuffer.wrap(blob, 0, 4).int
        if (n <= 0 || n > 64 || 4 + n > blob.size) return "?"
        return String(blob, 4, n, Charsets.US_ASCII)
    }

    fun typeLabel(algo: String): String = when {
        algo.startsWith("ssh-ed25519") -> "ED25519"
        algo.startsWith("ecdsa-") -> "ECDSA"
        algo == "ssh-rsa" || algo.startsWith("rsa-sha2") -> "RSA"
        algo.startsWith("ssh-dss") -> "DSA"
        else -> algo
    }

    fun fingerprint(blob: ByteArray): String {
        val d = MessageDigest.getInstance("SHA-256").digest(blob)
        return "SHA256:" + Base64.getEncoder().withoutPadding().encodeToString(d)
    }

    fun info(blob: ByteArray): HostKeyInfo {
        val algo = keyAlgo(blob)
        return HostKeyInfo(typeLabel(algo), algo, fingerprint(blob))
    }

    /** Все известные ключи хоста: тип -> blob. */
    fun known(keys: Map<String, String>, host: String): Map<String, ByteArray> {
        val res = linkedMapOf<String, ByteArray>()
        for ((k, v) in keys) {
            val name = k.substringBefore('|')
            if (name != host) continue
            val blob = runCatching { Base64.getDecoder().decode(v) }.getOrNull() ?: continue
            res[keyAlgo(blob)] = blob
        }
        return res
    }

    fun check(keys: Map<String, String>, host: String, blob: ByteArray): Result {
        val known = known(keys, host)
        if (known.isEmpty()) return Result.NEW
        val same = known[keyAlgo(blob)] ?: return Result.TYPE_CHANGED
        return if (same.contentEquals(blob)) Result.OK else Result.CHANGED
    }

    fun mismatch(keys: Map<String, String>, host: String, port: Int, blob: ByteArray): HostKeyMismatch? {
        val r = check(keys, host, blob)
        if (r != Result.CHANGED && r != Result.TYPE_CHANGED) return null
        val bare = host.removePrefix("[").substringBefore("]")
        return HostKeyMismatch(bare, port, known(keys, host).values.map { info(it) }, info(blob), r == Result.TYPE_CHANGED)
    }

    /** Первый вход: запомнить ключ. */
    fun add(keys: MutableMap<String, String>, host: String, blob: ByteArray) {
        keys["$host|${keyAlgo(blob)}"] = Base64.getEncoder().encodeToString(blob)
    }

    /** «Доверять новому ключу»: убрать все старые ключи этого хоста и записать один новый. */
    fun replace(keys: MutableMap<String, String>, host: String, blob: ByteArray) {
        keys.keys.removeAll { it.substringBefore('|') == host }
        add(keys, host, blob)
    }

    /** Типы ключей для согласования: сначала уже известные (как в OpenSSH), чтобы не было ложной «смены». */
    fun preferredAlgorithms(keys: Map<String, String>, host: String, defaults: List<String>): List<String> {
        val known = known(keys, host).keys.flatMap {
            if (typeLabel(it) == "RSA") listOf("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa") else listOf(it)
        }
        if (known.isEmpty()) return defaults
        return known.filter { it in defaults } + defaults.filter { it !in known }
    }
}
