package org.amnezia.monitor

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import android.net.Uri
import org.amnezia.monitor.data.AmneziaImport
import org.amnezia.monitor.data.AppSettings
import org.amnezia.monitor.data.CountryDetect
import org.amnezia.monitor.data.GeoIp
import org.amnezia.monitor.data.ThemeInfo
import org.amnezia.monitor.data.Endpoint
import org.amnezia.monitor.data.HostKeyBook
import org.amnezia.monitor.data.ImportedServer
import org.amnezia.monitor.data.MonitorApi
import org.amnezia.monitor.data.ProbePrev
import org.amnezia.monitor.data.ServerDetail
import org.amnezia.monitor.data.ServerSummary
import org.amnezia.monitor.data.SettingsStore
import org.amnezia.monitor.data.SshProbe
import org.amnezia.monitor.data.LocalHistory
import java.io.File
import org.amnezia.monitor.data.friendlyError
import java.io.ByteArrayOutputStream
import java.util.UUID

data class HomeUi(
    val servers: List<ServerSummary> = emptyList(),
    val loading: Boolean = false,
    val refreshing: Boolean = false,
    val error: String? = null,
    val updatedAt: Long? = null,
    val hasEndpoint: Boolean = false,
    val endpointLabel: String = "",
)

data class DetailUi(
    val detail: ServerDetail? = null,
    val loading: Boolean = false,
    val refreshing: Boolean = false,
    val error: String? = null,
    val hours: Int = 24,
)

