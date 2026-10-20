package com.dashcamstats.obdlogger

import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.util.Locale

/**
 * Turns the published `status.json` into the handful of lines a driver can read at arm's
 * length on the head unit. Pure, so it is unit-tested without a device; the activity only
 * reads the file and renders the result.
 */
internal object LiveStatusSummary {
    /** Beyond this the service is assumed stopped or wedged; the panel says so first. */
    const val STALE_AFTER_MILLIS = 90_000L
    private const val MAXIMUM_STATUS_BYTES = 65_536
    private val states = setOf(
        "disabled", "parked", "probing", "ecu_online", "logging", "idle", "backoff",
        "ingestion_ready", "ingestion_request_invalid", "storage_unavailable",
        "permission_required", "invalid_configuration", "startup_failed", "export_error", "retention_error",
    )

    /** Bounded reads run on the activity's IO dispatcher, never its UI thread. */
    fun readPublishedStatus(roots: List<File>, nowMillis: Long): JSONObject? {
        val candidates = roots.distinct().mapNotNull { root ->
            runCatching {
                val file = File(root, "status.json")
                if (!file.isFile || file.length() !in 1..MAXIMUM_STATUS_BYTES.toLong()) {
                    return@runCatching null
                }
                val bytes = ByteArray(MAXIMUM_STATUS_BYTES + 1)
                val count = file.inputStream().use { input ->
                    var total = 0
                    while (total < bytes.size) {
                        val read = input.read(bytes, total, bytes.size - total)
                        if (read < 0) break
                        total += read
                    }
                    total
                }
                if (count > MAXIMUM_STATUS_BYTES) null
                else JSONObject(String(bytes, 0, count, Charsets.UTF_8))
            }.getOrNull()
        }
        return newestStatus(candidates, nowMillis)
    }

    internal fun newestStatus(candidates: List<JSONObject>, nowMillis: Long): JSONObject? {
        val valid = candidates.mapNotNull { candidate ->
            val schema = wholeNumber(candidate, "schema_version", PUBLIC_STATUS_SCHEMA_VERSION.toLong())
            val publishedAt = instantMillis(candidate.opt("updated_at_utc") as? String ?: "")
            val state = candidate.opt("state") as? String
            if (schema == null || schema < 1 || state == null || state !in states ||
                publishedAt == null || publishedAt < 0
            ) null else candidate to publishedAt
        }
        // A clock-ahead file must not mask a current valid snapshot on the other volume.
        val current = valid.filter { (_, publishedAt) -> publishedAt <= nowMillis }
        return (current.ifEmpty { valid }).maxByOrNull { it.second }?.first
    }

    fun lines(status: JSONObject?, nowMillis: Long): List<String> {
        if (status == null) return listOf("Logger status: not published yet")
        val out = mutableListOf<String>()
        val updatedAt = instantMillis(status.optString("updated_at_utc", ""))
        val ageMillis = updatedAt?.let { ageMillis(nowMillis, it) }
        val stale = ageMillis == null || ageMillis < 0 || ageMillis > STALE_AFTER_MILLIS
        val state = allowedText(status, "state", states)
        out += if (stale) {
            val age = if (ageMillis != null && ageMillis < 0) "clock ahead" else ageText(ageMillis)
            "Logger: STALE ($age) · last state $state"
        } else {
            "Logger: ${state.uppercase(Locale.US)} · updated ${ageText(ageMillis)}"
        }
        out += "Vehicle: ${allowedText(status, "vehicle_state", states + "driving")}" +
            (if (flag(status, "engine_running")) " · engine running" else "")
        out += "Adapter: " + when {
            flag(status, "ecu_connected") -> "connected, ECU responding"
            flag(status, "adapter_connected") -> "connected, no ECU"
            flag(status, "adapter_reachable") -> "reachable, not connected"
            else -> "not reachable"
        }
        val voltage = finiteNumber(status, "battery_voltage", 9.0, 16.5)
        out += if (voltage == null) {
            "Battery: unknown"
        } else {
            val quality = allowedText(
                status, "battery_voltage_quality", setOf("valid", "measured", "stale", "invalid", "failed", "unavailable"),
            )
            val fresh = if (flag(status, "battery_voltage_fresh")) "" else " (old)"
            "Battery: ${String.format(Locale.US, "%.1f", voltage)} V$fresh" +
                " · $quality"
        }
        if (hasText(status, "current_drive_id")) out += "Drive: recording"
        val lastSample = instantMillis(status.optString("last_sample_at_utc", ""))
        out += "Last sample: " + (lastSample?.let {
            val age = ageMillis(nowMillis, it)
            if (age == null || age < 0) "clock ahead" else ageText(age) + " ago"
        } ?: "none yet")
        val pending = counter(status, "pending_bundle_count")
        out += "Bundles waiting for the server: $pending"
        out += "Wi-Fi: " + (if (flag(status, "wifi_connected")) "connected" else "off") +
            (if (flag(status, "ingestion_sleep_hold")) " · sleep held for backup" else "")
        if (hasText(status, "last_error")) {
            val at = instantMillis(status.optString("last_error_at_utc", ""))
            val age = at?.let { ageMillis(nowMillis, it) }?.takeIf { it >= 0 }
            // Error bodies can contain transport details. Keep the on-device panel identifier-free.
            out += "Last error: reported" + (age?.let { " (${ageText(it)} ago)" } ?: "")
        }
        return out
    }

