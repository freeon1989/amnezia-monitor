package org.amnezia.monitor.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.AlertDialog
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.text.font.FontFamily
import org.amnezia.monitor.data.HostKeyMismatch
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import org.amnezia.monitor.HomeUi
import org.amnezia.monitor.data.ServerSummary
import org.amnezia.monitor.data.blockLabel
import org.amnezia.monitor.data.fmtAgo
import org.amnezia.monitor.data.fmtClock
import org.amnezia.monitor.data.fmtMs
import org.amnezia.monitor.data.fmtRate

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    home: HomeUi,
    onRefresh: () -> Unit,
    onOpen: (ServerSummary) -> Unit,
    onSettings: () -> Unit,
    onTrustHostKey: (ServerSummary) -> Unit = {},
    hostKeyMessage: String? = null,
    onDismissHostKeyMessage: () -> Unit = {},
) {
    var confirm by remember { mutableStateOf<ServerSummary?>(null) }
    confirm?.let { srv ->
        val mm = srv.hostKey
        if (mm != null) {
            AlertDialog(
                onDismissRequest = { confirm = null },
                title = { Text("Доверять новому ключу?") },
                text = {
                    Text(
                        "Сервер ${srv.name} (${mm.host}${if (mm.port != 22) ":" + mm.port else ""}).\n\n" +
                            "Новый ключ: ${mm.new.type} ${mm.new.fingerprint}\n\n" +
                            "Подтверждайте, только если ключ на сервере действительно меняли (переустановка, " +
                            "пересоздание у хостера) и отпечаток совпадает с выводом на сервере:\n" +
                            "ssh-keygen -lf /etc/ssh/ssh_host_${mm.new.type.lowercase()}_key.pub",
                    )
                },
                confirmButton = {
                    TextButton(onClick = { confirm = null; onTrustHostKey(srv) }) { Text("Доверять", color = Red) }
                },
                dismissButton = { TextButton(onClick = { confirm = null }) { Text("Отмена") } },
            )
        }
    }
    Scaffold(
        containerColor = Bg,
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("Amnezia Monitor", fontWeight = FontWeight.SemiBold)
                        val sub = when {
                            home.updatedAt != null -> "обновлено ${fmtClock(home.updatedAt)}"
                            home.endpointLabel.isNotBlank() -> home.endpointLabel
                            else -> "только чтение"
                        }
                        Text(sub, style = MaterialTheme.typography.labelMedium, color = Muted)
                    }
                },
                actions = {
                    IconButton(onClick = onRefresh) { Icon(Icons.Filled.Refresh, contentDescription = "Обновить") }
                    IconButton(onClick = onSettings) { Icon(Icons.Filled.Settings, contentDescription = "Настройки") }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Bg, titleContentColor = TextMain, actionIconContentColor = TextMain),
            )
        },
    ) { padding ->
        PullToRefreshBox(
            isRefreshing = home.refreshing,
            onRefresh = onRefresh,
            modifier = Modifier.fillMaxSize().padding(padding),
        ) {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                if (!home.hasEndpoint) {
                    item { WelcomeCard(onSettings) }
                }
                if (home.loading && home.servers.isEmpty()) {
                    item {
                        Row(Modifier.fillMaxWidth().padding(24.dp), horizontalArrangement = Arrangement.Center) {
                            CircularProgressIndicator(color = Indigo)
                        }
                    }
                }
                home.error?.let { err -> item { ErrorBanner(err) } }
                hostKeyMessage?.let { msg ->
                    item {
                        Card(
                            colors = CardDefaults.cardColors(containerColor = Indigo.copy(alpha = 0.14f)),
                            modifier = Modifier.fillMaxWidth().clickable(onClick = onDismissHostKeyMessage),
                        ) { Text(msg, color = TextMain, modifier = Modifier.padding(14.dp)) }
                    }
                }
                if (home.hasEndpoint && !home.loading && home.servers.isEmpty() && home.error == null) {
                    item {
                        Text(
                            "Серверов пока нет. Добавьте их в Amnezia Monitor на компьютере.",
                            color = Muted,
                            modifier = Modifier.padding(8.dp),
                        )
                    }
                }
                items(home.servers, key = { it.localId ?: "dash:${it.name}" }) { server ->
                    ServerCard(server, onClick = { onOpen(server) }, onTrust = { confirm = server })
                }
            }
        }
    }
}

