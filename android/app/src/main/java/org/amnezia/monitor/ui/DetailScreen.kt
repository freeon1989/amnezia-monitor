package org.amnezia.monitor.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import org.amnezia.monitor.DetailUi
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.ui.draw.clip
import org.amnezia.monitor.data.CountryDetect
import org.amnezia.monitor.data.ServerSummary
import org.amnezia.monitor.data.ThemeInfo
import org.amnezia.monitor.data.DnsInfo
import org.amnezia.monitor.data.ServerDetail
import org.amnezia.monitor.data.UserRow
import org.amnezia.monitor.data.fmtAgo
import org.amnezia.monitor.data.fmtBytes
import org.amnezia.monitor.data.fmtDuration
import org.amnezia.monitor.data.fmtEventTime
import org.amnezia.monitor.data.fmtMs
import org.amnezia.monitor.data.fmtPct
import org.amnezia.monitor.data.fmtRate

private val hourChoices = listOf(6 to "6 ч", 24 to "24 ч", 72 to "3 д", 168 to "7 д")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DetailScreen(
    name: String,
    state: DetailUi,
    onBack: () -> Unit,
    onRefresh: () -> Unit,
    onHours: (Int) -> Unit,
    theme: ThemeInfo = CountryDetect.DEFAULT,
    servers: List<ServerSummary> = emptyList(),
    isCurrent: (ServerSummary) -> Boolean = { false },
    onSwitch: (ServerSummary) -> Unit = {},
) {
    val detail = state.detail
    Scaffold(
        containerColor = Color.Transparent,
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(detail?.name?.ifBlank { name } ?: name, fontWeight = FontWeight.SemiBold)
                        Text(detail?.host ?: "сервер", color = Muted, style = MaterialTheme.typography.labelMedium)
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Назад") }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Color.Transparent, titleContentColor = TextMain, navigationIconContentColor = TextMain),
            )
        },
    ) { padding ->
        PullToRefreshBox(
            isRefreshing = state.refreshing,
            onRefresh = onRefresh,
            modifier = Modifier.fillMaxSize().padding(padding),
        ) {
            Column(
                Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(PaddingValues(16.dp)),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                HeroStage(theme, LocalAccent.current, LocalAccent2.current)
                if (servers.size > 1) ServerSwitcher(servers, isCurrent, onSwitch)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    hourChoices.forEach { (h, label) ->
                        FilterChip(selected = state.hours == h, onClick = { onHours(h) }, label = { Text(label) })
                    }
                }
                if (state.loading && detail == null) {
                    Row(Modifier.fillMaxWidth().padding(24.dp), horizontalArrangement = Arrangement.Center) {
                        CircularProgressIndicator(color = Indigo)
                    }
                }
                state.error?.let { ErrorBanner(it) }
                if (detail != null) DetailBody(detail)
            }
        }
    }
}

/** Флаги всех серверов: нажатие — переход на другой сервер (фон, героиня и цвета плавно сменяются). */
@Composable
private fun ServerSwitcher(servers: List<ServerSummary>, isCurrent: (ServerSummary) -> Boolean, onSwitch: (ServerSummary) -> Unit) {
    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        servers.forEach { s ->
            val on = isCurrent(s)
            val accent = s.theme?.let { Color(it.accent) } ?: Indigo
            Row(
                Modifier
                    .clip(RoundedCornerShape(14.dp))
                    .background(if (on) accent.copy(alpha = 0.22f) else Card)
                    .border(1.dp, if (on) accent else Line, RoundedCornerShape(14.dp))
                    .clickable(enabled = !on) { onSwitch(s) }
                    .padding(horizontal = 10.dp, vertical = 7.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                FlagWithDot(s.theme?.country, s.status, 26.dp)
                Text(s.name, style = MaterialTheme.typography.labelLarge, color = TextMain, maxLines = 1)
            }
        }
    }
}

