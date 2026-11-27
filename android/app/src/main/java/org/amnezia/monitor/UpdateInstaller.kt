package org.amnezia.monitor

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.layout.heightIn
import java.io.File

/** Передаёт проверенный APK системному установщику Android; установку подтверждает пользователь. */
object UpdateInstaller {
    /** Возвращает сообщение для пользователя или null, если установщик открыт. */
    fun install(ctx: Context, apk: File): String? {
        if (!ctx.packageManager.canRequestPackageInstalls()) {
            return try {
                ctx.startActivity(
                    Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + ctx.packageName))
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
                "Разрешите этому приложению установку обновлений, вернитесь и нажмите «Обновить» ещё раз: файл уже скачан."
            } catch (_: ActivityNotFoundException) {
                "Разрешите установку из этого приложения в настройках Android и повторите."
            }
        }
        val uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".updates", apk)
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            ctx.startActivity(intent)
            null
        } catch (_: ActivityNotFoundException) {
            "Не удалось открыть установщик Android."
        }
    }
}

@Composable
fun UpdatePrompt(vm: MonitorViewModel) {
    val u = vm.update
    val rel = u.release
    if (!u.prompt || rel == null || !u.available) return
    AlertDialog(
        onDismissRequest = vm::dismissUpdatePrompt,
        title = { Text("Доступна версия ${rel.version}") },
        text = {
            Text(
                (if (rel.hasAsset) "У вас ${u.current}. Что нового:\n\n" else "У вас ${u.current}. Автоустановки для этой версии нет — откройте страницу релиза на GitHub.\n\n") +
                    rel.notes.ifBlank { "Описание не указано." },
                modifier = Modifier.heightIn(max = 320.dp).verticalScroll(rememberScrollState()),
            )
        },
        confirmButton = {
            TextButton(onClick = vm::installUpdate, enabled = rel.hasAsset) { Text("Обновить") }
        },
        dismissButton = {
            Row2(vm)
        },
    )
}

@Composable
private fun Row2(vm: MonitorViewModel) {
    androidx.compose.foundation.layout.Row {
        TextButton(onClick = vm::skipUpdate) { Text("Пропустить версию") }
        TextButton(onClick = vm::dismissUpdatePrompt) { Text("Позже") }
    }
}
