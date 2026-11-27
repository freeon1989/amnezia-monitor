package org.amnezia.monitor

import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.Buffer
import kotlinx.coroutines.runBlocking
import org.amnezia.monitor.data.AppUpdate
import org.amnezia.monitor.data.AppUpdater
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest

class AppUpdateTest {
    private lateinit var server: MockWebServer
    private lateinit var dir: File
    private var apk = ByteArray(5000) { (it % 251).toByte() }
    private var shaOverride: String? = null
    private var redirectTo: String? = null
    private var version = "1.9.0"
    private var assets = true
    private var apkHits = 0
    private var apiHits = 0
    private val base get() = "http://127.0.0.1:${server.port}"

    private fun sha(b: ByteArray) = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

    @Before
    fun setUp() {
        dir = Files.createTempDirectory("upd").toFile()
        server = MockWebServer()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path.orEmpty()
                return when {
                    path == "/repos/${AppUpdate.REPO}/releases/latest" -> {
                        apiHits++
                        val a = if (assets) """[
                            {"name":"${AppUpdate.APK_NAME}","size":${apk.size},"browser_download_url":"$base/dl/${AppUpdate.APK_NAME}"},
                            {"name":"${AppUpdate.SHA_NAME}","size":90,"browser_download_url":"$base/dl/${AppUpdate.SHA_NAME}"}]""" else "[]"
                        MockResponse().setBody("""{"tag_name":"v$version","draft":false,"prerelease":false,"body":"Что нового: тест","html_url":"https://github.com/${AppUpdate.REPO}/releases/tag/v$version","assets":$a}""")
                    }
                    path.startsWith("/dl/") -> {
                        apkHits++
                        val r = redirectTo
                        when {
                            r != null -> MockResponse().setResponseCode(302).setHeader("Location", r)
                            path.endsWith(".sha256") -> MockResponse().setBody("${shaOverride ?: sha(apk)}  ${AppUpdate.APK_NAME}\n")
                            else -> MockResponse().setBody(Buffer().write(apk))
                        }
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
        dir.deleteRecursively()
    }

    private fun updater() = AppUpdater(dir, api = base, hosts = setOf("127.0.0.1"), allowHttp = true)

    @Test
    fun semver() {
        assertTrue(AppUpdate.isNewer("v1.10.0", "1.9.9"))
        assertTrue(AppUpdate.isNewer("1.5.0", "1.4.2"))
        assertFalse(AppUpdate.isNewer("1.5.0", "1.5.0"))
        assertFalse(AppUpdate.isNewer("1.4.9", "1.5.0"))
        assertFalse(AppUpdate.isNewer("latest", "1.5.0"))
        assertNull(AppUpdate.parseVersion("1.5"))
        assertNull(AppUpdate.parseVersion("1.5.0-beta"))
    }

    @Test
    fun shaParsing() {
        val h = "ab".repeat(32)
        assertEquals(h, AppUpdate.parseSha("${h.uppercase()}  ${AppUpdate.APK_NAME}\n", AppUpdate.APK_NAME))
        assertEquals(h, AppUpdate.parseSha("$h\n", AppUpdate.APK_NAME))
        assertNull(AppUpdate.parseSha("$h  other.apk\n$h  x.apk\n", AppUpdate.APK_NAME))
        assertNull(AppUpdate.parseSha("not a hash", AppUpdate.APK_NAME))
    }

    @Test
    fun checkDoesNotDownloadAnything() = runBlocking {
        val rel = updater().latest()
        assertEquals("1.9.0", rel.version)
        assertTrue(rel.hasAsset)
        assertTrue(rel.notes.contains("Что нового"))
        assertEquals(0, apkHits)
        assertEquals(1, apiHits)
    }

    @Test
    fun downloadVerifiesHash() = runBlocking {
        val f = updater().download(updater().latest())
        assertTrue(f.isFile)
        assertEquals(sha(apk), sha(f.readBytes()))
        assertFalse(File(dir, "download.part").exists())
    }

    @Test
    fun tamperedHashRejectedAndNothingLeft() = runBlocking {
        shaOverride = "0".repeat(64)
        try {
            updater().download(updater().latest())
            fail("должно отклониться")
        } catch (e: AppUpdate.UpdateException) {
            assertTrue(e.message!!.contains("SHA-256"))
        }
        assertEquals(0, dir.listFiles()!!.size)
    }

    @Test
    fun tamperedFileRejected() = runBlocking {
        val good = sha(apk)
        shaOverride = good
        apk = ByteArray(5000) { 7 }           // файл подменили, хеш остался прежним
        try {
            updater().download(updater().latest())
            fail("должно отклониться")
        } catch (e: AppUpdate.UpdateException) {
            assertTrue(e.message!!.contains("SHA-256"))
        }
        assertEquals(0, dir.listFiles()!!.size)
    }

    @Test
    fun redirectToForeignHostBlocked() = runBlocking {
        redirectTo = "http://localhost:1/evil.apk"      // хост не в списке
        try {
            updater().download(updater().latest())
            fail("должно отклониться")
        } catch (e: AppUpdate.UpdateException) {
            assertTrue(e.message!!, e.message!!.contains("не с GitHub") || e.message!!.contains("Нет связи"))
        }
        Unit
    }

    @Test
    fun httpAndForeignHostsBlockedByDefault() = runBlocking {
        val strict = AppUpdater(dir, api = base)          // https и GitHub-хосты по умолчанию
        try {
            strict.latest()
            fail("http://127.0.0.1 не должен приниматься")
        } catch (e: AppUpdate.UpdateException) {
            assertNotNull(e.message)
        }
        assertEquals(0, apiHits)
    }

    @Test
    fun releaseWithoutAssetsHasNoAutoInstall() = runBlocking {
        assets = false
        val rel = updater().latest()
        assertFalse(rel.hasAsset)
        try {
            updater().download(rel)
            fail()
        } catch (_: AppUpdate.UpdateException) {
        }
        Unit
    }

    @Test
    fun releasePageCheck() {
        assertTrue(AppUpdate.pageOk("https://github.com/freeon1989/amnezia-monitor/releases/tag/v1.5.0"))
        assertFalse(AppUpdate.pageOk("https://evil.com/freeon1989/amnezia-monitor/releases"))
        assertFalse(AppUpdate.pageOk("http://github.com/freeon1989/amnezia-monitor/releases"))
    }

    @Test
    fun draftAndPrereleaseIgnored() {
        try {
            AppUpdate.parseRelease("""{"tag_name":"v2.0.0","draft":false,"prerelease":true,"assets":[]}""")
            fail()
        } catch (_: AppUpdate.UpdateException) {
        }
        try {
            AppUpdate.parseRelease("""{"tag_name":"nightly","assets":[]}""")
            fail()
        } catch (_: AppUpdate.UpdateException) {
        }
    }
}
