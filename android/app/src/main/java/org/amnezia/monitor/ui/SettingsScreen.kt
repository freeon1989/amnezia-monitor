package org.amnezia.monitor.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import org.amnezia.monitor.MonitorViewModel
import org.amnezia.monitor.data.Endpoint
import org.amnezia.monitor.data.ImportedServer
import org.amnezia.monitor.data.cleanToken
import org.amnezia.monitor.data.normalizeBaseUrl

private val intervals = listOf(15, 30, 60, 120)

@Composable
private fun ImportFromAmneziaCard(
    servers: List<ImportedServer>,
    message: String?,
    paste: String,
    onPaste: (String) -> Unit,
    onPick: () -> Unit,
    onImportText: () -> Unit,
    onDelete: (ImportedServer) -> Unit,
) {
    Card(colors = CardDefaults.cardColors(containerColor = Card), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Импорт из AmneziaVPN", fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.titleMedium)
            Text(
                "Список серверов AmneziaVPN лежит в закрытой памяти приложения и зашифрован. Другое приложение его не видит, root для этого не нужен и не обязателен. Сделайте резервную копию в AmneziaVPN (Настройки → резервная копия) и выберите файл здесь, либо вставьте ссылку vpn://.",
                color = Muted,
                style = MaterialTheme.typography.bodySmall,
            )
            Text(
                "Полная копия содержит пароль SSH: он сохраняется только в защищённом хранилище телефона и используется для команд чтения (нагрузка, пиры, трафик). Клиентский .conf или vpn:// без пароля добавит адрес. Если этот хост уже есть в панели на компьютере, цифры берутся оттуда.",
                color = Muted,
                style = MaterialTheme.typography.bodySmall,
            )
            OutlinedButton(onClick = onPick) { Text("Выбрать файл") }
            OutlinedTextField(
                value = paste,
                onValueChange = onPaste,
                label = { Text("Или вставьте vpn:// / JSON") },
                modifier = Modifier.fillMaxWidth(),
                minLines = 2,
                maxLines = 6,
            )
            OutlinedButton(onClick = onImportText, enabled = paste.isNotBlank()) { Text("Импортировать") }
            if (message != null) Text(message, color = TextMain, style = MaterialTheme.typography.bodySmall)
            servers.forEach { srv ->
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(srv.name, fontWeight = FontWeight.SemiBold)
                        Text(
                            "${srv.user}@${srv.host}:${srv.port} · " +
                                if (srv.hasSshAuth) "пароль SSH сохранён" else "только адрес",
                            color = Muted,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    TextButton(onClick = { onDelete(srv) }) { Text("Удалить") }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(vm: MonitorViewModel, onBack: () -> Unit) {
    val settings = vm.settings
    var editing by remember { mutableStateOf<Endpoint?>(null) }
    var creating by remember { mutableStateOf(false) }
    var pendingDelete by remember { mutableStateOf<Endpoint?>(null) }
    var formError by remember { mutableStateOf<String?>(null) }
    var testMsg by remember { mutableStateOf<String?>(null) }
    var testing by remember { mutableStateOf(false) }
    var paste by remember { mutableStateOf("") }
    var dropImport by remember { mutableStateOf<ImportedServer?>(null) }
    val pickFile = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) vm.importUri(uri)
    }

    Scaffold(
        containerColor = Bg,
        topBar = {
            TopAppBar(
                title = { Text("Настройки", fontWeight = FontWeight.SemiBold) },
                navigationIcon = {
                    IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Назад") }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = Bg,
                    titleContentColor = TextMain,
                    navigationIconContentColor = TextMain,
                ),
            )
        },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            ImportFromAmneziaCard(
                servers = settings.imported,
                message = vm.importMessage,
                paste = paste,
                onPaste = { paste = it },
                onPick = { pickFile.launch(arrayOf("*/*")) },
                onImportText = { vm.importText(paste) },
                onDelete = { dropImport = it },
            )
            Text("Интервал обновления, пока приложение открыто", color = Muted)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                intervals.forEach { sec ->
                    FilterChip(
                        selected = settings.intervalSec == sec,
                        onClick = { vm.setInterval(sec) },
                        label = { Text("$sec с") },
                    )
                }
            }
            SectionLabel("Панели")
            if (settings.endpoints.isEmpty()) {
                Text("Пока ни одного адреса. Добавьте панель с компьютера, где запущен Amnezia Monitor.", color = Muted)
            }
            settings.endpoints.forEach { ep ->
                Card(colors = CardDefaults.cardColors(containerColor = Card), modifier = Modifier.fillMaxWidth()) {
                    Row(
                        Modifier.fillMaxWidth().padding(start = 4.dp, end = 4.dp, top = 4.dp, bottom = 4.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        RadioButton(selected = settings.active?.id == ep.id, onClick = { vm.setActive(ep.id) })
                        Column(Modifier.weight(1f)) {
                            Text(ep.label.ifBlank { "Панель" }, fontWeight = FontWeight.SemiBold)
                            Text(ep.baseUrl, color = Muted, style = MaterialTheme.typography.bodySmall)
                            Text(
                                if (ep.token.isBlank()) "токен не задан" else "токен сохранён",
                                color = Muted,
                                style = MaterialTheme.typography.labelSmall,
                            )
                        }
                        IconButton(onClick = { pendingDelete = ep }) {
                            Icon(Icons.Filled.Delete, contentDescription = "Удалить", tint = Red)
                        }
                    }
                    TextButton(onClick = {
                        editing = ep
                        creating = false
                        formError = null
                        testMsg = null
                    }, modifier = Modifier.padding(start = 12.dp, bottom = 4.dp)) { Text("Изменить") }
                }
            }
            OutlinedButton(onClick = {
                editing = Endpoint(id = vm.newId(), label = "", baseUrl = "", token = "")
                creating = true
                formError = null
                testMsg = null
            }) { Text("Добавить панель") }

            val draft = editing
            if (draft != null) {
                EndpointForm(
                    initial = draft,
                    isNew = creating,
                    error = formError,
                    testMsg = testMsg,
                    testing = testing,
                    onDismiss = { editing = null },
                    onTest = { ep ->
                        testing = true
                        testMsg = null
                        formError = null
                        vm.testEndpoint(ep) { msg ->
                            testing = false
                            testMsg = msg
                        }
                    },
                    onSave = { ep ->
                        try {
                            val clean = ep.copy(
                                label = ep.label.trim().take(40),
                                baseUrl = normalizeBaseUrl(ep.baseUrl),
                                token = cleanToken(ep.token),
                            )
                            vm.upsertEndpoint(clean, makeActive = creating || settings.active == null)
                            editing = null
                        } catch (e: IllegalArgumentException) {
                            formError = e.message
                        }
                    },
                )
            }

            Card(colors = CardDefaults.cardColors(containerColor = Card), modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Как открыть данные с компьютера", fontWeight = FontWeight.SemiBold)
                    Text(
                        "На ПК: Amnezia Monitor → Настройки → Телефон → разрешить чтение в локальной сети. Скопируйте адрес вида http://192.168.1.104:8765 и токен. Разрешите программу в брандмауэре для частной сети.",
                        color = Muted,
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Text(
                        "Пока панель слушает только 127.0.0.1, телефон её не видит. Альтернатива — VPN до этого компьютера или SSH-туннель. Канал HTTP без шифрования: не открывайте порт в интернет. Пароли SSH остаются на компьютере.",
                        color = Muted,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }
    }

    dropImport?.let { srv ->
        AlertDialog(
            onDismissRequest = { dropImport = null },
            title = { Text("Убрать сервер из мониторинга?") },
            text = { Text("${srv.name}\n${srv.host}") },
            confirmButton = {
                TextButton(onClick = {
                    vm.deleteImported(srv.id)
                    dropImport = null
                }) { Text("Удалить") }
            },
            dismissButton = { TextButton(onClick = { dropImport = null }) { Text("Отмена") } },
        )
    }

    pendingDelete?.let { ep ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("Удалить панель?") },
            text = { Text(ep.baseUrl) },
            confirmButton = {
                TextButton(onClick = {
                    vm.deleteEndpoint(ep.id)
                    if (editing?.id == ep.id) editing = null
                    pendingDelete = null
                }) { Text("Удалить") }
            },
            dismissButton = { TextButton(onClick = { pendingDelete = null }) { Text("Отмена") } },
        )
    }
}

@Composable
private fun EndpointForm(
    initial: Endpoint,
    isNew: Boolean,
    error: String?,
    testMsg: String?,
    testing: Boolean,
    onDismiss: () -> Unit,
    onTest: (Endpoint) -> Unit,
    onSave: (Endpoint) -> Unit,
) {
    var label by remember(initial.id) { mutableStateOf(initial.label) }
    var url by remember(initial.id) { mutableStateOf(initial.baseUrl) }
    var token by remember(initial.id) { mutableStateOf(initial.token) }
    var showToken by remember(initial.id) { mutableStateOf(false) }
    var localError by remember(initial.id) { mutableStateOf<String?>(null) }
    val current = Endpoint(id = initial.id, label = label, baseUrl = url.trim(), token = token)

    Card(colors = CardDefaults.cardColors(containerColor = Card2), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(if (isNew) "Новая панель" else "Изменить панель", fontWeight = FontWeight.SemiBold)
            OutlinedTextField(
                value = label,
                onValueChange = { label = it },
                label = { Text("Название") },
                placeholder = { Text("Дом") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = url,
                onValueChange = { url = it },
                label = { Text("Адрес панели") },
                placeholder = { Text("http://192.168.1.104:8765") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                label = { Text("Токен чтения") },
                placeholder = { Text("не нужен для туннеля на 127.0.0.1") },
                singleLine = true,
                visualTransformation = if (showToken) VisualTransformation.None else PasswordVisualTransformation(),
                modifier = Modifier.fillMaxWidth(),
            )
            TextButton(onClick = { showToken = !showToken }) {
                Text(if (showToken) "Скрыть токен" else "Показать токен")
            }
            val shownError = localError ?: error
            if (shownError != null) Text(shownError, color = Red, style = MaterialTheme.typography.bodySmall)
            if (testMsg != null) Text(testMsg, color = TextMain, style = MaterialTheme.typography.bodySmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { onSave(current) }) { Text("Сохранить") }
                OutlinedButton(enabled = !testing, onClick = {
                    try {
                        val ep = current.copy(baseUrl = normalizeBaseUrl(current.baseUrl), token = cleanToken(current.token))
                        localError = null
                        onTest(ep)
                    } catch (e: IllegalArgumentException) {
                        localError = e.message
                    }
                }) { Text(if (testing) "Проверяю…" else "Проверить") }
                TextButton(onClick = onDismiss) { Text("Отмена") }
            }
        }
    }
}
