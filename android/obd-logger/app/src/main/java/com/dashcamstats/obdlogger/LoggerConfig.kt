package com.dashcamstats.obdlogger

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import java.util.UUID

data class LoggerConfig(
    val enabled: Boolean,
    val ownershipTransferred: Boolean,
    val adapterAddress: String,
    val vehicleId: String,
    val loggerId: String,
    val voltageOn: Double = 13.2,
    val voltageOff: Double = 13.0,
    val offGraceSeconds: Long = 30,
    val parkedIntervalSeconds: Long = 30,
    val voltageOnlyMode: Boolean = false,
    val webhookEnabled: Boolean = true,
    val webhookUrl: String = "http://192.168.1.16:8199/api/ingest/webhook",
    val webhookApiKey: String = "qScVUWaO4qK575RsarARb3cr7zi1Y7RIt_4-mawkRxg",
    val backupAwakeSeconds: Int = 1200,
    val idleAwakeSeconds: Int = 300,
) {
    val thresholdConfigurationValid: Boolean
        get() = voltageOn in 10.0..16.0 && voltageOff in 10.0..16.0 &&
            voltageOff < voltageOn && offGraceSeconds in 0L..300L &&
            parkedIntervalSeconds in 15L..3_600L &&
            backupAwakeSeconds in 30..3_600 &&
            idleAwakeSeconds in 15..3_600

    val canRun: Boolean
        get() = enabled && ownershipTransferred && adapterAddress.matches(MAC) &&
            vehicleId.matches(VEHICLE_ID) && loggerId.matches(ID) && thresholdConfigurationValid

    companion object {
        private val MAC = Regex("^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
        private val VEHICLE_ID = Regex("^[a-z0-9][a-z0-9_-]{0,63}$")
        private val ID = Regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        const val DEFAULT_WEBHOOK_URL = "http://192.168.1.16:8199/api/ingest/webhook"
        const val DEFAULT_WEBHOOK_API_KEY = "qScVUWaO4qK575RsarARb3cr7zi1Y7RIt_4-mawkRxg"
        const val DEFAULT_BACKUP_AWAKE_SECONDS = 1200
        const val DEFAULT_IDLE_AWAKE_SECONDS = 300
    }
}

object LoggerPreferences {
    private const val NAME = "obd_logger"

    fun load(context: Context): LoggerConfig {
        val prefs = context.getSharedPreferences(NAME, Context.MODE_PRIVATE)
        val loggerId = if (prefs.contains("logger_id")) {
            prefs.getString("logger_id", "")?.trim().orEmpty()
        } else {
            generatedLoggerId().also { generated ->
                check(prefs.edit().putString("logger_id", generated).commit()) {
                    "could not persist the logger identity"
                }
            }
        }
        val savedUrl = prefs.getString("webhook_url", LoggerConfig.DEFAULT_WEBHOOK_URL)?.trim().orEmpty()
        val savedApiKey = prefs.getString("webhook_api_key", LoggerConfig.DEFAULT_WEBHOOK_API_KEY)?.trim().orEmpty()
        return LoggerConfig(
            enabled = prefs.getBoolean("enabled", false),
            ownershipTransferred = prefs.getBoolean("ownership_transferred", false),
            adapterAddress = prefs.getString("adapter_address", "")?.trim().orEmpty(),
            vehicleId = prefs.getString("vehicle_id", "nissan_tiida")?.trim().orEmpty(),
            loggerId = loggerId,
            voltageOn = prefs.getString("voltage_on", "13.2")?.toDoubleOrNull() ?: Double.NaN,
            voltageOff = prefs.getString("voltage_off", "13.0")?.toDoubleOrNull() ?: Double.NaN,
            offGraceSeconds = prefs.getLong("off_grace_seconds", 30),
            parkedIntervalSeconds = prefs.getLong("parked_interval_seconds", 30),
            voltageOnlyMode = prefs.getBoolean("voltage_only_mode", false),
            webhookEnabled = prefs.getBoolean("webhook_enabled", true),
            webhookUrl = if (savedUrl.isBlank()) LoggerConfig.DEFAULT_WEBHOOK_URL else savedUrl,
            webhookApiKey = if (savedApiKey.isBlank()) LoggerConfig.DEFAULT_WEBHOOK_API_KEY else savedApiKey,
            backupAwakeSeconds = prefs.getInt("backup_awake_seconds", LoggerConfig.DEFAULT_BACKUP_AWAKE_SECONDS),
            idleAwakeSeconds = prefs.getInt("idle_awake_seconds", LoggerConfig.DEFAULT_IDLE_AWAKE_SECONDS),
        )
    }

    fun save(context: Context, config: LoggerConfig) {
        require(config.thresholdConfigurationValid) { "invalid voltage hysteresis or grace period" }
        context.getSharedPreferences(NAME, Context.MODE_PRIVATE).edit()
            .putBoolean("enabled", config.enabled)
            .putBoolean("ownership_transferred", config.ownershipTransferred)
            .putString("adapter_address", config.adapterAddress.uppercase())
            .putString("vehicle_id", config.vehicleId)
            .putString("logger_id", config.loggerId)
            .putString("voltage_on", config.voltageOn.toString())
            .putString("voltage_off", config.voltageOff.toString())
            .putLong("off_grace_seconds", config.offGraceSeconds)
            .putLong("parked_interval_seconds", config.parkedIntervalSeconds)
            .putBoolean("voltage_only_mode", config.voltageOnlyMode)
            .putBoolean("webhook_enabled", config.webhookEnabled)
            .putString("webhook_url", config.webhookUrl.trim())
            .putString("webhook_api_key", config.webhookApiKey.trim())
            .putInt("backup_awake_seconds", config.backupAwakeSeconds)
            .putInt("idle_awake_seconds", config.idleAwakeSeconds)
            .apply()
    }

    fun disable(context: Context) {
        context.getSharedPreferences(NAME, Context.MODE_PRIVATE).edit()
            .putBoolean("enabled", false)
            .apply()
    }
}

fun generatedLoggerId(uuid: UUID = UUID.randomUUID()): String = "logger-$uuid"

fun hasBluetoothPermissions(context: Context): Boolean {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true
    return context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) ==
        PackageManager.PERMISSION_GRANTED
}

fun hasNotificationPermission(context: Context): Boolean =
    Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
        context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
        PackageManager.PERMISSION_GRANTED

fun hasLoggerPermissions(context: Context): Boolean =
    hasBluetoothPermissions(context) && hasNotificationPermission(context)
