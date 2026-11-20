package org.amnezia.monitor.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/**
 * Адрес панели, токен чтения и импортированные SSH-пароли лежат в EncryptedSharedPreferences
 * (ключ в Android Keystore). Ключи VPN из Amnezia сюда не попадают.
 */
class SettingsStore(context: Context) {
    private val app = context.applicationContext
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val prefs: SharedPreferences = open()
    private val _state = MutableStateFlow(read())
    val state: StateFlow<AppSettings> = _state.asStateFlow()

    fun update(block: (AppSettings) -> AppSettings) {
        val next = block(_state.value).normalized()
        prefs.edit().putString(KEY, json.encodeToString(next)).apply()
        _state.value = next
    }

    /** Отпечатки ключей хостов. Не трогают поток настроек, чтобы опрос не запускался заново. */
    fun readHostKeys(): Map<String, String> {
        val raw = prefs.getString(HOST_KEYS, null) ?: return emptyMap()
        return try {
            json.decodeFromString<Map<String, String>>(raw)
        } catch (_: Exception) {
            emptyMap()
        }
    }

    fun writeHostKeys(keys: Map<String, String>) {
        prefs.edit().putString(HOST_KEYS, json.encodeToString(keys)).apply()
    }

    private fun read(): AppSettings {
        val raw = prefs.getString(KEY, null) ?: return AppSettings()
        return try {
            json.decodeFromString(AppSettings.serializer(), raw).normalized()
        } catch (_: Exception) {
            AppSettings()
        }
    }

    private fun open(): SharedPreferences {
        return try {
            createEncrypted()
        } catch (_: Exception) {
            app.deleteSharedPreferences(FILE)
            createEncrypted()
        }
    }

    private fun createEncrypted(): SharedPreferences {
        val masterKey = MasterKey.Builder(app)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        return EncryptedSharedPreferences.create(
            app,
            FILE,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    companion object {
        private const val FILE = "amnezia_monitor_secure"
        private const val KEY = "settings"
        private const val HOST_KEYS = "hostkeys"
    }
}
