package org.amnezia.monitor.data

import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import javax.net.ssl.SSLException

fun friendlyError(e: Throwable): String = when (e) {
    is ApiException -> when (e.code) {
        401, 403 -> "Неверный токен. Скопируйте его из «Настройки → Телефон» на компьютере."
        404 -> "Такого сервера нет в панели."
        else -> e.message?.takeIf { it.isNotBlank() } ?: "Панель вернула ошибку"
    }
    is UnknownHostException -> "Адрес панели не найден. Проверьте URL."
    is SocketTimeoutException -> "Панель не ответила вовремя."
    is ConnectException -> "Нет связи с панелью. Amnezia Monitor должен быть запущен, а телефон — в той же сети или VPN."
    is SSLException -> "Ошибка HTTPS. Для панели без сертификата укажите адрес http://."
    is IllegalArgumentException -> e.message ?: "Проверьте адрес"
    else -> {
        val msg = e.message.orEmpty()
        when {
            msg.contains("CLEARTEXT", ignoreCase = true) || msg.contains("Cleartext", ignoreCase = true) ->
                "Система заблокировала HTTP. Для локальной панели нужен адрес http:// и эта сборка приложения."
            msg.contains("Failed to connect", ignoreCase = true) ||
                msg.contains("Connection refused", ignoreCase = true) ||
                msg.contains("ECONNREFUSED", ignoreCase = true) ->
                "Нет связи с панелью. Проверьте адрес, порт и что Amnezia Monitor слушает сеть."
            else -> "Не удалось связаться с панелью"
        }
    }
}
