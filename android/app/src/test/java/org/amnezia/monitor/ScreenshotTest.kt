package org.amnezia.monitor

import android.graphics.Bitmap
import android.graphics.Canvas
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.graphics.Color
import org.amnezia.monitor.data.ApiJson
import org.amnezia.monitor.data.CountryDetect
import org.amnezia.monitor.data.ServerSummary
import org.amnezia.monitor.ui.AmneziaTheme
import org.amnezia.monitor.ui.DetailScreen
import org.amnezia.monitor.ui.HomeScreen
import org.amnezia.monitor.ui.LocalAccent
import org.amnezia.monitor.ui.LocalAccent2
import org.amnezia.monitor.ui.LocalAnimations
import org.amnezia.monitor.ui.NeonBackground


import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File
import java.time.Duration

/**
 * Снимки экранов на демо-данных (только адреса из документационных диапазонов).
 * Анимации выключены: бесконечные анимации (лепестки, Ken Burns) не дают Robolectric «успокоиться».
 * Запуск: ./gradlew testDebugUnitTest -Pshots=1 --tests ScreenshotTest → android/build/shots
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [34], qualifiers = "w400dp-h860dp-xhdpi")
class ScreenshotTest {
    private fun on() = System.getProperty("amnezia.shots").orEmpty().isNotEmpty()

    private fun srv(name: String, host: String, status: String, cpu: Double?, on: Int?, tot: Int?, panelCc: String? = null): ServerSummary {
        val t = if (panelCc != null) CountryDetect.theme(panelCc, "manual") else CountryDetect.themeFor(name)
        return ServerSummary(name, host, status, 8.0, 21.0, cpu, cpu?.let { it + 22 }, on, tot,
            cpu?.let { it * 9000 }, cpu?.let { it * 3000 }, if (status == "down") "SSH timeout" else null, theme = t)
    }

    private val servers = listOf(
        srv("Svecia", "203.0.113.11", "up", 12.0, 3, 5),
        srv("Вена-2", "203.0.113.12", "up", 34.0, 1, 4),
        srv("Kazahi", "203.0.113.13", "up", 71.0, 6, 8),
        srv("Charlotte", "198.51.100.20", "up", 8.0, 2, 3),
        srv("Amsterdam", "198.51.100.21", "down", null, null, null),
        srv("test1", "192.0.2.10", "nodata", null, null, null),
    )

    private fun detailJson(name: String, host: String): String {
        val n = 48
        fun arr(f: (Int) -> Double) = (0 until n).joinToString(",", "[", "]") { "%.1f".format(java.util.Locale.US, f(it)) }
        return """
            {"name":"$name","host":"$host",
             "current":{"status":"up","age":6,"tcp_ms":18,"ping_ms":17.5,"availability":99.8,"latency_avg":19,
               "uptime_s":864000,"load1":0.31,"nproc":2,"cpu":14,"mem":38,"mem_used":818000000,"mem_total":2147483648,
               "rx_bps":420000,"tx_bps":96000,"stale":false},
             "containers":[{"name":"amnezia-awg","type":"awg","up":true,"status":"Up 10 days"}],
             "series":{"start":1800000000,"end":1800086400,"buckets":$n,
               "cpu":${arr { 12 + 9 * Math.sin(it / 5.0) + (it % 7) }},"mem":${arr { 36 + 3 * Math.sin(it / 9.0) }},
               "online":${arr { (2 + (it / 8) % 3).toDouble() }},"down_bytes":${arr { 2e8 + 1.5e8 * Math.sin(it / 6.0) }},
               "up_bytes":${arr { 4e7 + 2e7 * Math.cos(it / 7.0) }},"avail":${arr { 1.0 }}},
             "users":[
               {"client":"Ноутбук","pubkey":"k1…","container":"amnezia-awg","online":true,"handshake_age":40,
                "endpoint":"198.51.100.9","rate_down":310000,"rate_up":52000,"spark":[1,3,2,5,4,6],
                "down_24h":2.1e9,"up_24h":3.2e8,"down_total":4.4e10,"up_total":6e9},
               {"client":"Телефон","pubkey":"k2…","container":"amnezia-awg","online":false,"handshake_age":5400,
                "endpoint":null,"rate_down":null,"rate_up":null,"spark":[2,1,0,0],
                "down_24h":4.5e8,"up_24h":6e7,"down_total":9e9,"up_total":1e9}],
             "totals":{"down_24h":2.55e9,"up_24h":3.8e8},
             "dns":{"status":"ok","note":null,"queries":1240,"top":[{"site":"youtube.com","n":410},{"site":"telegram.org","n":220},{"site":"github.com","n":95}],
               "per_user":[]},
             "events":[{"ts":1800080000,"kind":"peer","message":"Ноутбук подключился"}]}
        """.trimIndent()
    }

    private fun shoot(file: String, anim: Boolean, theme: org.amnezia.monitor.data.ThemeInfo, content: @Composable () -> Unit) {
        val ctl = Robolectric.buildActivity(ComponentActivity::class.java)
        ctl.get().setTheme(R.style.Theme_AmneziaMonitor_Main)
        val act = ctl.setup().get()
        act.setContent {
            AmneziaTheme {
                CompositionLocalProvider(
                    LocalAnimations provides anim,
                    LocalAccent provides Color(theme.accent),
                    LocalAccent2 provides Color(theme.accent2),
                ) { NeonBackground(theme, Color(theme.accent)) { content() } }
            }
        }
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        val v = act.window.decorView
        val bmp = Bitmap.createBitmap(v.width, v.height, Bitmap.Config.ARGB_8888)
        v.draw(Canvas(bmp))
        val dir = File(System.getProperty("amnezia.shotsDir") ?: "build/shots").apply { mkdirs() }
        File(dir, "$file.png").outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    @Test
    fun home() {
        assumeTrue(on())
        val ui = HomeUi(servers = servers, hasEndpoint = true, endpointLabel = "Панель ПК · 192.0.2.5:8765", updatedAt = 1800086400000)
        shoot("android_home", false, CountryDetect.DEFAULT) { HomeScreen(ui, {}, {}, {}) }
    }

    @Test
    fun details() {
        assumeTrue(on())
        for ((i, s) in servers.take(4).withIndex()) {
            val t = s.theme!!
            val d = ApiJson.parseDetail(detailJson(s.name, s.host!!))
            shoot("android_detail_${t.key}", false, t) {
                DetailScreen(s.name, DetailUi(detail = d), {}, {}, {}, theme = t, servers = servers, isCurrent = { it.name == s.name })
            }
        }
    }
}
