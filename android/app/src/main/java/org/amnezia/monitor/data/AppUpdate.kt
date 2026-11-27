package org.amnezia.monitor.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.longOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * Обновление приложения с GitHub Releases. Ничего не скачивается без нажатия «Обновить»;
 * файл берётся только с хостов GitHub по HTTPS и принимается, только если его SHA-256
 * совпал с файлом .sha256 из того же релиза. Установку подтверждает системный установщик Android.
 */
object AppUpdate {
    const val REPO = "freeon1989/amnezia-monitor"
    const val API = "https://api.github.com"
    const val APK_NAME = "AmneziaMonitor-android.apk"
    const val SHA_NAME = "AmneziaMonitor-android.apk.sha256"
    const val MAX_APK = 120L * 1024 * 1024
    val GITHUB_HOSTS = setOf(
        "api.github.com", "github.com", "objects.githubusercontent.com",
        "release-assets.githubusercontent.com", "github-releases.githubusercontent.com",
    )
    private val TAG = Regex("^v?(\\d{1,4})\\.(\\d{1,4})\\.(\\d{1,4})$")
    private val HASH = Regex("\\b([0-9a-fA-F]{64})\\b")

    class UpdateException(message: String) : Exception(message)

    data class Release(
        val version: String,
        val notes: String,
        val pageUrl: String,
        val apkUrl: String,
        val shaUrl: String,
        val size: Long,
    ) {
        val hasAsset get() = apkUrl.isNotEmpty() && shaUrl.isNotEmpty()
    }

    fun parseVersion(text: String): List<Int>? =
        TAG.matchEntire(text.trim())?.groupValues?.drop(1)?.map { it.toInt() }

    fun isNewer(candidate: String, current: String): Boolean {
        val a = parseVersion(candidate) ?: return false
        val b = parseVersion(current) ?: return false
        for (i in 0 until 3) if (a[i] != b[i]) return a[i] > b[i]
        return false
    }

    /** Строка «<64 hex>  имя» из файла .sha256. */
    fun parseSha(text: String, name: String): String? {
        val lines = text.lines().map { it.trim() }.filter { it.isNotEmpty() }
        lines.firstOrNull { name in it && HASH.containsMatchIn(it) }?.let { return HASH.find(it)!!.value.lowercase() }
        if (lines.size == 1) HASH.matchEntire(lines[0].split(Regex("\\s+"))[0])?.let { return it.value.lowercase() }
        return null
    }

    fun parseRelease(body: String): Release {
        val o = try {
            Json.parseToJsonElement(body).jsonObject
        } catch (_: Exception) {
            throw UpdateException("GitHub вернул непонятный ответ")
        }
        if (o["draft"]?.str() == "true" || o["prerelease"]?.str() == "true") throw UpdateException("Подходящего релиза нет")
        val tag = o["tag_name"]?.str().orEmpty()
        val v = parseVersion(tag) ?: throw UpdateException("Непонятный номер версии")
        var apk = ""
        var sha = ""
        var size = 0L
        (o["assets"] as? JsonArray)?.forEach { el ->
            val a = el as? JsonObject ?: return@forEach
            when (a["name"]?.str()) {
                APK_NAME -> { apk = a["browser_download_url"]?.str().orEmpty(); size = (a["size"] as? JsonPrimitive)?.longOrNull ?: 0L }
                SHA_NAME -> sha = a["browser_download_url"]?.str().orEmpty()
            }
        }
        val page = o["html_url"]?.str().orEmpty().ifEmpty { "https://github.com/$REPO/releases/tag/$tag" }
        return Release(v.joinToString("."), (o["body"]?.str().orEmpty()).take(20_000), page, apk, sha, size)
    }

    private fun kotlinx.serialization.json.JsonElement.str(): String? = (this as? JsonPrimitive)?.contentOrNull

    fun pageOk(url: String): Boolean = url.startsWith("https://github.com/$REPO/releases")
}

