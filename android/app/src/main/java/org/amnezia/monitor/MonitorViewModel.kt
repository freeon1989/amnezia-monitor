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
import org.amnezia.monitor.data.AppSettings
import org.amnezia.monitor.data.Endpoint
import org.amnezia.monitor.data.MonitorApi
import org.amnezia.monitor.data.ServerDetail
import org.amnezia.monitor.data.ServerSummary
import org.amnezia.monitor.data.SettingsStore
import org.amnezia.monitor.data.friendlyError
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

    private val gate = Mutex()
    private var loop: Job? = null
    private var foreground = false
    private var openServer: String? = null
    private var hours: Int = 24

    init {
        viewModelScope.launch {
            store.state.collect { next ->
                settings = next
                val ep = next.active
                home = home.copy(
                    hasEndpoint = ep != null,
                    endpointLabel = ep?.label?.ifBlank { ep.baseUrl } ?: "",
                )
                if (foreground) restartLoop()
            }
        }
    }

    fun setForeground(on: Boolean) {
        foreground = on
        if (on) restartLoop() else loop?.cancel()
    }

    fun watchDetail(name: String?) {
        openServer = name
        if (name != null) {
            viewModelScope.launch { refreshDetail(manual = detail.detail?.name != name) }
        }
    }

    fun setHours(value: Int) {
        val h = value.coerceIn(1, 168)
        if (h == hours) return
        hours = h
        detail = detail.copy(hours = h)
        viewModelScope.launch { refreshDetail(manual = true) }
    }

    fun pull() {
        viewModelScope.launch { refresh(manual = true) }
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
            val ep = store.state.value.active
            if (ep == null) {
                home = HomeUi()
                return
            }
            home = home.copy(
                hasEndpoint = true,
                endpointLabel = ep.label.ifBlank { ep.baseUrl },
                refreshing = manual && home.servers.isNotEmpty(),
                loading = home.servers.isEmpty(),
            )
            val name = openServer
            if (name != null && manual) {
                detail = detail.copy(refreshing = detail.detail != null, loading = detail.detail == null)
            }
            try {
                val list = withContext(Dispatchers.IO) { api.servers(ep) }
                home = home.copy(
                    servers = list,
                    error = null,
                    updatedAt = System.currentTimeMillis(),
                    loading = false,
                    refreshing = false,
                )
            } catch (e: Exception) {
                val text = friendlyError(e)
                home = home.copy(error = text, loading = false, refreshing = false)
                if (name != null) {
                    detail = detail.copy(error = text, loading = false, refreshing = false, hours = hours)
                }
                return
            }
            if (name != null) loadDetailLocked(ep, name, manual)
        }
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
