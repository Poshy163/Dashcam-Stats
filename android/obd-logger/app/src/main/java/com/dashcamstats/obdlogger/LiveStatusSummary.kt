package com.dashcamstats.obdlogger

import org.json.JSONObject
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

    fun lines(status: JSONObject?, nowMillis: Long): List<String> {
        if (status == null) return listOf("Logger status: not published yet")
        val out = mutableListOf<String>()
        val updatedAt = instantMillis(status.optString("updated_at_utc", ""))
        val ageMillis = updatedAt?.let { nowMillis - it }
        val stale = ageMillis == null || ageMillis > STALE_AFTER_MILLIS
        val state = status.optString("state", "unknown")
        out += if (stale) {
            "Logger: STALE (${ageText(ageMillis)}) · last state $state"
        } else {
            "Logger: ${state.uppercase()} · updated ${ageText(ageMillis)}"
        }
        out += "Vehicle: ${status.optString("vehicle_state", "unknown")}" +
            (if (status.optBoolean("engine_running", false)) " · engine running" else "")
        out += "Adapter: " + when {
            status.optBoolean("ecu_connected", false) -> "connected, ECU responding"
            status.optBoolean("adapter_connected", false) -> "connected, no ECU"
            status.optBoolean("adapter_reachable", false) -> "reachable, not connected"
            else -> "not reachable"
        }
        val voltage = status.optDouble("battery_voltage", Double.NaN)
        out += if (voltage.isNaN()) {
            "Battery: unknown"
        } else {
            val quality = status.optString("battery_voltage_quality", "")
            val fresh = if (status.optBoolean("battery_voltage_fresh", false)) "" else " (old)"
            "Battery: ${String.format(Locale.US, "%.1f", voltage)} V$fresh" +
                (if (quality.isNotBlank()) " · $quality" else "")
        }
        val drive = status.optString("current_drive_id", "")
        if (drive.isNotBlank() && drive != "null") out += "Drive: $drive"
        val lastSample = instantMillis(status.optString("last_sample_at_utc", ""))
        out += "Last sample: " + (lastSample?.let { ageText(nowMillis - it) + " ago" } ?: "none yet")
        val pending = status.optInt("pending_bundle_count", 0)
        out += "Bundles waiting for the server: $pending"
        out += "Wi-Fi: " + (if (status.optBoolean("wifi_connected", false)) "connected" else "off") +
            (if (status.optBoolean("ingestion_sleep_hold", false)) " · sleep held for backup" else "")
        val error = status.optString("last_error", "")
        if (error.isNotBlank() && error != "null") {
            val at = instantMillis(status.optString("last_error_at_utc", ""))
            out += "Last error" + (at?.let { " (${ageText(nowMillis - it)} ago)" } ?: "") + ": $error"
        }
        return out
    }

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