class AppUpdater(
    private val dir: File,
    private val api: String = AppUpdate.API,
    private val hosts: Set<String> = AppUpdate.GITHUB_HOSTS,
    private val allowHttp: Boolean = false,    // только для тестов с локальным сервером
    userAgent: String = "AmneziaMonitor-Android",
) {
    private val ua = userAgent
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .followRedirects(true)
        // сетевой перехватчик видит каждый переход по перенаправлению
        .addNetworkInterceptor { chain ->
            val u = chain.request().url
            if ((!u.isHttps && !allowHttp) || u.host.lowercase() !in hosts) {
                throw IOException("Адрес обновления не с GitHub или не HTTPS: ${u.host}")
            }
            chain.proceed(chain.request())
        }
        .build()

    private fun get(url: String): okhttp3.Response {
        val req = Request.Builder().url(url).header("User-Agent", ua).header("Accept", "application/vnd.github+json, */*").build()
        val r = try {
            client.newCall(req).execute()
        } catch (e: IOException) {
            throw AppUpdate.UpdateException(e.message?.takeIf { it.startsWith("Адрес") } ?: "Нет связи с GitHub")
        }
        if (!r.isSuccessful) {
            val code = r.code
            r.close()
            throw AppUpdate.UpdateException(
                when (code) {
                    403, 429 -> "GitHub временно ограничил запросы (код $code). Попробуйте позже."
                    404 -> "Релиз или файл не найден на GitHub."
                    else -> "GitHub ответил ошибкой $code."
                },
            )
        }
        return r
    }

    suspend fun latest(): AppUpdate.Release = withContext(Dispatchers.IO) {
        get("${api.trimEnd('/')}/repos/${AppUpdate.REPO}/releases/latest").use { r ->
            val body = r.body?.source()?.let { src -> src.request(1_000_001); if (src.buffer.size > 1_000_000) null else src.buffer.readUtf8() }
                ?: throw AppUpdate.UpdateException("Ответ GitHub слишком большой")
            AppUpdate.parseRelease(body)
        }
    }

    /** Скачивает APK, сверяет SHA-256 с .sha256 из релиза. При несовпадении файл удаляется. */
    suspend fun download(rel: AppUpdate.Release, progress: (Long, Long) -> Unit = { _, _ -> }): File = withContext(Dispatchers.IO) {
        if (!rel.hasAsset) throw AppUpdate.UpdateException("В релизе нет APK для автоматической установки")
        dir.mkdirs()
        dir.listFiles()?.forEach { it.delete() }
        val expected = get(rel.shaUrl).use { r ->
            val text = r.body?.source()?.let { s -> s.request(4097); if (s.buffer.size > 4096) null else s.buffer.readUtf8() }.orEmpty()
            AppUpdate.parseSha(text, AppUpdate.APK_NAME) ?: throw AppUpdate.UpdateException("В файле .sha256 нет контрольной суммы")
        }
        val out = File(dir, "AmneziaMonitor-${rel.version}.apk")
        val part = File(dir, "download.part")
        try {
            val md = MessageDigest.getInstance("SHA-256")
            get(rel.apkUrl).use { r ->
                val body = r.body ?: throw AppUpdate.UpdateException("Пустой ответ GitHub")
                val total = body.contentLength().takeIf { it > 0 } ?: rel.size
                if (total > AppUpdate.MAX_APK) throw AppUpdate.UpdateException("Файл обновления слишком большой")
                var done = 0L
                body.byteStream().use { input ->
                    part.outputStream().use { o ->
                        val buf = ByteArray(64 * 1024)
                        while (true) {
                            val n = input.read(buf)
                            if (n < 0) break
                            done += n
                            if (done > AppUpdate.MAX_APK) throw AppUpdate.UpdateException("Файл обновления слишком большой")
                            md.update(buf, 0, n)
                            o.write(buf, 0, n)
                            progress(done, total)
                        }
                    }
                }
            }
            val actual = md.digest().joinToString("") { "%02x".format(it) }
            if (actual != expected) {
                throw AppUpdate.UpdateException("Контрольная сумма SHA-256 не совпала: файл повреждён или подменён. Обновление отменено.")
            }
            if (!part.renameTo(out)) throw AppUpdate.UpdateException("Не удалось сохранить файл обновления")
            out
        } catch (e: Exception) {
            part.delete()
            out.delete()
            if (e is AppUpdate.UpdateException) throw e
            throw AppUpdate.UpdateException(e.message?.takeIf { it.startsWith("Адрес") } ?: "Не удалось скачать обновление")
        }
    }
}