class MonitorViewModel(
    app: Application,
    private val store: SettingsStore,
    private val api: MonitorApi,
) : AndroidViewModel(app) {

    var settings by mutableStateOf(store.state.value)
        private set
    var home by mutableStateOf(HomeUi())
        private set
    var detail by mutableStateOf(DetailUi())
        private set

    var importMessage by mutableStateOf<String?>(null)
        private set

    /** Итог «Доверять новому ключу» для баннера на главном экране. */
    var hostKeyMessage by mutableStateOf<String?>(null)
        private set

    private val gate = Mutex()
    private var loop: Job? = null
    private var foreground = false
    private var openServer: String? = null
    private var openLocal: String? = null
    private var hours: Int = 24
    private val localSnaps = mutableMapOf<String, ServerDetail>()
    private val probePrev = mutableMapOf<String, ProbePrev>()
    private val geo = GeoIp(app)
    /** История прямых (импортированных) серверов — для графиков и доступности, как у панели ПК. */
    private val history = LocalHistory(File(app.filesDir, "local_history.v1"))
    private val geoPending = mutableSetOf<String>()

    init {
        viewModelScope.launch {
            store.state.collect { next ->
                val prev = settings
                settings = next
                // Оформление (анимации, GeoIP, страна) не требует нового опроса серверов
                if (appearanceOnly(prev, next) && prev !== next) {
                    home = home.copy(servers = withThemes(home.servers, next))
                    return@collect
                }
                val ep = next.active
                home = home.copy(
                    hasEndpoint = ep != null || next.imported.isNotEmpty(),
                    endpointLabel = when {
                        ep != null -> ep.label.ifBlank { ep.baseUrl }
                        next.imported.isNotEmpty() -> "AmneziaVPN"
                        else -> ""
                    },
                )
                if (foreground) restartLoop()
            }
        }
    }

    fun setForeground(on: Boolean) {
        foreground = on
        if (on) restartLoop() else {
            loop?.cancel()
            viewModelScope.launch(Dispatchers.IO) { history.save(force = true) }
        }
    }

    /** Снимок прямого сервера с графиками и доступностью за выбранный период. */
    private fun localDetail(id: String): ServerDetail? {
        val snap = localSnaps[id] ?: return null
        val now = System.currentTimeMillis() / 1000
        val (avail, lat) = history.stats(id, hours, now)
        return snap.copy(
            series = history.series(id, hours, now),
            current = snap.current.copy(availability = avail, latencyAvg = lat),
        )
    }

    fun watchDetail(name: String?) {
        openLocal = null
        openServer = name
        if (name != null) {
            viewModelScope.launch { refreshDetail(manual = detail.detail?.name != name) }
        }
    }

    fun watchLocal(id: String?) {
        openServer = null
        openLocal = id
        if (id == null) return
        val snap = localDetail(id)
        if (snap != null) detail = DetailUi(detail = snap, hours = hours)
        viewModelScope.launch { refresh(manual = snap == null) }
    }

    /** Уход с экрана сервера. При переключении A → B экран A закрывается позже — не сбрасываем B. */
    fun unwatch(name: String? = null, localId: String? = null) {
        if (name != null && openServer == name) openServer = null
        if (localId != null && openLocal == localId) openLocal = null
    }

    fun setHours(value: Int) {
        val h = value.coerceIn(1, 168)
        if (h == hours) return
        hours = h
        detail = detail.copy(hours = h)
        openLocal?.let { id ->
            localDetail(id)?.let { detail = detail.copy(detail = it) }
            return
        }
        viewModelScope.launch { refreshDetail(manual = true) }
    }

    fun importText(text: String) {
        viewModelScope.launch {
            try {
                applyImport(text)
            } catch (e: Exception) {
                importMessage = e.message ?: "Не удалось прочитать конфиг"
            }
        }
    }

    fun importUri(uri: Uri) {
        viewModelScope.launch {
            try {
                val text = withContext(Dispatchers.IO) { readUri(uri) }
                applyImport(text)
            } catch (e: Exception) {
                importMessage = e.message ?: "Не удалось открыть файл"
            }
        }
    }

    fun deleteImported(id: String) {
        localSnaps.remove(id)
        probePrev.remove(id)
        history.forget(id)
        store.update { cur -> cur.copy(imported = cur.imported.filterNot { it.id == id }) }
        if (openLocal == id) openLocal = null
    }

    private fun applyImport(text: String) {
        val parsed = AmneziaImport.parse(text)
        var added = 0
        store.update { cur ->
            val before = cur.imported.map { it.host.lowercase() to it.port }.toSet()
            val merged = AmneziaImport.merge(cur.imported, parsed) { newId() }
            added = merged.count { (it.host.lowercase() to it.port) !in before }
            cur.copy(imported = merged)
        }
        val withSsh = parsed.count { it.hasSshAuth }
        importMessage = "Серверов в файле: ${parsed.size}. Новых адресов: $added. С паролем SSH: $withSsh."
    }

    private fun readUri(uri: Uri): String {
        val app = getApplication<Application>()
        app.contentResolver.openInputStream(uri)?.use { input ->
            val buf = ByteArrayOutputStream()
            val tmp = ByteArray(8192)
            var total = 0
            while (true) {
                val n = input.read(tmp)
                if (n < 0) break
                total += n
                if (total > 1_500_000) throw IllegalArgumentException("Файл слишком большой")
                buf.write(tmp, 0, n)
            }
            return buf.toString(Charsets.UTF_8.name())
        } ?: throw IllegalArgumentException("Не удалось открыть файл")
    }

    /** Изменились только поля оформления — опрос не перезапускаем. */
    private fun appearanceOnly(a: AppSettings, b: AppSettings): Boolean =
        a.copy(animations = b.animations, geoip = b.geoip, imported = a.imported.map { it.copy(country = "") }) ==
            b.copy(imported = b.imported.map { it.copy(country = "") })

    fun setAnimations(on: Boolean) = store.update { it.copy(animations = on) }

    fun setGeoip(on: Boolean) = store.update { it.copy(geoip = on) }

    fun setImportedCountry(id: String, country: String) = store.update { cur ->
        cur.copy(imported = cur.imported.map { if (it.id == id) it.copy(country = CountryDetect.normalize(country)) else it })
    }

    /** Оформление сервера: панель ПК (её поле theme) > вручную > по названию > GeoIP (если включён). */
    private fun themeOf(s: ServerSummary, cfg: AppSettings): ThemeInfo {
        val panel = s.panelCountry
        if (!panel.isNullOrEmpty()) return CountryDetect.theme(panel, s.panelCountrySource ?: "panel")
        val manual = s.localId?.let { id -> cfg.imported.firstOrNull { it.id == id }?.country }
        var geoCc: String? = null
        val host = s.host
        if (cfg.geoip && host != null && CountryDetect.isPublicIpv4(host)) {
            geoCc = geo.cached(host)
            if (geoCc == null && CountryDetect.normalize(manual).isEmpty() && CountryDetect.byName(s.name) == null) {
                scheduleGeo(host)
            }
        }
        return CountryDetect.themeFor(s.name, manual, geoCc)
    }

    private fun withThemes(list: List<ServerSummary>, cfg: AppSettings): List<ServerSummary> =
        list.map { it.copy(theme = themeOf(it, cfg)) }

    private fun scheduleGeo(host: String) {
        if (!geoPending.add(host)) return
        viewModelScope.launch {
            withContext(Dispatchers.IO) { geo.lookup(host) }
            geoPending.remove(host)
            home = home.copy(servers = withThemes(home.servers, store.state.value))
        }
    }

    /** Тема для экрана сервера (по имени из панели или id импортированного). */
    fun themeFor(name: String?, localId: String?): ThemeInfo {
        val s = home.servers.firstOrNull { if (localId != null) it.localId == localId else it.localId == null && it.name == name }
        return s?.theme ?: CountryDetect.themeFor(name ?: settings.imported.firstOrNull { it.id == localId }?.name,
            settings.imported.firstOrNull { it.id == localId }?.country)
    }

    fun pull() {
        viewModelScope.launch { refresh(manual = true) }
    }

    /**
     * Заменить сохранённый ключ хоста. Вызывается только после подтверждения в диалоге.
     * Ключ берётся с сервера заново и записывается, только если его отпечаток совпадает с тем,
     * что пользователь видел и подтвердил.
     */
    fun trustHostKey(server: ServerSummary) {
        val mm = server.hostKey ?: return
        viewModelScope.launch {
            val blob = withContext(Dispatchers.IO) {
                runCatching { SshProbe.fetchMatchingKey(mm.host, mm.port, mm.new.fingerprint) }.getOrNull()
            }
            if (blob == null) {
                hostKeyMessage = "Ключ ${mm.host} не заменён: сервер сейчас показывает другой ключ или недоступен."
                return@launch
            }
            gate.withLock {
                val keys = store.readHostKeys().toMutableMap()
                HostKeyBook.replace(keys, HostKeyBook.entryName(mm.host, mm.port), blob)
                store.writeHostKeys(keys)
            }
            hostKeyMessage = "Новый ключ ${mm.host} сохранён: ${mm.new.type} ${mm.new.fingerprint}"
            refresh(manual = true)
        }
    }

    fun clearHostKeyMessage() {
        hostKeyMessage = null
    }

    fun updateSettings(block: (AppSettings) -> AppSettings) = store.update(block)

    fun upsertEndpoint(ep: Endpoint, makeActive: Boolean) {
        store.update { cur ->
            val exists = cur.endpoints.any { it.id == ep.id }
            val list = if (exists) cur.endpoints.map { if (it.id == ep.id) ep else it } else cur.endpoints + ep
            cur.copy(
                endpoints = list,
                activeId = if (makeActive || cur.activeId == null) ep.id else cur.activeId,
            )
        }
    }

    fun deleteEndpoint(id: String) {
        store.update { cur ->
            val list = cur.endpoints.filterNot { it.id == id }
            cur.copy(endpoints = list, activeId = if (cur.activeId == id) list.firstOrNull()?.id else cur.activeId)
        }
    }

    fun setActive(id: String) = store.update { it.copy(activeId = id) }

    fun setInterval(sec: Int) = store.update { it.copy(intervalSec = sec) }

    fun newId(): String = UUID.randomUUID().toString()

    fun testEndpoint(ep: Endpoint, onResult: (String) -> Unit) {
        viewModelScope.launch {
            val msg = try {
                withContext(Dispatchers.IO) {
                    val health = api.health(ep)
                    if (!health.app.isNullOrBlank() && health.app != "amnezia-monitor") {
                        "Ответ не от Amnezia Monitor (${health.app})."
                    } else {
                        val n = api.servers(ep).size
                        "Связь есть. Серверов в панели: $n."
                    }
                }
            } catch (e: Exception) {
                friendlyError(e)
            }
            onResult(msg)
        }
    }

    private fun restartLoop() {
        loop?.cancel()
        val sec = store.state.value.intervalSec.coerceIn(15, 300)
        loop = viewModelScope.launch {
            while (isActive) {
                refresh(manual = false)
                delay(sec * 1000L)
            }
        }
    }

    private suspend fun refresh(manual: Boolean) {
        gate.withLock {
            val cur = store.state.value
            val ep = cur.active
            val imported = cur.imported
            if (ep == null && imported.isEmpty()) {
                home = HomeUi()
                return
            }
            home = home.copy(
                hasEndpoint = true,
                endpointLabel = ep?.label?.ifBlank { ep.baseUrl } ?: "AmneziaVPN",
                refreshing = manual && home.servers.isNotEmpty(),
                loading = home.servers.isEmpty(),
            )
            val name = openServer
            if (name != null && manual) {
                detail = detail.copy(refreshing = detail.detail != null, loading = detail.detail == null)
            }
            var dash = emptyList<ServerSummary>()
            var dashError: String? = null
            if (ep != null) {
                try {
                    dash = withContext(Dispatchers.IO) { api.servers(ep) }
                } catch (e: Exception) {
                    dashError = friendlyError(e)
                }
            }
            val covered = if (dashError == null && ep != null) {
                dash.mapNotNull { it.host?.lowercase() }.toSet()
            } else {
                emptySet()
            }
            val localRows = mutableListOf<ServerSummary>()
            if (imported.isNotEmpty()) {
                val keys = store.readHostKeys().toMutableMap()
                var keysChanged = false
                for (srv in imported) {
                    if (srv.host.lowercase() in covered) continue
                    val prevTs = probePrev[srv.id]?.tsMs
                    val polled = withContext(Dispatchers.IO) {
                        val before = keys.toMap()
                        val result = SshProbe.poll(srv, probePrev[srv.id], keys)
                        if (keys != before) keysChanged = true
                        val ping = SshProbe.icmpPing(srv.host)
                        result.copy(detail = result.detail.copy(current = result.detail.current.copy(pingMs = ping)))
                    }
                    probePrev[srv.id] = polled.prev
                    localSnaps[srv.id] = polled.detail
                    history.record(srv.id, historySample(polled, prevTs))
                    localRows.add(polled.summary)
                }
                if (keysChanged) store.writeHostKeys(keys)
                withContext(Dispatchers.IO) { history.save() }
            }
            home = home.copy(
                servers = withThemes(dash + localRows, cur),
                error = dashError,
                updatedAt = System.currentTimeMillis(),
                loading = false,
                refreshing = false,
                hasEndpoint = true,
            )
            val localId = openLocal
            if (localId != null) {
                val snap = localDetail(localId)
                if (snap != null) detail = DetailUi(detail = snap, hours = hours)
            } else if (name != null && ep != null && dashError == null) {
                loadDetailLocked(ep, name, manual)
            } else if (name != null && dashError != null) {
                detail = detail.copy(error = dashError, loading = false, refreshing = false, hours = hours)
            }
        }
    }

    private fun historySample(p: org.amnezia.monitor.data.LocalPoll, prevTs: Long?): LocalHistory.Sample {
        val c = p.detail.current
        // при ошибке опроса prev не меняется — время берём текущее
        val nowMs = if (p.summary.status == "up") p.prev.tsMs else System.currentTimeMillis()
        // трафик между опросами: скорость × время (длинные паузы — приложение было закрыто — не считаем)
        val dt = if (prevTs == null) null else ((nowMs - prevTs) / 1000.0).takeIf { it > 0 && it <= 900 }
        return LocalHistory.Sample(
            ts = nowMs / 1000,
            up = p.summary.status == "up",
            tcpMs = c.tcpMs,
            pingMs = c.pingMs,
            cpu = c.cpu,
            mem = c.mem,
            online = p.summary.peersOnline,
            downBytes = if (dt == null) null else c.txBps?.times(dt),
            upBytes = if (dt == null) null else c.rxBps?.times(dt),
        )
    }

    private suspend fun refreshDetail(manual: Boolean) {
        gate.withLock {
            val ep = store.state.value.active ?: return
            val name = openServer ?: return
            loadDetailLocked(ep, name, manual)
        }
    }

    private suspend fun loadDetailLocked(ep: Endpoint, name: String, manual: Boolean) {
        detail = detail.copy(
            hours = hours,
            loading = detail.detail == null || detail.detail?.name != name,
            refreshing = manual && detail.detail?.name == name,
            error = if (detail.detail?.name == name) detail.error else null,
        )
        try {
            val loaded = withContext(Dispatchers.IO) { api.server(ep, name, hours) }
            if (openServer != name) return
            detail = DetailUi(detail = loaded, hours = hours)
        } catch (e: Exception) {
            if (openServer != name) return
            detail = detail.copy(error = friendlyError(e), loading = false, refreshing = false, hours = hours)
        }
    }

    companion object {
        fun factory(app: Application): ViewModelProvider.Factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T {
                return MonitorViewModel(app, SettingsStore(app), MonitorApi()) as T
            }
        }
    }
}