    fun detailLines(status: JSONObject?): List<String> {
        if (status == null) return listOf("Metrics: not published yet")
        val metrics = status.optJSONObject("metrics")
        val target = wholeNumber(status, "sleep_window_target_s", 3_600)?.takeIf { it > 0 }
        val observed = wholeNumber(status, "sleep_window_observed_s", 3_600)?.takeIf { it > 0 }
        val verified = flag(status, "sleep_window_verified") && target != null && target == observed
        val policy = when (status.opt("sleep_window_policy")) {
            "managed_active" -> "active Wi-Fi / backup"
            "managed_idle" -> "idle"
            "awaiting_ingestion_state" -> "waiting for backup state"
            "uninitialized" -> "not initialized"
            else -> "unknown"
        }
        val sleepError = when (status.opt("sleep_window_error")) {
            "sleep countdown update was refused" -> "update refused"
            "sleep countdown readback was unavailable" -> "readback unavailable"
            "sleep countdown readback did not match the requested value" -> "readback mismatch"
            else -> if (hasText(status, "sleep_window_error")) "reported" else "none"
        }
        val acc = if (!flag(status, "acc_state_known")) "unknown"
            else if (flag(status, "acc_on")) "on" else "off"
        val hold = if (!flag(status, "ingestion_sleep_hold_known")) "unknown"
            else if (flag(status, "ingestion_sleep_hold")) "active" else "inactive"
        val duty = metrics?.let { finiteNumber(it, "polling_duty_cycle_percent", 0.0, 100.0) }
            ?.let { String.format(Locale.US, "%.1f%%", it) } ?: "unknown"
        return listOf(
            "Sleep window · $policy",
            "Target: ${target?.let { "$it s" } ?: "unknown"} · readback: ${observed?.let { "$it s" } ?: "unknown"}",
            "Verification: ${if (verified) "verified" else "not verified"}",
            "Sleep error: $sleepError",
            "ACC: $acc · backup hold: $hold",
            "Pipeline counters · published snapshot",
            "Commands requested / sent / completed: ${counters(metrics, "commands_requested", "commands_sent", "commands_completed")}",
            "Commands blocked / timed out: ${counters(metrics, "commands_blocked", "command_timeouts")}",
            "BLE attempts / connected / failed: ${counters(metrics, "ble_connection_attempts", "ble_connection_successes", "ble_connection_failures")}",
            "BLE disconnects / reconnects: ${counters(metrics, "ble_disconnects", "reconnect_attempts")}",
            "Samples created / persisted / dropped: ${counters(metrics, "samples_created", "samples_persisted", "samples_dropped")}",
            "Database write failures: ${counter(metrics, "database_write_failures")}",
            "Parser / checksum failures: ${counters(metrics, "parse_failures", "checksum_failures")}",
            "Queue depth / peak: ${counters(metrics, "queue_depth", "maximum_queue_depth")}",
            "Polling duty cycle: $duty",
        )
    }

    private fun allowedText(status: JSONObject, field: String, values: Set<String>): String =
        (status.opt(field) as? String)?.takeIf { it in values } ?: "unknown"

    private fun flag(status: JSONObject, field: String): Boolean = status.opt(field) == true

    private fun hasText(status: JSONObject, field: String): Boolean =
        (status.opt(field) as? String)?.let { it.isNotBlank() && it != "null" } == true

    private fun counter(status: JSONObject?, field: String): String =
        status?.let { wholeNumber(it, field, Int.MAX_VALUE.toLong()) }?.toString() ?: "unknown"

    private fun counters(status: JSONObject?, vararg fields: String): String =
        fields.joinToString(" / ") { counter(status, it) }

    private fun wholeNumber(status: JSONObject, field: String, maximum: Long): Long? {
        val value = finiteNumber(status, field, 0.0, maximum.toDouble()) ?: return null
        return value.toLong().takeIf { it.toDouble() == value }
    }

    private fun finiteNumber(status: JSONObject, field: String, minimum: Double, maximum: Double): Double? =
        (status.opt(field) as? Number)?.toDouble()?.takeIf { it.isFinite() && it in minimum..maximum }

    private fun ageMillis(now: Long, then: Long): Long? =
        runCatching { Math.subtractExact(now, then) }.getOrNull()

    private fun instantMillis(value: String): Long? {
        if (value.isBlank() || value == "null") return null
        return runCatching { Instant.parse(value).toEpochMilli() }.getOrNull()
    }

    internal fun ageText(ageMillis: Long?): String {
        if (ageMillis == null) return "never"
        val seconds = (ageMillis / 1000L).coerceAtLeast(0L)
        return when {
            seconds < 60 -> "${seconds}s"
            seconds < 3600 -> "${seconds / 60}m ${seconds % 60}s"
            else -> "${seconds / 3600}h ${(seconds % 3600) / 60}m"
        }
    }
}