@Composable
private fun DetailBody(d: ServerDetail) {
    val c = d.current
    Card(colors = CardDefaults.cardColors(containerColor = Card), border = GlassBorder, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                StatusChip(c.status)
                Text("опрос ${fmtAgo(c.ageSec)}", color = Muted, style = MaterialTheme.typography.labelMedium)
            }
            if (!c.error.isNullOrBlank()) Text(c.error, color = Red)
            if (c.stale) {
                Text("Сервер сейчас недоступен. Цифры ниже — с последнего успешного опроса.", color = Amber, style = MaterialTheme.typography.bodySmall)
            }
            Meter("CPU", c.cpu)
            Meter("Память", c.mem)
            Text(
                "Память ${fmtBytes(c.memUsed)} / ${fmtBytes(c.memTotal)}",
                color = Muted,
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        MiniKpi("Задержка", fmtMs(c.tcpMs), "ping ${fmtMs(c.pingMs)}", Modifier.weight(1f))
        MiniKpi("Доступность", if (c.availability == null) "—" else "${c.availability}%", "средняя ${fmtMs(c.latencyAvg)}", Modifier.weight(1f))
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        MiniKpi("Аптайм", fmtDuration(c.uptimeSec), "load ${c.load1 ?: "—"} / ${c.nproc ?: "—"}", Modifier.weight(1f))
        MiniKpi("Трафик", "↓ ${fmtRate(c.txBps)}", "↑ ${fmtRate(c.rxBps)}", Modifier.weight(1f))
    }
    if (d.containers.isNotEmpty()) {
        SectionLabel("Контейнеры")
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            d.containers.forEach { box ->
                val color = if (box.up) Green else Red
                Text(
                    "${box.name}${box.type?.let { " · $it" } ?: ""}",
                    color = color,
                    modifier = Modifier
                        .background(Card2, RoundedCornerShape(8.dp))
                        .padding(horizontal = 10.dp, vertical = 6.dp),
                    style = MaterialTheme.typography.labelMedium,
                )
            }
        }
    }
    val series = d.series
    if (series != null && (series.cpu.isNotEmpty() || series.downBytes.isNotEmpty())) {
        Card(colors = CardDefaults.cardColors(containerColor = Card), border = GlassBorder, modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                SectionLabel("CPU и память", "CPU ${fmtPct(c.cpu)}")
                LineChart(series.cpu, LocalAccent.current, yMax = 100f)
                LineChart(series.mem, Cyan, yMax = 100f)
                Text("Цвет страны — CPU, бирюзовый — память. Пустые промежутки — не было опроса.", color = Muted, style = MaterialTheme.typography.labelSmall)
                SectionLabel("Трафик интерфейса", "↓ отдан  ↑ принят")
                DualBarChart(series.downBytes, series.upBytes)
                Text(
                    "За период: ↓ ${fmtBytes(d.down24h)}  ↑ ${fmtBytes(d.up24h)}",
                    color = Muted,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    } else {
        Text("Истории для графиков пока нет — дождитесь нескольких опросов.", color = Muted)
    }
    SectionLabel("Пользователи", "${d.users.count { it.online }} онлайн")
    if (d.users.isEmpty()) {
        Text("Пиров WireGuard/AmneziaWG нет в последнем опросе.", color = Muted)
    }
    d.users.forEach { UserCard(it) }
    DnsBlock(d.dns)
    if (d.events.isNotEmpty()) {
        SectionLabel("События")
        Card(colors = CardDefaults.cardColors(containerColor = Card), border = GlassBorder, modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                d.events.take(25).forEach { ev ->
                    Column {
                        Text(fmtEventTime(ev.ts), color = Muted, style = MaterialTheme.typography.labelSmall)
                        Text(ev.message, color = TextMain, style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }
    }
}

@Composable
private fun MiniKpi(title: String, value: String, sub: String, modifier: Modifier = Modifier) {
    Card(colors = CardDefaults.cardColors(containerColor = Card), border = GlassBorder, modifier = modifier) {
        Column(Modifier.padding(12.dp)) {
            Text(title, color = Muted, style = MaterialTheme.typography.labelMedium)
            Text(value, fontWeight = FontWeight.SemiBold)
            Text(sub, color = Muted, style = MaterialTheme.typography.labelSmall)
        }
    }
}

@Composable
private fun UserCard(u: UserRow) {
    Card(colors = CardDefaults.cardColors(containerColor = Card), border = GlassBorder, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(u.client, fontWeight = FontWeight.SemiBold)
                    val sub = listOfNotNull(u.container, u.endpoint, u.pubkey).joinToString(" · ")
                    if (sub.isNotBlank()) Text(sub, color = Muted, style = MaterialTheme.typography.labelSmall)
                }
                StatusChip(if (u.online) "up" else "down", label = if (u.online) "онлайн" else "офлайн")
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Column {
                    Text("↓ ${fmtRate(u.rateDown)}   ↑ ${fmtRate(u.rateUp)}", color = TextMain, style = MaterialTheme.typography.bodySmall)
                    Text(
                        "24 ч ↓ ${fmtBytes(u.down24h)}  ↑ ${fmtBytes(u.up24h)}",
                        color = Muted,
                        style = MaterialTheme.typography.labelSmall,
                    )
                    Text(
                        "всего ↓ ${fmtBytes(u.downTotal)}  ↑ ${fmtBytes(u.upTotal)} · handshake ${fmtDuration(u.handshakeAge)}",
                        color = Muted,
                        style = MaterialTheme.typography.labelSmall,
                    )
                }
                if (u.spark.isNotEmpty()) Sparkline(u.spark, if (u.online) Green else Muted)
            }
        }
    }
}

@Composable
private fun DnsBlock(dns: DnsInfo) {
    SectionLabel("Сайты по DNS", dns.queries?.let { "$it запросов" })
    Card(colors = CardDefaults.cardColors(containerColor = Card), border = GlassBorder, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (dns.status != "ok") {
                Text(dns.note ?: "DNS-журнал недоступен.", color = Amber, style = MaterialTheme.typography.bodySmall)
            }
            if (dns.top.isEmpty() && dns.status == "ok") {
                Text("Запросов за период нет.", color = Muted)
            }
            val max = dns.top.maxOfOrNull { it.count }?.coerceAtLeast(1) ?: 1
            dns.top.forEach { site ->
                Column {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(site.site, modifier = Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
                        Text(site.count.toString(), color = Muted, style = MaterialTheme.typography.labelSmall)
                    }
                    Box(
                        Modifier
                            .padding(top = 3.dp)
                            .fillMaxWidth()
                            .height(8.dp)
                            .background(Card2, RoundedCornerShape(6.dp)),
                    ) {
                        Box(
                            Modifier
                                .fillMaxWidth((site.count.toFloat() / max).coerceIn(0f, 1f))
                                .height(8.dp)
                                .background(LocalAccent.current, RoundedCornerShape(6.dp)),
                        )
                    }
                }
            }
            dns.perUser.forEach { user ->
                Text(user.client, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 6.dp))
                Text(
                    user.top.joinToString(" · ") { "${it.site} (${it.count})" },
                    color = Muted,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