@Composable
private fun WelcomeCard(onSettings: () -> Unit) {
    Card(
        colors = CardDefaults.cardColors(containerColor = Card),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Подключите панель", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Text(
                "Импортируйте серверы из AmneziaVPN (резервная копия или vpn://) или укажите адрес панели на компьютере, например http://192.168.1.104:8765. Пароли SSH сохраняются только в защищённом хранилище телефона.",
                color = Muted,
            )
            TextButton(onClick = onSettings) { Text("Открыть настройки") }
        }
    }
}

@Composable
fun ErrorBanner(text: String) {
    Card(colors = CardDefaults.cardColors(containerColor = Red.copy(alpha = 0.12f)), modifier = Modifier.fillMaxWidth()) {
        Text(text, color = Red, modifier = Modifier.padding(14.dp))
    }
}

@Composable
private fun ServerCard(server: ServerSummary, onClick: () -> Unit, onTrust: () -> Unit) {
    Card(
        colors = CardDefaults.cardColors(containerColor = Card),
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick),
    ) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(server.name, fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.titleMedium)
                    val host = server.host ?: "—"
                    val latency = if (server.tcpMs != null) fmtMs(server.tcpMs) else "—"
                    Text("$host · $latency · ${fmtAgo(server.ageSec)}", color = Muted, style = MaterialTheme.typography.bodySmall)
                }
                StatusChip(server.status)
            }
            Meter("CPU", server.cpu)
            Meter("Память", server.mem)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                val peers = if (server.peersOnline == null && server.peersTotal == null) "—"
                else "${server.peersOnline ?: 0} / ${server.peersTotal ?: 0}"
                Text("Пиры $peers", color = TextMain, style = MaterialTheme.typography.bodyMedium)
                Text(
                    "↓ ${fmtRate(server.txBps)}   ↑ ${fmtRate(server.rxBps)}",
                    color = Muted,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            val blk = blockLabel(server.blockVerdict)
            if (blk != null) {
                val color = when (server.blockVerdict) {
                    "ok" -> Green
                    "blocked_ru", "down" -> Red
                    "partial" -> Amber
                    else -> Muted
                }
                val ago = server.blockTs?.let { fmtAgo(System.currentTimeMillis() / 1000.0 - it) }
                Text(if (ago != null) "$blk · $ago" else blk, color = color, style = MaterialTheme.typography.bodySmall)
            }
            val mm = server.hostKey
            if (mm != null) {
                HostKeyBlock(mm, onTrust)
            } else if (!server.error.isNullOrBlank() && server.status != "up") {
                Text(server.error, color = Red, style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun HostKeyBlock(mm: HostKeyMismatch, onTrust: () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            "Ключ SSH сервера не совпадает с сохранённым. Опрос остановлен до входа — пароль не отправлялся." +
                if (mm.typeChanged) " Сервер показал ключ другого типа." else "",
            color = Red,
            style = MaterialTheme.typography.bodySmall,
        )
        Text("Было:", color = Muted, style = MaterialTheme.typography.labelMedium)
        for (k in mm.old) {
            Text("${k.type} ${k.fingerprint}", color = TextMain, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
        }
        Text("Сейчас:", color = Muted, style = MaterialTheme.typography.labelMedium)
        Text("${mm.new.type} ${mm.new.fingerprint}", color = TextMain, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
        TextButton(onClick = onTrust) { Text("Доверять новому ключу", color = Red) }
    }
}
