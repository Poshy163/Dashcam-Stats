package com.dashcamstats.obdlogger

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import java.net.URI
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
    val webhookApiKey: String = "",
    val backupAwakeSeconds: Int = 1200,
    val idleAwakeSeconds: Int = 300,
    val webhookCredentialError: String? = null,
    val pollTrialEnabled: Boolean = false,
    val pollTrialFallbackReason: String? = null,
) {
    override fun toString(): String =
        "LoggerConfig(enabled=$enabled, ownershipTransferred=$ownershipTransferred, " +
            "voltageOnlyMode=$voltageOnlyMode, webhookEnabled=$webhookEnabled, " +
            "webhookCredentialConfigured=${webhookApiKey.isNotEmpty()}, webhookCredentialError=$webhookCredentialError)"

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
        const val DEFAULT_WEBHOOK_API_KEY = ""
        const val DEFAULT_BACKUP_AWAKE_SECONDS = 1200
        const val DEFAULT_IDLE_AWAKE_SECONDS = 300
    }
}

object LoggerPreferences {
    private const val NAME = "obd_logger"

    fun load(context: Context, includeWebhookCredential: Boolean = true): LoggerConfig {
        // Foreground/boot gates must not wait for another thread's Keystore operation.
        if (!includeWebhookCredential) return readConfiguration(context, StoredWebhookCredential())
        return load(context, WebhookCredentialStore(context.getSharedPreferences(NAME, Context.MODE_PRIVATE)))
    }

    @Synchronized
    internal fun load(context: Context, credentials: WebhookCredentialStore): LoggerConfig =
        readConfiguration(context, credentials.load())

    private fun readConfiguration(context: Context, savedCredential: StoredWebhookCredential): LoggerConfig {
        val prefs = context.getSharedPreferences(NAME, Context.MODE_PRIVATE)
        val loggerId = synchronized(preferenceWriteLock) {
            if (prefs.contains("logger_id")) {
                prefs.getString("logger_id", "")?.trim().orEmpty()
            } else {
                generatedLoggerId().also { generated ->
                    check(prefs.edit().putString("logger_id", generated).commit()) {
                        "could not persist the logger identity"
                    }
                }
            }
        }
        val savedUrl = prefs.getString("webhook_url", LoggerConfig.DEFAULT_WEBHOOK_URL)?.trim().orEmpty()
        val trial = pollTrialSnapshot(context)
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
            pollTrialEnabled = trial.enabled,
            pollTrialFallbackReason = trial.fallbackReason,
            webhookEnabled = prefs.getBoolean("webhook_enabled", true),
            webhookUrl = if (savedUrl.isBlank()) LoggerConfig.DEFAULT_WEBHOOK_URL else savedUrl,
            webhookApiKey = savedCredential.value,
            webhookCredentialError = savedCredential.error,
            backupAwakeSeconds = prefs.getInt("backup_awake_seconds", LoggerConfig.DEFAULT_BACKUP_AWAKE_SECONDS),
            idleAwakeSeconds = prefs.getInt("idle_awake_seconds", LoggerConfig.DEFAULT_IDLE_AWAKE_SECONDS),
        )
    }

    fun save(context: Context, config: LoggerConfig) = save(
        context, config, WebhookCredentialStore(context.getSharedPreferences(NAME, Context.MODE_PRIVATE)),
    )

    @Synchronized
    internal fun save(context: Context, config: LoggerConfig, credentials: WebhookCredentialStore) {
        require(config.thresholdConfigurationValid) { "invalid voltage hysteresis or grace period" }
        val editor = context.getSharedPreferences(NAME, Context.MODE_PRIVATE).edit()
        credentials.writeTo(
            editor, config.webhookApiKey.trim(), preserveUnreadable = config.webhookCredentialError != null,
        )
        val update = editor
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
            .putInt("backup_awake_seconds", config.backupAwakeSeconds)
            .putInt("idle_awake_seconds", config.idleAwakeSeconds)
        commitPreferenceUpdate(context.getSharedPreferences(NAME, Context.MODE_PRIVATE), update)
    }

    /** Called once at a new drive boundary; its result is immutable for that drive. */
    fun pollPlanForNewDrive(context: Context, supported: Set<Int>): Int {
        val requested = load(context, includeWebhookCredential = false).pollTrialEnabled
        val selected = PollTrialPlan.select(requested, supported)
        if (requested && selected != PollTrialPlan.VERSION) setPollTrial(context, false, "required_pids_unavailable")
        return selected
    }

    fun setPollTrial(context: Context, enabled: Boolean, reason: String? = null) {
        require(reason == null || reason in PollTrialPlan.fallbackReasons)
        TrialSettings.repository(context).change(enabled, reason)
    }

    internal fun pollTrialSnapshot(context: Context): TrialSettingsSnapshot = TrialSettings.repository(context).snapshot()

    /** Persist an explicit stop without validating or replacing the caller's unsaved draft. */
    internal fun disablePersisted(context: Context) {
        synchronized(preferenceWriteLock) {
            val preferences = context.getSharedPreferences(NAME, Context.MODE_PRIVATE)
            commitPreferenceUpdate(preferences, preferences.edit().putBoolean("enabled", false))
        }
    }

    fun disable(context: Context) {
        synchronized(preferenceWriteLock) {
            context.getSharedPreferences(NAME, Context.MODE_PRIVATE).edit()
                .putBoolean("enabled", false)
                .apply()
        }
    }
}

internal fun isAllowedWebhookUrl(value: String): Boolean = runCatching {
    if (value.length !in 1..MAX_WEBHOOK_URL_LENGTH) return@runCatching false
    val uri = URI(value.trim())
    if (uri.userInfo != null || uri.fragment != null || uri.query != null || uri.host.isNullOrBlank() ||
        uri.path != WEBHOOK_PATH
    ) return@runCatching false
    when (uri.scheme?.lowercase()) {
        "https" -> true
        "http" -> isPrivateOrLoopbackIpv4(uri.host)
        else -> false
    }
}.getOrDefault(false)

internal fun webhookConfigurationError(config: LoggerConfig): String? = when {
    !config.webhookEnabled -> null
    config.webhookCredentialError != null && config.webhookApiKey.isBlank() -> WEBHOOK_CREDENTIAL_UNAVAILABLE
    config.webhookApiKey.isBlank() -> "webhook_api_key_required"
    config.webhookApiKey.length > MAX_WEBHOOK_API_KEY_LENGTH -> "webhook_api_key_too_long"
    !isAllowedWebhookUrl(config.webhookUrl) -> "invalid_webhook_url"
    else -> null
}

private const val MAX_WEBHOOK_URL_LENGTH = 2_048
private const val MAX_WEBHOOK_API_KEY_LENGTH = 512
private const val WEBHOOK_PATH = "/api/ingest/webhook"

private fun isPrivateOrLoopbackIpv4(host: String): Boolean {
    if (host.equals("localhost", ignoreCase = true)) return true
    val octets = host.split('.').map { it.toIntOrNull() ?: return false }
    if (octets.size != 4 || octets.any { it !in 0..255 }) return false
    return octets[0] == 10 || octets[0] == 127 ||
        (octets[0] == 172 && octets[1] in 16..31) ||
        (octets[0] == 192 && octets[1] == 168)
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
